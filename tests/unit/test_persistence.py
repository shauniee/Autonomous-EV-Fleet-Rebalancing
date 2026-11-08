import asyncio
import json
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException

from ev_fleet.common.persistence import decode_state, encode_state
from ev_fleet.common.settings import DispatchSettings, SimulatorRecoverySettings
from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.contracts.recovery import SimulatorSessionRequest
from ev_fleet.contracts.vehicles import Position, VehicleRegistration
from ev_fleet.dispatch.service import DispatchService
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.fleet.store import FleetStore


class MemoryRepository:
    enabled = True
    ready = True

    def __init__(self):
        self.payload = None
        self.save_count = 0

    async def start(self):
        return None

    async def load(self):
        return deepcopy(self.payload)

    async def save(self, payload):
        # Exercise the exact tagged JSON round trip used by PostgreSQL JSONB.
        encoded = json.loads(json.dumps(encode_state(payload), allow_nan=False))
        self.payload = decode_state(encoded)
        self.save_count += 1

    async def close(self):
        return None

    def status(self):
        return "memory-test"


class BlockingFailRepository(MemoryRepository):
    def __init__(self):
        super().__init__()
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()
        self.fail_next_save = False
        self.ready = True

    async def save(self, payload):
        if self.fail_next_save:
            self.fail_next_save = False
            self.save_started.set()
            await self.release_save.wait()
            self.ready = False
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        await super().save(payload)


class BlockingRepository(MemoryRepository):
    def __init__(self):
        super().__init__()
        self.block_next_save = False
        self.save_started = asyncio.Event()
        self.release_save = asyncio.Event()

    async def save(self, payload):
        if self.block_next_save:
            self.block_next_save = False
            self.save_started.set()
            await self.release_save.wait()
        await super().save(payload)


def test_tagged_json_round_trip_preserves_models_keys_sets_and_time():
    identifier = uuid4()
    observed = datetime.now(timezone.utc)
    value = {
        4: Position(latitude=1.35, longitude=103.85),
        "ids": {identifier},
        "observed": observed,
    }
    restored = decode_state(json.loads(json.dumps(encode_state(value))))
    assert restored[4] == value[4]
    assert restored["ids"] == {identifier}
    assert restored["observed"] == observed


def test_fleet_snapshot_remains_available_during_durable_write(clock):
    async def scenario():
        repository = BlockingRepository()
        store = FleetStore(clock=clock, repository=repository)
        await store.start()

        repository.block_next_save = True
        registration = asyncio.create_task(store.register(VehicleRegistration(
            vehicle_id="ev-writing", battery_capacity_kwh=60,
            consumption_kwh_per_km=.18)))
        await asyncio.wait_for(repository.save_started.wait(), .5)

        while_writing = await asyncio.wait_for(store.snapshot(), .1)
        assert while_writing["vehicles"] == []

        repository.release_save.set()
        await registration
        committed = await store.snapshot()
        assert [vehicle["vehicle_id"] for vehicle in committed["vehicles"]] == [
            "ev-writing"
        ]

    asyncio.run(scenario())


def test_recovery_commit_refreshes_fleet_read_model(clock):
    async def scenario():
        repository = MemoryRepository()
        recovery = SimulatorRecoverySettings(
            enabled=True, vehicle_ids=["ev-recovery"]
        )
        store = FleetStore(
            settings=DispatchSettings(), clock=clock,
            repository=repository, recovery_settings=recovery,
        )
        await store.start()
        await store.register(VehicleRegistration(
            vehicle_id="ev-recovery", battery_capacity_kwh=60,
            consumption_kwh_per_km=.18,
        ))
        record = store.record("ev-recovery")
        lease = await store.simulator_recovery.session(
            "ev-recovery",
            SimulatorSessionRequest(
                request_id=uuid4(), action="ACQUIRE",
                expected_fleet_instance_id=store.instance_id,
                expected_control_version=record["control_version"],
                expected_generation=0, expected_owner_revision=0,
                expected_checkpoint_revision=0,
            ),
        )

        await store.lock.acquire()
        try:
            snapshot = await store.snapshot()
        finally:
            store.lock.release()

        vehicle = snapshot["vehicles"][0]
        assert vehicle["simulator_recovery"]["generation"] == lease.generation
        assert vehicle["simulator_recovery"]["status"] == "ACTIVE"

    asyncio.run(scenario())


def test_legacy_fleet_snapshot_without_charging_task_allows_simulator_session(clock):
    async def scenario():
        repository = MemoryRepository()
        recovery = SimulatorRecoverySettings(enabled=True, vehicle_ids=["ev-legacy"])
        original = FleetStore(settings=DispatchSettings(), clock=clock,
                              repository=repository, recovery_settings=recovery)
        await original.start()
        await original.register(VehicleRegistration(
            vehicle_id="ev-legacy", battery_capacity_kwh=60,
            consumption_kwh_per_km=.18))
        await original.close()

        repository.payload["vehicles"]["ev-legacy"].pop("active_charging_task")
        restored = FleetStore(settings=DispatchSettings(), clock=clock,
                              repository=repository, recovery_settings=recovery)
        await restored.start()
        record = restored.record("ev-legacy")
        receipt = await restored.simulator_recovery.session(
            "ev-legacy", SimulatorSessionRequest(
                request_id=uuid4(), action="ACQUIRE",
                expected_fleet_instance_id=restored.instance_id,
                expected_control_version=record["control_version"],
                expected_generation=0, expected_owner_revision=0,
                expected_checkpoint_revision=0))

        assert receipt.generation == 1
        assert restored.record("ev-legacy")["active_charging_task"] is None
        await restored.close()

    asyncio.run(scenario())


def test_facilities_state_survives_store_recreation():
    async def scenario():
        repository = MemoryRepository()
        site = BY_ID["mock-cbd-01"]
        request = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-durable",
            expected_zone_id=site.zone_id, expected_position=site.position)

        original = FacilitiesStore(repository=repository)
        await original.start()
        receipt = await original.reserve(site.facility_id, request)
        await original.change(receipt["dispatch_id"], "OCCUPIED")

        restored = FacilitiesStore(repository=repository)
        await restored.start()
        assert restored.instance_id == original.instance_id
        assert await restored.get_hold(receipt["dispatch_id"]) == {
            **receipt, "status": "OCCUPIED"}
        assert (await restored.get(site.facility_id)).parking_available == site.parking_available - 1

    asyncio.run(scenario())


def test_legacy_occupied_parking_access_repair_survives_store_recreation():
    async def scenario():
        repository = MemoryRepository()
        site = BY_ID["mock-punggol-01"]
        centre = site.position.model_dump()
        road_access = Position(
            latitude=centre["latitude"] + .00055,
            longitude=centre["longitude"],
        )
        request = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-legacy-access",
            expected_zone_id=site.zone_id, expected_position=site.position)

        original = FacilitiesStore(repository=repository)
        await original.start()
        receipt = await original.reserve(site.facility_id, request)
        await original.change(receipt["dispatch_id"], "OCCUPIED")
        legacy = await original.vehicle_parking(request.vehicle_id)
        assert legacy["access_position_missing"] is True
        assert legacy["parking"]["position"] == site.position

        repaired = await original.repair_vehicle_parking_access(request.vehicle_id, road_access)
        assert repaired["access_position_missing"] is False
        assert repaired["parking"]["position"] == road_access

        restored = FacilitiesStore(repository=repository)
        await restored.start()
        persisted = await restored.vehicle_parking(request.vehicle_id)
        assert persisted["access_position_missing"] is False
        assert persisted["parking"]["position"] == road_access

    asyncio.run(scenario())


def test_parking_access_repair_rejects_unsafe_nonoccupied_or_rewritten_holds():
    async def scenario():
        store = FacilitiesStore(repository=MemoryRepository())
        await store.start()
        site = BY_ID["mock-punggol-01"]
        centre = site.position.model_dump()
        safe_access = Position(
            latitude=centre["latitude"] + .00055,
            longitude=centre["longitude"],
        )
        far_access = Position(
            latitude=centre["latitude"] + .003,
            longitude=centre["longitude"],
        )

        pending_request = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-pending-access",
            expected_zone_id=site.zone_id, expected_position=site.position)
        await store.reserve(site.facility_id, pending_request)
        with pytest.raises(HTTPException) as pending_error:
            await store.repair_vehicle_parking_access(pending_request.vehicle_id, safe_access)
        assert pending_error.value.detail == "PARKING_NOT_OCCUPIED"

        legacy_request = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-unsafe-access",
            expected_zone_id=site.zone_id, expected_position=site.position)
        await store.reserve(site.facility_id, legacy_request)
        await store.change(legacy_request.dispatch_id, "OCCUPIED")
        with pytest.raises(HTTPException) as distance_error:
            await store.repair_vehicle_parking_access(legacy_request.vehicle_id, far_access)
        assert distance_error.value.detail == "PARKING_ROAD_ACCESS_TOO_FAR"

        await store.repair_vehicle_parking_access(legacy_request.vehicle_id, safe_access)
        same = await store.repair_vehicle_parking_access(legacy_request.vehicle_id, safe_access)
        assert same["parking"]["position"] == safe_access
        with pytest.raises(HTTPException) as rewrite_error:
            await store.repair_vehicle_parking_access(
                legacy_request.vehicle_id,
                Position(latitude=centre["latitude"] + .00060,
                         longitude=centre["longitude"]),
            )
        assert rewrite_error.value.detail == "PARKING_ACCESS_ALREADY_CONFIRMED"

    asyncio.run(scenario())


def test_dispatch_journal_and_idempotency_indexes_survive_recreation(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        record.data.update(vehicle_id="ev-durable", state="SUCCEEDED")
        await service.persist()

        restored = DispatchService(None, clock=clock, repository=repository)
        await restored.start()
        same = await restored.store.begin(approval)
        assert same.data["dispatch_id"] == record.data["dispatch_id"]
        assert same.data["vehicle_id"] == "ev-durable"
        assert same.data["state"] == "SUCCEEDED"

    asyncio.run(scenario())


def test_unchanged_dispatch_reads_do_not_rewrite_snapshot(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        record.data.update(vehicle_id="ev-durable", state="SUCCEEDED")
        await service.persist()
        saves_before_reads = repository.save_count

        assert (await service.get(record.data["dispatch_id"]))["state"] == "SUCCEEDED"
        assert await service.commands("ev-unused") == {"commands": []}
        assert repository.save_count == saves_before_reads

    asyncio.run(scenario())


def test_dispatch_read_persists_a_reconciliation_change(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        await service.persist()
        saves_before_read = repository.save_count

        async def change_state(current):
            service.transition(current, "UNKNOWN", "TEST_RECONCILIATION_CHANGE")

        service.refresh = change_state
        result = await service.get(record.data["dispatch_id"])

        assert result["state"] == "UNKNOWN"
        assert repository.save_count == saves_before_read + 1
        assert repository.payload["records"][record.data["dispatch_id"]]["data"]["state"] == "UNKNOWN"

    asyncio.run(scenario())


def test_reconciliation_batches_changed_records_and_skips_unchanged_cycles(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        records = []
        for index in range(2):
            approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
            record = await service.store.begin(approval)
            record.data["vehicle_id"] = f"ev-{index}"
            records.append(record)
        await service.persist()
        saves_before_cycles = repository.save_count

        async def no_change(_record):
            return None

        service.refresh = no_change
        await service.reconcile_once()
        assert repository.save_count == saves_before_cycles

        async def change_state(record):
            service.transition(record, "UNKNOWN", "TEST_RECONCILIATION_CHANGE")

        service.refresh = change_state
        await service.reconcile_once()
        assert repository.save_count == saves_before_cycles + 1
        assert all(item.data["state"] == "UNKNOWN" for item in records)

    asyncio.run(scenario())


def test_reconciliation_loop_refreshes_each_record_once_per_interval(clock, monkeypatch):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        record = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        await service.persist()
        refreshes = 0

        async def count_refresh(current):
            nonlocal refreshes
            assert current is record
            refreshes += 1

        async def stop_after_cycle(_seconds):
            raise StopAsyncIteration

        service.refresh = count_refresh
        monkeypatch.setattr("ev_fleet.dispatch.service.asyncio.sleep", stop_after_cycle)

        with pytest.raises(StopAsyncIteration):
            await service.reconcile_loop()
        assert refreshes == 1

    asyncio.run(scenario())


def test_unchanged_concurrent_read_waits_for_failed_persistence(clock):
    async def scenario():
        repository = BlockingFailRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        await service.persist()
        refresh_calls = 0

        async def change_once(current):
            nonlocal refresh_calls
            refresh_calls += 1
            if refresh_calls == 1:
                service.transition(current, "SUCCEEDED")

        service.refresh = change_once
        repository.fail_next_save = True
        changing_read = asyncio.create_task(service.get(record.data["dispatch_id"]))
        await repository.save_started.wait()
        unchanged_read = asyncio.create_task(service.get(record.data["dispatch_id"]))
        await asyncio.sleep(0)

        assert not unchanged_read.done()
        assert repository.payload["records"][record.data["dispatch_id"]]["data"]["state"] == "PENDING"

        repository.release_save.set()
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await changing_read
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await unchanged_read

    asyncio.run(scenario())


def test_queued_unchanged_reads_do_not_repeat_a_completed_save(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        approval = DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())
        record = await service.store.begin(approval)
        await service.persist()

        async def no_change(_record):
            return None

        service.refresh = no_change
        await record.lock.acquire()
        reads = [asyncio.create_task(service.get(record.data["dispatch_id"])) for _ in range(3)]
        await asyncio.sleep(0)
        service.transition(record, "SUCCEEDED")
        await service.persist()
        saves_after_change = repository.save_count
        record.lock.release()

        results = await asyncio.gather(*reads)
        assert all(result["state"] == "SUCCEEDED" for result in results)
        assert repository.save_count == saves_after_change

    asyncio.run(scenario())
