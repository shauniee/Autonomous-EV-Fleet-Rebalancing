import asyncio
from copy import deepcopy
from uuid import uuid4

import httpx
from httpx import AsyncClient
import pytest
from fastapi import HTTPException

from ev_fleet.common.settings import DispatchSettings, SimulatorRecoverySettings
from ev_fleet.contracts.recovery import (SimulatorCheckpoint, SimulatorCheckpointRequest,
    SimulatorConfiguration, SimulatorIdleRestoreRequest, SimulatorSessionRequest)
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.fleet.app import create_app
from ev_fleet.fleet.store import FleetStore
from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.recovery import CheckpointRunner, RecoveryRequired
from tests.unit.test_persistence import MemoryRepository


class CheckpointRepository(MemoryRepository):
    async def save(self, payload, telemetry_sample=None):
        await super().save(payload)


async def idle_vehicle(clock, charging_state="NOT_CHARGING"):
    config = SimulatorRecoverySettings(enabled=True, vehicle_ids=["ev-idle"], dispatch_url="http://dispatch")
    store = FleetStore(settings=DispatchSettings(), clock=clock, repository=CheckpointRepository(),
                       recovery_settings=config)
    await store.start()
    await store.register(VehicleRegistration(vehicle_id="ev-idle", battery_capacity_kwh=60,
                                             consumption_kwh_per_km=.18))
    receipt = await store.simulator_recovery.session("ev-idle", SimulatorSessionRequest(
        request_id=uuid4(), action="ACQUIRE", expected_fleet_instance_id=store.instance_id,
        expected_control_version=1, expected_generation=0, expected_owner_revision=0,
        expected_checkpoint_revision=0))
    physical = TelemetryUpdate(session_id=receipt.session_id, sequence=0, observed_at=clock.now(),
        position={"latitude": 1.32, "longitude": 103.81},
        soc_pct=60 if charging_state == "CHARGING" else 85.4, odometer_km=15.2,
        availability="UNAVAILABLE" if charging_state == "CHARGING" else "AVAILABLE",
        heading_deg=90, speed_kph=0, charging_state=charging_state,
        parked_facility_id="charger-idle" if charging_state != "NOT_CHARGING" else None,
        charging_power_kw=3.7 if charging_state != "NOT_CHARGING" else None)
    checkpoint = SimulatorCheckpoint(vehicle_id="ev-idle", fleet_instance_id=store.instance_id,
        physical=physical, simulation=SimulatorConfiguration(capacity_kwh=60,
            consumption_kwh_per_km=.18, speed_kph=30, time_scale=10))
    original = SimulatorCheckpointRequest(request_id=uuid4(), session_id=receipt.session_id,
        generation=1, expected_checkpoint_revision=0, checkpoint=checkpoint, publish_telemetry=True)
    await store.simulator_recovery.checkpoint("ev-idle", original)
    clock.advance(31)
    record = store.record("ev-idle")
    request = SimulatorIdleRestoreRequest(request_id=uuid4(), expected_fleet_instance_id=store.instance_id,
        expected_control_version=record["control_version"], expected_generation=1,
        expected_owner_revision=1, expected_checkpoint_revision=1, simulation=checkpoint.simulation)
    return store, request, original


def dispatch_response(monkeypatch, payload=None, status=200, before_response=None, parking=None,
                      parking_status=200):
    original_client = AsyncClient

    def respond(request):
        if parking is not None and request.url.path == "/api/v1/vehicles/ev-idle/parking":
            return httpx.Response(parking_status, json=parking)
        assert request.url.path == "/api/v1/dispatches"
        assert request.url.params["vehicle_id"] == "ev-idle"
        if before_response:
            before_response()
        return httpx.Response(status, json=payload if payload is not None else {"dispatches": []})

    monkeypatch.setattr("ev_fleet.fleet.recovery.httpx.AsyncClient",
        lambda **kwargs: original_client(transport=httpx.MockTransport(respond), **kwargs))


def completed_parking():
    return {"vehicle_id": "ev-idle", "pending": False, "parking": {
        "facility_id": "charger-idle", "name": "Idle charger", "zone_id": "bishan", "source": "MOCK",
        "position": {"latitude": 1.32, "longitude": 103.81}, "charging_allocated": True,
        "charging_power_kw": 3.7, "charging_target_soc_pct": 85.4}}


@pytest.mark.parametrize("charging_state", ["NOT_CHARGING", "COMPLETE", "CHARGING"])
def test_restore_survives_store_restart_preserves_state_and_fences_old_owner(
        clock, monkeypatch, charging_state):
    async def scenario():
        parking = completed_parking() if charging_state != "NOT_CHARGING" else None
        dispatch_response(monkeypatch, parking=parking)
        initial, request, original = await idle_vehicle(clock, charging_state)
        store = FleetStore(settings=initial.settings, clock=clock, repository=initial.repository,
                           recovery_settings=initial.simulator_recovery.settings)
        await store.start()
        before = deepcopy(store.record("ev-idle"))
        receipt = await store.simulator_recovery.session("ev-idle", request)
        assert receipt.generation == 2 and receipt.session_id != original.session_id
        assert receipt.checkpoint.completed_command_id is None
        assert receipt.checkpoint.physical.model_dump(exclude={"session_id", "sequence"}) == (
            original.checkpoint.physical.model_dump(exclude={"session_id", "sequence"}))
        after = store.record("ev-idle")
        assert after["telemetry"] == before["telemetry"]
        assert after["received_at"] == before["received_at"]
        assert after["claims"] == before["claims"]
        assert (await store.get("ev-idle"))["stale"]
        assert await store.simulator_recovery.session("ev-idle", request) == receipt
        with pytest.raises(HTTPException, match="SIMULATOR_OWNER_CONFLICT"):
            await store.simulator_recovery.checkpoint("ev-idle", original)
        # Another restore cannot consume the same evidence, even concurrently.
        with pytest.raises(HTTPException, match="SIMULATOR_EVIDENCE_CHANGED"):
            await store.simulator_recovery.session("ev-idle", request.model_copy(update={"request_id": uuid4()}))
    asyncio.run(scenario())


@pytest.mark.parametrize("charging_state", ["NOT_CHARGING", "COMPLETE", "CHARGING"])
def test_concurrent_idle_restores_admit_one_owner_after_succeeded_journey(
        clock, monkeypatch, charging_state):
    async def scenario():
        from ev_fleet.contracts.dispatch import DispatchApproval
        from ev_fleet.dispatch.store import DispatchStore
        parking = completed_parking() if charging_state != "NOT_CHARGING" else None
        dispatch_response(monkeypatch, parking=parking)
        store, request, _ = await idle_vehicle(clock, charging_state)
        completed = await DispatchStore(clock).begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        completed.data.update(vehicle_id="ev-idle", state="SUCCEEDED", claim_held=False)
        dispatch_response(monkeypatch, payload={"dispatches": [completed.view()]}, parking=parking)
        results = await asyncio.gather(store.simulator_recovery.session("ev-idle", request),
            store.simulator_recovery.session("ev-idle", request.model_copy(update={"request_id": uuid4()})),
            return_exceptions=True)
        assert sum(not isinstance(r, Exception) for r in results) == 1
        assert sum(isinstance(r, HTTPException) and r.status_code == 409 for r in results) == 1
        assert store.record("ev-idle")["simulator_owner"]["generation"] == 2
        assert completed.data["state"] == "SUCCEEDED"
    asyncio.run(scenario())


@pytest.mark.parametrize("conflict", ["live_owner", "claim", "rental", "charging_task", "moving",
                                     "configuration", "telemetry", "owner_revision", "session"])
@pytest.mark.parametrize("charging_state", ["NOT_CHARGING", "COMPLETE", "CHARGING"])
def test_idle_recovery_rejects_unsafe_state(clock, monkeypatch, conflict, charging_state):
    async def scenario():
        parking = completed_parking() if charging_state != "NOT_CHARGING" else None
        dispatch_response(monkeypatch, parking=parking)
        store, request, _ = await idle_vehicle(clock, charging_state)
        record = store.record("ev-idle")
        if conflict == "live_owner":
            record["simulator_owner"]["expires_at"] = clock.now().replace(year=2027)
        elif conflict in ("claim", "rental", "charging_task"):
            record["active_" + conflict] = str(uuid4()) if conflict == "charging_task" else {"conflict": True}
        elif conflict == "moving":
            record["simulator_checkpoint"]["data"]["physical"]["speed_kph"] = 1
        elif conflict == "configuration":
            request = request.model_copy(update={"simulation": request.simulation.model_copy(update={"time_scale": 20})})
        elif conflict == "telemetry":
            record["telemetry"] = record["telemetry"].model_copy(update={"soc_pct": 50})
        elif conflict == "session":
            record["simulator_checkpoint"]["data"]["physical"]["session_id"] = str(uuid4())
        else:
            record["simulator_owner"]["revision"] += 1
        before = deepcopy(record)
        with pytest.raises(HTTPException):
            await store.simulator_recovery.session("ev-idle", request)
        assert store.record("ev-idle") == before
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "unresolved", "race"])
@pytest.mark.parametrize("charging_state", ["NOT_CHARGING", "COMPLETE", "CHARGING"])
def test_dispatch_checks_fail_closed_and_recheck_fleet_after_io(
        clock, monkeypatch, failure, charging_state):
    async def scenario():
        parking = completed_parking() if charging_state != "NOT_CHARGING" else None
        dispatch_response(monkeypatch, parking=parking)
        store, request, _ = await idle_vehicle(clock, charging_state)
        if failure == "unavailable":
            dispatch_response(monkeypatch, status=503)
        elif failure == "malformed":
            dispatch_response(monkeypatch, payload={"missing": "dispatches"})
        elif failure == "unresolved":
            from ev_fleet.contracts.dispatch import DispatchApproval
            from ev_fleet.dispatch.store import DispatchStore
            pending = await DispatchStore(clock).begin(DispatchApproval(
                request_id=uuid4(), recommendation_id=uuid4()))
            pending.data["vehicle_id"] = "ev-idle"
            dispatch_response(monkeypatch, payload={"dispatches": [pending.view()]})
        else:
            dispatch_response(monkeypatch, before_response=lambda: store.record("ev-idle").update(
                control_version=store.record("ev-idle")["control_version"] + 1))
        with pytest.raises(HTTPException):
            await store.simulator_recovery.session("ev-idle", request)
        assert store.record("ev-idle")["simulator_owner"]["generation"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("response_delay", [0, 31])
@pytest.mark.parametrize("facilities_required,charging_state", [
    (False, "NOT_CHARGING"), (True, "NOT_CHARGING"),
    (True, "COMPLETE"), (True, "CHARGING"),
])
def test_runner_recovers_idle_after_restart_and_lost_response_then_lease_expiry(
        clock, monkeypatch, response_delay, facilities_required, charging_state):
    async def scenario():
        client_factory = AsyncClient
        parking = completed_parking() if charging_state != "NOT_CHARGING" else None
        dispatch_response(monkeypatch, parking=parking)
        store, _, original = await idle_vehicle(clock, charging_state)
        app = create_app(store)
        async with client_factory(transport=httpx.ASGITransport(app=app), base_url="http://fleet") as client:
            node = MovingNode("http://fleet", "http://dispatch", "ev-idle",
                {"latitude": 1.4, "longitude": 103.9}, 10, client=client, checkpointing=True,
                facilities_url="http://facilities" if facilities_required else None)
            runner = CheckpointRunner(node)
            runner.blocked = "EXISTING_JOURNEY_OR_OWNER_REQUIRES_PHASE4_COORDINATION"
            monkeypatch.setattr("ev_fleet.simulator.recovery.utcnow", clock.now)
            send = node.request
            lost = False

            async def lose_response(method, url, body=None, **kwargs):
                nonlocal lost
                if parking is not None and url == "http://facilities/api/v1/vehicles/ev-idle/parking":
                    return deepcopy(parking)
                result = await send(method, url, body, **kwargs)
                if method == "POST" and not lost:
                    lost = True
                    raise httpx.ReadTimeout("response lost after commit")
                return result

            node.request = lose_response
            with pytest.raises(httpx.ReadTimeout):
                await runner.try_handoff()
            request_id = runner.pending_idle_restore.request_id
            assert store.record("ev-idle")["simulator_owner"]["generation"] == 2
            # Exact retry must work while the newly committed owner is still live.
            if response_delay:
                clock.advance(response_delay)
                with pytest.raises(RecoveryRequired, match="IDLE_RECOVERY_SESSION_EXPIRED"):
                    await runner.try_handoff()
            await runner.try_handoff()
            generation = 3 if response_delay else 2
            assert runner.blocked is None and runner.lease.generation == generation
            if not response_delay:
                assert store.record("ev-idle")["simulator_restore"]["request_id"] == str(request_id)
            assert node.movement.position == original.checkpoint.physical.position.model_dump()
            assert node.movement.soc_pct == original.checkpoint.physical.soc_pct
            assert node.movement.odometer_km == original.checkpoint.physical.odometer_km
            assert not node.ready.is_set() and (await store.get("ev-idle"))["stale"]
            await runner.checkpoint()
            assert node.ready.is_set() and not (await store.get("ev-idle"))["stale"]
            if facilities_required and charging_state == "NOT_CHARGING":
                assert (await store.get("ev-idle"))["effective_availability"] == "UNAVAILABLE"
            if charging_state != "NOT_CHARGING":
                assert node.movement.charging_state == charging_state and node.parking_verified
                assert node.movement.parking["facility_id"] == original.checkpoint.physical.parked_facility_id
                proof = store.record("ev-idle")["simulator_checkpoint"]["charging"]
                assert proof["generation"] == generation
            clock.advance(31)
            runner.blocked = "SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED"
            await runner.try_handoff()
            assert runner.blocked is None and runner.lease.generation == generation + 1
            await runner.checkpoint()
            assert not (await store.get("ev-idle"))["stale"]
            assert node.movement.soc_pct == original.checkpoint.physical.soc_pct
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["unavailable", "malformed", "pending", "missing", "allocation",
                                     "facility", "power", "position", "target", "vehicle"])
@pytest.mark.parametrize("charging_state", ["COMPLETE", "CHARGING"])
def test_charging_idle_restore_requires_current_charger_evidence(
        clock, monkeypatch, failure, charging_state):
    async def scenario():
        parking = completed_parking()
        dispatch_response(monkeypatch, parking=parking)
        store, request, _ = await idle_vehicle(clock, charging_state)
        if failure == "unavailable":
            dispatch_response(monkeypatch, parking=parking, parking_status=503)
        elif failure == "malformed":
            parking["parking"]["charging_power_kw"] = "invalid"
        elif failure == "pending":
            parking["pending"] = True
        elif failure == "missing":
            parking["parking"] = None
        elif failure == "allocation":
            parking["parking"]["charging_allocated"] = False
        elif failure == "facility":
            parking["parking"]["facility_id"] = "another-charger"
        elif failure == "power":
            parking["parking"]["charging_power_kw"] = 7.4
        elif failure == "position":
            parking["parking"]["position"]["latitude"] = 1.4
        elif failure == "target":
            parking["parking"]["charging_target_soc_pct"] = (
                60 if charging_state == "CHARGING" else 90)
        else:
            parking["vehicle_id"] = "another-ev"
        before = deepcopy(store.record("ev-idle"))
        with pytest.raises(HTTPException):
            await store.simulator_recovery.session("ev-idle", request)
        assert store.record("ev-idle") == before
    asyncio.run(scenario())


@pytest.mark.parametrize("charging_state", ["COMPLETE", "CHARGING"])
@pytest.mark.parametrize("changed", ["pending", "allocation", "target"])
def test_charging_restore_does_not_publish_before_parking_reverification(
        clock, monkeypatch, charging_state, changed):
    async def scenario():
        client_factory = AsyncClient
        parking = completed_parking()
        dispatch_response(monkeypatch, parking=parking)
        store, _, original = await idle_vehicle(clock, charging_state)
        async with client_factory(transport=httpx.ASGITransport(app=create_app(store)),
                                  base_url="http://fleet") as client:
            node = MovingNode("http://fleet", "http://dispatch", "ev-idle",
                {"latitude": 1.4, "longitude": 103.9}, 10, client=client,
                checkpointing=True, facilities_url="http://facilities")
            runner = CheckpointRunner(node)
            runner.blocked = "EXISTING_JOURNEY_OR_OWNER_REQUIRES_PHASE4_COORDINATION"
            monkeypatch.setattr("ev_fleet.simulator.recovery.utcnow", clock.now)
            send = node.request

            async def dependencies(method, url, body=None, **kwargs):
                if url == "http://facilities/api/v1/vehicles/ev-idle/parking":
                    return deepcopy(parking)
                return await send(method, url, body, **kwargs)

            node.request = dependencies
            await runner.try_handoff()
            before = deepcopy(store.record("ev-idle"))
            if changed == "pending":
                parking["pending"] = True
            elif changed == "allocation":
                parking["parking"]["charging_allocated"] = False
            else:
                parking["parking"]["charging_target_soc_pct"] = (
                    60 if charging_state == "CHARGING" else 90)
            with pytest.raises(RecoveryRequired, match="IDLE_RECOVERY_CHARGING_CONTEXT_CHANGED"):
                await runner.checkpoint()
            assert store.record("ev-idle") == before
            assert runner.pending_checkpoint is None and not node.ready.is_set()
            parking.update(completed_parking())
            await runner.checkpoint()
            telemetry = (await store.get("ev-idle"))["telemetry"]
            assert telemetry["soc_pct"] == original.checkpoint.physical.soc_pct
            assert telemetry["position"] == original.checkpoint.physical.position.model_dump()
            assert telemetry["odometer_km"] == original.checkpoint.physical.odometer_km
            assert telemetry["charging_state"] == charging_state
            assert node.ready.is_set()
    asyncio.run(scenario())


@pytest.mark.parametrize("lost_baseline_response", [False, True])
def test_restored_charging_credits_only_time_after_fresh_baseline(
        clock, monkeypatch, lost_baseline_response):
    from types import SimpleNamespace

    async def scenario():
        parking = completed_parking()
        dispatch_response(monkeypatch, parking=parking)
        initial, _, original = await idle_vehicle(clock, "CHARGING")
        old_epoch = initial.simulator_recovery.charging_epoch
        clock.advance(3600)
        store = FleetStore(
            settings=initial.settings, clock=clock,
            repository=initial.repository,
            recovery_settings=initial.simulator_recovery.settings)
        await store.start()
        monkeypatch.setattr("ev_fleet.simulator.recovery.utcnow", clock.now)
        monkeypatch.setattr(
            "ev_fleet.simulator.recovery.time",
            SimpleNamespace(monotonic=clock.monotonic))
        async with AsyncClient(
                transport=httpx.ASGITransport(app=create_app(store)),
                base_url="http://fleet") as client:
            node = MovingNode(
                "http://fleet", "http://dispatch", "ev-idle",
                {"latitude": 1.4, "longitude": 103.9}, 10,
                client=client, checkpointing=True,
                facilities_url="http://facilities")
            node.traffic_enabled = False
            runner = CheckpointRunner(node)
            runner.blocked = "SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED"
            send = node.request
            checkpoint_requests = []

            async def dependencies(method, url, body=None, **kwargs):
                if url == "http://facilities/api/v1/vehicles/ev-idle/parking":
                    return deepcopy(parking)
                if url == "http://dispatch/api/v1/vehicles/ev-idle/commands":
                    return {"commands": []}
                if method == "PUT":
                    checkpoint_requests.append(deepcopy(body))
                result = await send(method, url, body, **kwargs)
                if (method == "PUT" and lost_baseline_response
                        and len(checkpoint_requests) == 1):
                    raise httpx.ReadTimeout("baseline response lost after commit")
                return result

            node.request = dependencies
            await runner.try_handoff()
            assert runner.lease is not None
            assert store.record("ev-idle")["simulator_checkpoint"]["charging"] is None
            assert store.simulator_recovery.charging_epoch != old_epoch

            async def reject_energy_gain(amount, error):
                before = deepcopy(store.record("ev-idle"))
                request = SimulatorCheckpointRequest.model_validate(
                    runner.checkpoint_request())
                assert runner.last_checkpoint is not None
                physical = runner.last_checkpoint.physical.model_copy(update={
                    "sequence": request.checkpoint.physical.sequence,
                    "observed_at": clock.now(),
                    "soc_pct": node.movement.soc_pct + amount})
                request = request.model_copy(update={
                    "checkpoint": request.checkpoint.model_copy(update={
                        "physical": physical})})
                with pytest.raises(HTTPException, match=error):
                    await store.simulator_recovery.checkpoint("ev-idle", request)
                assert store.record("ev-idle") == before

            await reject_energy_gain(.1, "SIMULATOR_CHARGING_BASELINE_REQUIRED")
            # Neither outage nor waiting to publish can become chargeable time.
            clock.advance(8)
            if lost_baseline_response:
                with pytest.raises(httpx.ReadTimeout):
                    await runner.tick()
                assert not node.ready.is_set()
                clock.advance(3)
            await runner.tick()
            if lost_baseline_response:
                assert checkpoint_requests[0] == checkpoint_requests[1]
            assert not runner.restore_pending and node.ready.is_set()
            assert node.movement.soc_pct == original.checkpoint.physical.soc_pct
            baseline = store.record("ev-idle")["simulator_checkpoint"]
            proof = baseline["charging"]
            assert proof["epoch"] == store.simulator_recovery.charging_epoch
            assert proof["generation"] == runner.lease.generation
            assert proof["session_id"] == str(runner.lease.session_id)
            assert proof["verified_at"] == clock.now()
            assert (await store.get("ev-idle"))["effective_availability"] == "UNAVAILABLE"

            clock.advance(2)
            await reject_energy_gain(1, "SIMULATOR_CHARGING_ENERGY_MISMATCH")
            await runner.tick()
            expected_soc = original.checkpoint.physical.soc_pct + (
                100 * 3.7 * 2 * node.time_scale / (3600 * node.movement.capacity))
            current = await store.get("ev-idle")
            assert current["telemetry"]["soc_pct"] == pytest.approx(expected_soc)
            assert current["telemetry"]["charging_state"] == "CHARGING"
            assert current["telemetry"]["position"] == (
                original.checkpoint.physical.position.model_dump())
            assert current["telemetry"]["odometer_km"] == (
                original.checkpoint.physical.odometer_km)
            assert current["effective_availability"] == "UNAVAILABLE"
    asyncio.run(scenario())
