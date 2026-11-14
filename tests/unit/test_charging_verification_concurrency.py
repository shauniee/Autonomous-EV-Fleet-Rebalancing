import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from uuid import uuid4

import httpx
from httpx import AsyncClient
import pytest
from fastapi import HTTPException

from ev_fleet.contracts.facilities import Facility, ParkingHoldRequest
from ev_fleet.contracts.recovery import (
    SimulatorCheckpoint, SimulatorCheckpointRequest, SimulatorSessionRequest,
    SimulatorIdleRestoreRequest,
)
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.facilities.app import create_app as facilities_app
from ev_fleet.facilities.store import FacilitiesStore
from tests.unit.test_guarded_simulator_communication import idle_runner
from tests.unit.test_persistence import MemoryRepository


def checkpoint_request(store, vehicle_id="ev-idle", soc=None):
    record = store.record(vehicle_id)
    previous = record["simulator_checkpoint"]
    saved = SimulatorCheckpoint.model_validate(previous["data"])
    changes = {"sequence": saved.physical.sequence + 1,
               "observed_at": store.clock.now()}
    if soc is not None:
        changes["soc_pct"] = soc
    return SimulatorCheckpointRequest(
        request_id=uuid4(), session_id=record["session_id"],
        generation=record["simulator_owner"]["generation"],
        expected_checkpoint_revision=previous["revision"],
        checkpoint=saved.model_copy(update={
            "physical": saved.physical.model_copy(update=changes)}),
        publish_telemetry=True)


def renewal_request(store, vehicle_id):
    record = store.record(vehicle_id)
    owner = record["simulator_owner"]
    return SimulatorSessionRequest(
        request_id=uuid4(), action="RENEW", session_id=record["session_id"],
        expected_fleet_instance_id=store.instance_id,
        expected_control_version=record["control_version"],
        expected_generation=owner["generation"],
        expected_owner_revision=owner["revision"],
        expected_checkpoint_revision=record["simulator_checkpoint"]["revision"])


def restore_request(store):
    record = store.record("ev-idle")
    saved = SimulatorCheckpoint.model_validate(
        record["simulator_checkpoint"]["data"])
    assert saved.simulation is not None
    owner = record["simulator_owner"]
    return SimulatorIdleRestoreRequest(
        request_id=uuid4(), expected_fleet_instance_id=store.instance_id,
        expected_control_version=record["control_version"],
        expected_generation=owner["generation"],
        expected_owner_revision=owner["revision"],
        expected_checkpoint_revision=record["simulator_checkpoint"]["revision"],
        simulation=saved.simulation)


async def add_other_vehicle(store):
    store.simulator_recovery.settings.vehicle_ids.append("ev-other")
    await store.register(VehicleRegistration(
        vehicle_id="ev-other", battery_capacity_kwh=60,
        consumption_kwh_per_km=.18))
    session = await store.simulator_recovery.session(
        "ev-other", SimulatorSessionRequest(
            request_id=uuid4(), action="ACQUIRE",
            expected_fleet_instance_id=store.instance_id,
            expected_control_version=1, expected_generation=0,
            expected_owner_revision=0, expected_checkpoint_revision=0))
    configuration = SimulatorCheckpoint.model_validate(
        store.record("ev-idle")["simulator_checkpoint"]["data"]).simulation
    saved = SimulatorCheckpoint(
        vehicle_id="ev-other", fleet_instance_id=store.instance_id,
        simulation=configuration, physical=TelemetryUpdate(
            session_id=session.session_id, sequence=0,
            observed_at=store.clock.now(), soc_pct=90,
            position={"latitude": 1.33, "longitude": 103.81},
            heading_deg=0, odometer_km=0, speed_kph=0))
    await store.simulator_recovery.checkpoint(
        "ev-other", SimulatorCheckpointRequest(
            request_id=uuid4(), session_id=session.session_id,
            generation=1, expected_checkpoint_revision=0,
            checkpoint=saved, publish_telemetry=True))


async def occupy(facilities, vehicle_id="ev-idle", connected=True):
    site = facilities.catalogue["charger-idle"]
    dispatch_id = uuid4()
    await facilities.reserve(site.facility_id, ParkingHoldRequest(
        dispatch_id=dispatch_id, vehicle_id=vehicle_id,
        expected_zone_id=site.zone_id, expected_position=site.position,
        requires_charging=True))
    await facilities.change(dispatch_id, "OCCUPIED")
    # This fixture starts after an explicit ground-team connection.
    facilities.holds[str(dispatch_id)]["cable"] = {"status": "CONNECTED" if connected else "AWAITING_CONNECTION"}
    await facilities.persist()
    return dispatch_id


@asynccontextmanager
async def charging_cluster(clock, monkeypatch, charging_state="CHARGING", soc=None):
    async with idle_runner(clock, monkeypatch, charging_state, soc=soc) as data:
        runner, store, _, _ = data
        facilities = FacilitiesStore(repository=MemoryRepository(), clock=clock)
        facilities.catalogue = {"charger-idle": Facility(
            facility_id="charger-idle", name="Idle charger", zone_id="bishan",
            position={"latitude": 1.32, "longitude": 103.81},
            parking_capacity=4, parking_available=4,
            charging_bays=4, charging_available=4, charging_power_kw=3.7,
            connector_types=["TYPE_2"], status="OPEN")}
        app = facilities_app(facilities)
        async with app.router.lifespan_context(app):
            await occupy(facilities, connected=charging_state != "AWAITING_CONNECTION")
            await store.simulator_recovery.close()
            client = AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://facilities")
            store.simulator_recovery.charging_client = client
            try:
                await runner.checkpoint()  # Establish the new allocation proof.
                yield runner, store, facilities, client
            finally:
                await store.close()


def pause_first_observation(client, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    get = client.get
    calls = []

    async def delayed(*args, **kwargs):
        response = await get(*args, **kwargs)
        calls.append(response)
        if len(calls) == 1:
            entered.set()
            await release.wait()
        return response

    monkeypatch.setattr(client, "get", delayed)
    return entered, release


@pytest.mark.parametrize("operation", ["checkpoint", "restore"])
def test_slow_observation_allows_other_reads_renewals_and_commits(
        clock, monkeypatch, operation):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            if operation == "restore":
                clock.advance(31)
            request = (restore_request(store) if operation == "restore"
                       else checkpoint_request(store))
            method = (store.simulator_recovery.session if operation == "restore"
                      else store.simulator_recovery.checkpoint)
            await add_other_vehicle(store)
            entered, release = pause_first_observation(client, monkeypatch)
            task = asyncio.create_task(method("ev-idle", request))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                assert not store.lock.locked()
                other = await asyncio.wait_for(store.get("ev-other"), .2)
                assert other["vehicle_id"] == "ev-other"
                await asyncio.wait_for(store.simulator_recovery.session(
                    "ev-other", renewal_request(store, "ev-other")), .2)
                await asyncio.wait_for(store.simulator_recovery.checkpoint(
                    "ev-other", checkpoint_request(store, "ev-other")), .2)
            finally:
                release.set()
                await task
            assert store.record("ev-other")["simulator_owner"]["revision"] == 2
            assert store.record("ev-other")["simulator_checkpoint"]["revision"] == 2
            assert store.record("ev-idle")["simulator_checkpoint"]["revision"] == (
                request.expected_checkpoint_revision + 1)
            assert store.repository.payload["vehicles"] == store.vehicles
    asyncio.run(scenario())


@pytest.mark.parametrize("changed", [
    "owner", "checkpoint", "control", "physical", "claim", "rental",
    "charging_task", "expiry",
])
def test_changed_target_evidence_is_revalidated_before_commit(
        clock, monkeypatch, changed):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            entered, release = pause_first_observation(client, monkeypatch)
            request = checkpoint_request(store)
            task = asyncio.create_task(store.simulator_recovery.checkpoint(
                "ev-idle", request))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                if changed == "owner":
                    await store.simulator_recovery.session(
                        "ev-idle", renewal_request(store, "ev-idle"))
                elif changed == "checkpoint":
                    await store.simulator_recovery.checkpoint(
                        "ev-idle", checkpoint_request(store))
                else:
                    async with store.lock:
                        record = store.record("ev-idle")
                        if changed == "control":
                            record["control_version"] += 1
                        elif changed == "physical":
                            record["telemetry"] = record["telemetry"].model_copy(
                                update={"battery_temp_c": 42})
                        elif changed == "claim":
                            record["active_claim"] = {"conflict": True}
                        elif changed in ("rental", "charging_task"):
                            record["active_" + changed] = "conflict"
                        else:
                            clock.advance(31)
                before = deepcopy(store.record("ev-idle"))
                release.set()
                with pytest.raises(HTTPException):
                    await task
                assert store.record("ev-idle") == before
                assert before["simulator_checkpoint"]["request_id"] != str(request.request_id)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["checkpoint", "restore"])
def test_lock_queue_time_expires_external_evidence(clock, monkeypatch, operation):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            if operation == "restore":
                clock.advance(31)
            request = (restore_request(store) if operation == "restore"
                       else checkpoint_request(store))
            method = (store.simulator_recovery.session if operation == "restore"
                      else store.simulator_recovery.checkpoint)
            entered, release = pause_first_observation(client, monkeypatch)
            task = asyncio.create_task(method("ev-idle", request))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                async with store.lock:
                    release.set()
                    await asyncio.sleep(0)
                    clock.advance(2.1)
                with pytest.raises(HTTPException, match="EVIDENCE_EXPIRED"):
                    await task
                assert store.record("ev-idle")["simulator_checkpoint"]["revision"] == (
                    request.expected_checkpoint_revision)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_concurrent_exact_retry_returns_one_receipt_after_expiry(
        clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            entered, release = pause_first_observation(client, monkeypatch)
            request = checkpoint_request(store)
            task = asyncio.create_task(store.simulator_recovery.checkpoint(
                "ev-idle", request))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                receipt = await store.simulator_recovery.checkpoint("ev-idle", request)
                clock.advance(31)
                release.set()
                assert await task == receipt
                assert store.record("ev-idle")["simulator_checkpoint"]["revision"] == (
                    request.expected_checkpoint_revision + 1)
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_cancelled_observation_leaves_persistence_and_physical_state_intact(
        clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            entered, release = pause_first_observation(client, monkeypatch)
            before = deepcopy(store.record("ev-idle"))
            task = asyncio.create_task(store.simulator_recovery.checkpoint(
                "ev-idle", checkpoint_request(store)))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert store.record("ev-idle") == before
                assert store.ready and not store.persistence_uncertain
            finally:
                release.set()
                await asyncio.gather(task, return_exceptions=True)
    asyncio.run(scenario())


def test_changed_allocation_requires_new_baseline_even_at_same_charger(
        clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, facilities, _ = data
            before = await facilities.vehicle_parking("ev-idle")
            old_proof = deepcopy(store.record("ev-idle")["simulator_checkpoint"]["charging"])
            await occupy(facilities)
            after = await facilities.vehicle_parking("ev-idle")
            assert after["parking"]["charging_session_id"] != before["parking"]["charging_session_id"]
            assert {k: v for k, v in after["parking"].items() if k != "charging_session_id"} == {
                k: v for k, v in before["parking"].items() if k != "charging_session_id"}
            clock.advance(1)
            with pytest.raises(HTTPException, match="CHARGING_BASELINE_REQUIRED"):
                await store.simulator_recovery.checkpoint(
                    "ev-idle", checkpoint_request(store, soc=60.001))
            await store.simulator_recovery.checkpoint("ev-idle", checkpoint_request(store))
            new_proof = store.record("ev-idle")["simulator_checkpoint"]["charging"]
            assert new_proof["allocation_id"] != old_proof["allocation_id"]
            clock.advance(1)
            await store.simulator_recovery.checkpoint(
                "ev-idle", checkpoint_request(store, soc=60.001))
    asyncio.run(scenario())


def test_unrelated_facilities_revision_preserves_charging_baseline(
        clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, facilities, _ = data
            old_proof = deepcopy(store.record("ev-idle")["simulator_checkpoint"]["charging"])
            await occupy(facilities, "another-vehicle")
            clock.advance(1)
            await store.simulator_recovery.checkpoint(
                "ev-idle", checkpoint_request(store, soc=60.001))
            proof = store.record("ev-idle")["simulator_checkpoint"]["charging"]
            assert proof["allocation_id"] == old_proof["allocation_id"]
            assert proof["facilities_revision"] > old_proof["facilities_revision"]
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["nonce", "schema", "timeout"])
def test_invalid_or_unavailable_observation_cannot_publish(
        clock, monkeypatch, failure):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            get = client.get
            before = deepcopy(store.record("ev-idle"))

            async def invalid(*args, **kwargs):
                if failure == "timeout":
                    await asyncio.sleep(1)
                response = await get(*args, **kwargs)
                payload = response.json()
                if failure == "nonce":
                    payload["request_id"] = str(uuid4())
                else:
                    payload.pop("allocation_id")
                return httpx.Response(200, json=payload, request=response.request)

            store.simulator_recovery.evidence_timeout = .005
            monkeypatch.setattr(client, "get", invalid)
            with pytest.raises(HTTPException, match="EVIDENCE_UNAVAILABLE"):
                await asyncio.wait_for(store.simulator_recovery.checkpoint(
                    "ev-idle", checkpoint_request(store)), .5)
            assert store.record("ev-idle") == before
            assert store.ready
    asyncio.run(scenario())


def test_observation_is_uncached_and_refuses_uncommitted_allocations(
        clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, _, facilities, client = data
            request_id = str(uuid4())
            url = "/api/v1/vehicles/ev-idle/parking-observation"
            first = await client.get(url, params={"request_id": request_id})
            assert first.status_code == 200
            assert first.headers["Cache-Control"] == "no-store"
            assert first.json()["request_id"] == request_id
            async with facilities.lock:
                unavailable = await asyncio.wait_for(client.get(
                    url, params={"request_id": str(uuid4())}), .2)
                assert unavailable.status_code == 503
            second = await client.get(url, params={"request_id": str(uuid4())})
            assert second.json()["allocation_id"] == first.json()["allocation_id"]
            assert second.json()["request_id"] != request_id
            legacy = await client.get("/api/v1/vehicles/ev-idle/parking")
            assert legacy.json() == first.json()["parking"]
    asyncio.run(scenario())


def test_observation_client_is_reused_and_closed_with_fleet(clock, monkeypatch):
    async def scenario():
        async with charging_cluster(clock, monkeypatch) as data:
            _, store, _, client = data
            await store.simulator_recovery.checkpoint("ev-idle", checkpoint_request(store))
            await store.simulator_recovery.checkpoint("ev-idle", checkpoint_request(store))
            assert store.simulator_recovery.charging_client is client
            assert not client.is_closed
            await store.close()
            assert client.is_closed
            assert store.simulator_recovery.charging_client is None
    asyncio.run(scenario())
