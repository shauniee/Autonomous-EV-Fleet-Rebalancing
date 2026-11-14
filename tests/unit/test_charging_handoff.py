import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
from httpx import AsyncClient
import pytest

from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.facilities.app import create_app
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.simulator.movement import Movement
from tests.unit.test_persistence import MemoryRepository


def test_arrival_waits_for_cable_and_completion_waits_for_unplugging():
    position = {"latitude": 1.283, "longitude": 103.8513}
    parking = {"facility_id": "charger", "position": position,
               "charging_allocated": True, "charging_power_kw": 12,
               "charging_target_soc_pct": 80}
    moving = Movement(position, 70)
    assert moving.park(parking)
    assert moving.charging_state == "AWAITING_CONNECTION"
    assert moving.telemetry()["availability"] == "UNAVAILABLE"
    moving.charge(1800)
    assert moving.soc_pct == 70
    assert moving.park({**parking, "cable_status": "CONNECTED"})
    moving.charge(1800)
    assert moving.soc_pct == pytest.approx(80)
    assert moving.charging_state == "COMPLETE"
    assert moving.telemetry()["availability"] == "UNAVAILABLE"
    assert moving.park({**parking, "cable_status": "DISCONNECTED"})
    assert moving.charging_state == "NOT_CHARGING"
    assert moving.telemetry()["availability"] == "AVAILABLE"
    moving.charge(1800)
    assert moving.soc_pct == 80


def test_waiting_or_connected_vehicle_cannot_accept_a_journey():
    from tests.unit.test_movement import command
    cmd = command()
    for cable in ("AWAITING_CONNECTION", "CONNECTED"):
        moving = Movement(cmd["route"]["geometry"][0], 85)
        moving.park({"facility_id": "charger", "position": moving.position,
                     "charging_allocated": True, "charging_power_kw": 12,
                     "charging_target_soc_pct": 80, "cable_status": cable})
        with pytest.raises(ValueError, match="cable"):
            moving.accept(cmd)
        assert moving.command is None


async def setup(clock):
    store = FacilitiesStore(repository=MemoryRepository(), clock=clock)
    await store.start()
    site = next(s for s in store.catalogue.values() if s.charging_available)
    dispatch_id = uuid4()
    await store.reserve(site.facility_id, ParkingHoldRequest(
        dispatch_id=dispatch_id, vehicle_id="ev-1", expected_zone_id=site.zone_id,
        expected_position=site.position, access_position=site.position,
        requires_charging=True))
    await store.change(dispatch_id, "OCCUPIED")
    vehicle = {"vehicle_id": "ev-1", "battery_capacity_kwh": 60,
               "consumption_kwh_per_km": .18, "state_version": 3,
               "stale": False, "connectivity": "ONLINE",
               "received_at": clock.now().isoformat(), "telemetry_age_seconds": 0,
               "telemetry": {"session_id": str(uuid4()), "sequence": 1,
                   "observed_at": clock.now().isoformat(), "position": site.position.model_dump(),
                   "soc_pct": 70, "availability": "UNAVAILABLE", "relocation_state": "IDLE",
                   "parked_facility_id": site.facility_id, "speed_kph": 0,
                   "charging_state": "AWAITING_CONNECTION"}}
    async def respond(request):
        assert request.url.path == "/api/v1/vehicles/ev-1"
        return httpx.Response(200, json=vehicle)
    fleet_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    app = create_app(store=store, charging_client=fleet_client)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://facilities")
    return store, site, dispatch_id, vehicle, fleet_client, client


def body(dispatch_id, action="CONNECT", **changes):
    return {"dispatch_id": str(dispatch_id), "request_id": str(uuid4()),
            "action": action, "staff_name": "Ground Team A", **changes}


def test_confirmations_are_allocation_bound_idempotent_and_durable(clock):
    async def scenario():
        store, _, dispatch_id, vehicle, fleet_client, client = await setup(clock)
        async with fleet_client, client:
            path = "/api/v1/vehicles/ev-1/charging-cable"
            waiting = (await client.get("/api/v1/vehicles/ev-1/parking")).json()["parking"]
            assert waiting["cable_status"] == "AWAITING_CONNECTION"
            assert waiting["charging_session_id"] == str(dispatch_id)
            request = body(dispatch_id)
            connected = await client.post(path, json=request)
            assert connected.status_code == 200
            assert connected.json()["parking"]["cable_connected_by"] == "Ground Team A"
            saves = store.repository.save_count
            vehicle["stale"] = True
            assert (await client.post(path, json=request)).json() == connected.json()
            assert store.repository.save_count == saves
            vehicle["stale"] = False
            assert (await client.post(path, json=body(dispatch_id, "DISCONNECT"))).status_code == 409
            vehicle["telemetry"].update(charging_state="COMPLETE", soc_pct=80)
            disconnected = await client.post(path, json=body(dispatch_id, "DISCONNECT"))
            assert disconnected.status_code == 200
            assert disconnected.json()["parking"]["cable_status"] == "DISCONNECTED"
            # A lost connection response cannot reconnect a finished session.
            assert (await client.post(path, json=request)).json() == connected.json()
            restored = FacilitiesStore(repository=store.repository, clock=clock)
            await restored.start()
            assert (await restored.vehicle_parking("ev-1"))["parking"]["cable_status"] == "DISCONNECTED"
    asyncio.run(scenario())


@pytest.mark.parametrize("invalid", ["stale", "busy", "moving", "wrong_site", "wrong_session", "empty_staff", "far", "old_time"])
def test_invalid_confirmation_cannot_change_allocation(clock, invalid):
    async def scenario():
        store, _, dispatch_id, vehicle, fleet_client, client = await setup(clock)
        request = body(dispatch_id)
        if invalid == "stale": vehicle["stale"] = True
        elif invalid == "busy": vehicle["active_rental"] = {"invalid": "busy"}
        elif invalid == "moving": vehicle["telemetry"]["speed_kph"] = 10
        elif invalid == "wrong_site": vehicle["telemetry"]["parked_facility_id"] = "elsewhere"
        elif invalid == "wrong_session": request["dispatch_id"] = str(uuid4())
        elif invalid == "empty_staff": request["staff_name"] = "   "
        elif invalid == "far": vehicle["telemetry"]["position"] = {"latitude": 1.48, "longitude": 103.99}
        else: clock.advance(20)
        before = deepcopy(store.holds)
        async with fleet_client, client:
            response = await client.post("/api/v1/vehicles/ev-1/charging-cable", json=request)
            assert response.status_code in (409, 422, 503)
            assert store.holds == before
    asyncio.run(scenario())


def test_failed_persistence_does_not_authorize_connection(clock, monkeypatch):
    async def scenario():
        store, _, dispatch_id, _, fleet_client, client = await setup(clock)
        before = deepcopy(store.holds)
        async def fail(**_kwargs):
            raise RuntimeError("write failed")
        monkeypatch.setattr(store, "persist", fail)
        async with fleet_client, client:
            with pytest.raises(RuntimeError, match="write failed"):
                await client.post("/api/v1/vehicles/ev-1/charging-cable", json=body(dispatch_id))
            assert store.holds == before
    asyncio.run(scenario())


def test_connection_is_invisible_to_simulator_until_persistence_finishes(clock, monkeypatch):
    async def scenario():
        store, _, dispatch_id, _, fleet_client, client = await setup(clock)
        entered, release = asyncio.Event(), asyncio.Event()
        persist = store.persist
        async def delayed(**kwargs):
            entered.set()
            await release.wait()
            await persist(**kwargs)
        monkeypatch.setattr(store, "persist", delayed)
        async with fleet_client, client:
            task = asyncio.create_task(client.post("/api/v1/vehicles/ev-1/charging-cable", json=body(dispatch_id)))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                parking = await store.vehicle_parking("ev-1")
                assert parking["parking"]["cable_status"] == "AWAITING_CONNECTION"
            finally:
                release.set()
                response = await task
            assert response.status_code == 200
            assert (await store.vehicle_parking("ev-1"))["parking"]["cable_status"] == "CONNECTED"
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["pending", "replaced", "competing_confirmation"])
def test_allocation_is_rechecked_after_fleet_network_wait(clock, change):
    async def scenario():
        store, site, dispatch_id, vehicle, fleet_client, client = await setup(clock)
        async def respond(request):
            if change == "pending":
                await store.reserve(site.facility_id, ParkingHoldRequest(
                    dispatch_id=uuid4(), vehicle_id="ev-1", expected_zone_id=site.zone_id,
                    expected_position=site.position, requires_charging=True))
            elif change == "replaced":
                store.holds[str(dispatch_id)]["status"] = "RELEASED"
            else:
                store.holds[str(dispatch_id)]["cable"] = {"status": "CONNECTED"}
            return httpx.Response(200, json=vehicle)
        fleet_client._transport = httpx.MockTransport(respond)
        async with fleet_client, client:
            response = await client.post("/api/v1/vehicles/ev-1/charging-cable", json=body(dispatch_id))
            assert response.status_code == 409
            assert not store.holds[str(dispatch_id)].get("cable_requests")
    asyncio.run(scenario())


def test_fleet_refuses_release_before_human_disconnect(clock, monkeypatch):
    from fastapi import HTTPException
    from tests.unit.test_charging_verification_concurrency import charging_cluster, checkpoint_request
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as (_, store, _, _):
            request = checkpoint_request(store)
            physical = type(request.checkpoint.physical).model_validate({
                **request.checkpoint.physical.model_dump(),
                "charging_state": "NOT_CHARGING", "availability": "AVAILABLE"})
            request = request.model_copy(update={"checkpoint": request.checkpoint.model_copy(
                update={"physical": physical})})
            with pytest.raises(HTTPException, match="CABLE"):
                await store.simulator_recovery.checkpoint("ev-idle", request)
    asyncio.run(scenario())


def test_fleet_refuses_energy_gain_without_connected_cable(clock, monkeypatch):
    from fastapi import HTTPException
    from tests.unit.test_charging_verification_concurrency import charging_cluster, checkpoint_request
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as (_, store, facilities, _):
            occupied = next(h for h in facilities.holds.values() if h["status"] == "OCCUPIED")
            occupied["cable"]["status"] = "AWAITING_CONNECTION"
            clock.advance(1)
            with pytest.raises(HTTPException):
                await store.simulator_recovery.checkpoint("ev-idle", checkpoint_request(store, soc=60.001))
    asyncio.run(scenario())


def test_full_human_handoff_through_facilities_simulator_and_fleet(clock, monkeypatch):
    from tests.unit.test_charging_verification_concurrency import charging_cluster
    async def scenario():
        async with charging_cluster(clock, monkeypatch, "AWAITING_CONNECTION", soc=79.99) as data:
            runner, fleet, facilities, _ = data
            app = create_app(store=facilities, charging_client=runner.node.client)
            async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://facilities") as client:
                # Replace the older recovery fixture's static parking reply
                # with the actual Facilities allocation for this full flow.
                send = runner.node.request
                async def dependencies(method, url, body=None, **kwargs):
                    if url == "http://facilities/api/v1/vehicles/ev-idle/parking":
                        return await facilities.vehicle_parking("ev-idle")
                    return await send(method, url, body, **kwargs)
                runner.node.request = dependencies
                path = "/api/v1/vehicles/ev-idle/charging-cable"
                visit = (await facilities.vehicle_parking("ev-idle"))["parking"]
                request = body(visit["charging_session_id"])
                await runner.parking()
                await runner.checkpoint()
                clock.advance(1)
                runner.advance()
                await runner.checkpoint()
                waiting = await fleet.get("ev-idle")
                assert waiting["telemetry"]["soc_pct"] == 79.99
                assert waiting["effective_availability"] == "UNAVAILABLE"
                connected = await client.post(path, json=request)
                assert connected.status_code == 200, connected.text
                await runner.parking()
                await runner.checkpoint()  # unchanged-SOC baseline after connection
                clock.advance(1)
                runner.advance()
                await runner.checkpoint()
                complete = await fleet.get("ev-idle")
                assert complete["telemetry"]["soc_pct"] == 80
                assert complete["telemetry"]["charging_state"] == "COMPLETE"
                assert complete["effective_availability"] == "UNAVAILABLE"
                disconnected = await client.post(path, json=body(visit["charging_session_id"], "DISCONNECT"))
                assert disconnected.status_code == 200, disconnected.text
                assert (await fleet.get("ev-idle"))["effective_availability"] == "UNAVAILABLE"
                await runner.parking()
                await runner.checkpoint()
                ready = await fleet.get("ev-idle")
                assert ready["telemetry"]["soc_pct"] == 80
                assert ready["telemetry"]["charging_state"] == "NOT_CHARGING"
                assert ready["effective_availability"] == "AVAILABLE"
    asyncio.run(scenario())


@pytest.mark.parametrize("legacy_state", ["CHARGING", "COMPLETE"])
def test_legacy_charging_visit_recovers_to_human_confirmation_without_energy_gain(clock, monkeypatch, legacy_state):
    from ev_fleet.contracts.recovery import SimulatorCheckpoint
    from tests.unit.test_idle_simulator_recovery import completed_parking, dispatch_response, idle_vehicle
    parking = completed_parking()
    parking["parking"].pop("cable_status")
    async def scenario():
        # Seed a real pre-feature saved checkpoint; it had no cable proof and
        # the old COMPLETE publisher reported AVAILABLE while still at a charger.
        connected = completed_parking()
        dispatch_response(monkeypatch, parking=connected)
        store, request, original = await idle_vehicle(clock, legacy_state)
        record = store.record("ev-idle")
        if legacy_state == "COMPLETE":
            physical = type(original.checkpoint.physical).model_validate({
                **record["telemetry"].model_dump(), "availability": "AVAILABLE"})
            record["telemetry"] = physical
            record["simulator_checkpoint"]["data"]["physical"] = physical.model_dump(mode="json")
        await store.simulator_recovery.close()
        dispatch_response(monkeypatch, parking=parking)
        before = SimulatorCheckpoint.model_validate(record["simulator_checkpoint"]["data"])
        restored = await store.simulator_recovery.session("ev-idle", request)
        assert restored.checkpoint.physical.charging_state == "AWAITING_CONNECTION"
        assert restored.checkpoint.physical.availability == "UNAVAILABLE"
        assert restored.checkpoint.physical.position == before.physical.position
        assert restored.checkpoint.physical.soc_pct == before.physical.soc_pct
        assert restored.checkpoint.physical.odometer_km == before.physical.odometer_km
        assert (await store.get("ev-idle"))["effective_availability"] == "UNAVAILABLE"
        assert await store.simulator_recovery.session("ev-idle", request) == restored
        from ev_fleet.contracts.recovery import SimulatorCheckpointRequest
        sample = restored.checkpoint.physical.model_copy(update={
            "sequence": 1, "observed_at": clock.now()})
        await store.simulator_recovery.checkpoint("ev-idle", SimulatorCheckpointRequest(
            request_id=uuid4(), session_id=restored.session_id, generation=restored.generation,
            expected_checkpoint_revision=restored.checkpoint_revision,
            checkpoint=restored.checkpoint.model_copy(update={"physical": sample}),
            publish_telemetry=True))
        current = await store.get("ev-idle")
        assert not current["stale"]
        assert current["telemetry"]["charging_state"] == "AWAITING_CONNECTION"
        assert current["effective_availability"] == "UNAVAILABLE"
    asyncio.run(scenario())
