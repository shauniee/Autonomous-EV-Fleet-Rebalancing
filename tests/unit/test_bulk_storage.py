"""Bulk update cost must depend on changed records, with frozen read visibility."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.contracts.vehicles import VehicleRegistration
from ev_fleet.dispatch.service import DispatchService
from ev_fleet.fleet.store import FleetStore
from tests.unit.test_persistence import MemoryRepository


class UnchangedHistory(dict):
    def __deepcopy__(self, memo):
        raise AssertionError("An update copied unrelated committed history")


def test_checkpoint_commit_does_not_clone_unrelated_vehicle_history(clock):
    async def scenario():
        store = FleetStore(clock=clock, repository=MemoryRepository())
        await store.start()
        for vehicle_id in ("active", "idle"):
            await store.register(VehicleRegistration(
                vehicle_id=vehicle_id, battery_capacity_kwh=60,
                consumption_kwh_per_km=.18))
        store.record("idle")["claims"] = UnchangedHistory()
        staged = deepcopy(store.record("active"))
        staged["state_version"] += 1
        async with store.lock:
            await store.simulator_recovery.commit("active", staged)
        assert (await store.get("active"))["state_version"] == 2
        # A later in-place mutation must not escape through the frozen read view.
        async with store.lock:
            store.record("idle")["current_zone_id"] = "uncommitted"
            vehicles = (await store.snapshot())["vehicles"]
        assert next(v for v in vehicles if v["vehicle_id"] == "idle")["current_zone_id"] is None
        await store.close()
    asyncio.run(scenario())


def test_dispatch_progress_does_not_clone_unrelated_completed_history(clock):
    async def scenario():
        service = DispatchService(None, clock=clock, repository=MemoryRepository())
        await service.start()
        history = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        service.transition(history, "SUCCEEDED")
        active = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        await service.persist()
        history.events = UnchangedHistory()
        service.transition(active, "REJECTED", "fixture")
        await service.persist()
        assert service.persisted_snapshot["records"][active.data["dispatch_id"]]["data"]["state"] == "REJECTED"
        history.data["failure_reason"] = "uncommitted"
        assert service.persisted_snapshot["records"][history.data["dispatch_id"]]["data"]["failure_reason"] is None
        history.events = {}
        await service.close()
    asyncio.run(scenario())



def test_vehicle_read_uses_last_commit_without_waiting_for_an_unrelated_write(clock):
    from tests.unit.test_persistence import BlockingRepository

    async def scenario():
        repository = BlockingRepository()
        store = FleetStore(clock=clock, repository=repository)
        await store.start()
        await store.register(VehicleRegistration(vehicle_id="active", battery_capacity_kwh=60,
                                                 consumption_kwh_per_km=.18))
        repository.block_next_save = True
        writer = asyncio.create_task(store.session("active", uuid4()))
        await repository.save_started.wait()
        try:
            before = await asyncio.wait_for(store.get("active"), .1)
            assert before["state_version"] == 1
            assert before["telemetry"] is None
        finally:
            repository.release_save.set()
            await writer
        assert (await store.get("active"))["state_version"] == 2
        await store.close()
    asyncio.run(scenario())


class UnobservedHistory(dict):
    def __eq__(self, other):
        raise AssertionError("A vehicle update scanned unrelated journal history")

    def __ne__(self, other):
        return not self == other


def test_observed_progress_does_not_scan_or_commit_unrelated_pending_work(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        history = await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4()))
        active = await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4()))
        await service.persist()
        history.events = UnobservedHistory()
        pending = await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4()))
        active.data["distance_travelled_km"] = 1
        await service.persist_read([active])
        assert repository.payload["records"][active.data["dispatch_id"]]["data"]["distance_travelled_km"] == 1
        assert pending.data["dispatch_id"] not in repository.payload["records"]
        assert str(pending.data["request_id"]) not in repository.payload["requests"]
        history.events = {}
        await service.persist()
        assert pending.data["dispatch_id"] in repository.payload["records"]
        await service.close()
    asyncio.run(scenario())


def test_observed_update_with_new_planning_epoch_commits_coherent_global_state(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        active = await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4()))
        other = await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4()))
        await service.persist()
        service.transition(other, "UNKNOWN", "OWNERSHIP_UNCERTAIN")
        active.data["distance_travelled_km"] = 1
        await service.persist_read([active])
        assert repository.payload["planning_revision"] == service.planning_revision
        assert repository.payload["records"][other.data["dispatch_id"]]["data"]["state"] == "UNKNOWN"
        await service.close()
    asyncio.run(scenario())



@pytest.mark.parametrize("endpoint", ["acknowledge", "event"])
def test_duplicate_execution_receipt_waits_for_the_original_commit(clock, endpoint):
    from ev_fleet.contracts.dispatch import CommandAcknowledgement, DispatchEvent
    from tests.dispatch_support import Cluster
    from tests.unit.test_persistence import BlockingRepository

    async def scenario():
        async with Cluster(clock) as cluster:
            repository = BlockingRepository()
            cluster.dispatch.repository = repository
            await cluster.dispatch.persist()
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            command = (await cluster.dispatch.commands("ev-1"))["commands"][0]
            body = CommandAcknowledgement(command_id=command["command_id"], vehicle_id="ev-1", accepted=True)
            if endpoint=="event":
                await cluster.dispatch.acknowledge(dispatch["dispatch_id"], body)
                body = DispatchEvent(command_id=command["command_id"], vehicle_id="ev-1", sequence=0,
                    observed_at=clock.now(), kind="PROGRESS", distance_travelled_km=1)
            repository.block_next_save = True
            operation = getattr(cluster.dispatch, endpoint)
            first = asyncio.create_task(operation(dispatch["dispatch_id"], body))
            await repository.save_started.wait()
            duplicate = asyncio.create_task(operation(dispatch["dispatch_id"], body))
            try:
                await asyncio.sleep(.02)
                assert not duplicate.done(), "A duplicate response escaped before durability"
            finally:
                repository.release_save.set()
                await asyncio.gather(first,duplicate)
            saved = repository.payload["records"][dispatch["dispatch_id"]]
            assert saved["ack"]["accepted"]
            if endpoint=="event":
                assert saved["events"][0]["distance_travelled_km"]==1
    asyncio.run(scenario())



def test_shared_observation_commit_failure_fences_every_waiter(clock):
    from tests.unit.test_persistence import BlockingFailRepository

    async def scenario():
        repository = BlockingFailRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        records = [await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4())) for _ in range(2)]
        await service.persist()
        baseline = service.persisted_snapshot
        for record in records:
            record.data["distance_travelled_km"] = 1
        repository.fail_next_save = True
        waiters = [asyncio.create_task(service.persist_read([record])) for record in records]
        await repository.save_started.wait()
        assert all(not waiter.done() for waiter in waiters)
        repository.release_save.set()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(result, RuntimeError) for result in results)
        assert service.persisted_snapshot is baseline
        assert all(row["data"]["distance_travelled_km"] == 0
                   for row in repository.payload["records"].values())
        await service.close()
    asyncio.run(scenario())
