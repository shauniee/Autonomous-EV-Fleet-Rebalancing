"""Polling latency and durable command delivery, without live services."""
import asyncio
from contextlib import asynccontextmanager
from threading import Event
from uuid import uuid4

import pytest

from ev_fleet.common import persistence
from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.dispatch.service import DispatchService
from tests.dispatch_support import Cluster
from tests.unit.test_persistence import (
    BlockingFailRepository, BlockingRepository, MemoryRepository,
)


@pytest.mark.parametrize("read", ["commands", "get"])
def test_unchanged_read_does_not_scan_or_save_unrelated_history(clock, monkeypatch, read):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        record = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        record.data.update(vehicle_id="terminal", state="SUCCEEDED")
        await service.persist()
        saves = repository.save_count

        def unexpected_snapshot():
            raise AssertionError("Unchanged poll scanned the global journal")

        monkeypatch.setattr(service, "current_snapshot", unexpected_snapshot)
        if read == "commands":
            assert await service.commands("idle-vehicle") == {"commands": []}
        else:
            assert (await service.get(record.data["dispatch_id"]))["state"] == "SUCCEEDED"
        assert repository.save_count == saves
    asyncio.run(scenario())


def test_idle_poll_does_not_commit_another_requests_changes(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        record = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        await service.persist()
        saves = repository.save_count
        service.transition(record, "UNKNOWN", "UNRELATED_OWNER_WILL_PERSIST")
        assert await service.commands("idle-vehicle") == {"commands": []}
        assert repository.save_count == saves
        assert repository.payload["records"][record.data["dispatch_id"]]["data"]["state"] == "PENDING"
        await service.persist()
        assert repository.payload["records"][record.data["dispatch_id"]]["data"]["state"] == "UNKNOWN"
    asyncio.run(scenario())


def test_idle_poll_still_waits_for_an_uncertain_commit(clock):
    async def scenario():
        repository = BlockingFailRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        repository.fail_next_save = True
        save = asyncio.create_task(service.persist())
        await repository.save_started.wait()
        poll = asyncio.create_task(service.commands("idle-vehicle"))
        await asyncio.sleep(0)
        assert not poll.done()
        repository.release_save.set()
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await save
        with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
            await poll
    asyncio.run(scenario())


def test_command_delivery_waits_until_sent_receipt_is_durable(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            repository = BlockingRepository()
            cluster.dispatch.repository = repository
            await cluster.dispatch.persist()
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            repository.block_next_save = True
            delivery = asyncio.create_task(cluster.dispatch.commands("ev-1"))
            await repository.save_started.wait()
            assert not delivery.done()
            repository.release_save.set()
            commands = await delivery
            saved = repository.payload["records"][dispatch["dispatch_id"]]["data"]
            assert saved["state"] == "SENT"
            assert saved["delivery_attempts"] == 1
            assert commands["commands"][0]["command_id"] == saved["command"]["command_id"]
    asyncio.run(scenario())


class SuccessfulConnection:
    @asynccontextmanager
    async def transaction(self):
        yield self

    async def execute(self, *_args):
        return None


class SuccessfulPool:
    @asynccontextmanager
    async def acquire(self):
        yield SuccessfulConnection()


def test_dispatch_encoding_does_not_block_command_request_loop(monkeypatch):
    started, release, finished = Event(), Event(), Event()
    original = persistence.encode_state

    def slow_encode(payload):
        started.set()
        release.wait(timeout=.3)
        result = original(payload)
        finished.set()
        return result

    monkeypatch.setattr(persistence, "encode_state", slow_encode)

    async def scenario():
        repo = persistence.SnapshotRepository("dispatch", url="postgresql://fixture")
        repo.pool = SuccessfulPool()
        save = asyncio.create_task(repo.save({"snapshot": 1}))
        try:
            while not started.is_set():
                await asyncio.sleep(0)
            assert not finished.is_set(), "Snapshot encoding blocked the request event loop"
        finally:
            release.set()
            await save
    asyncio.run(scenario())


def test_read_persists_its_own_reconciliation_change_even_if_refresh_is_unchanged(clock):
    async def scenario():
        repository = MemoryRepository()
        service = DispatchService(None, clock=clock, repository=repository)
        await service.start()
        record = await service.store.begin(DispatchApproval(
            request_id=uuid4(), recommendation_id=uuid4()))
        record.data.update(vehicle_id="terminal", state="REJECTED")
        await service.persist()
        # A reconciliation cycle may update this record before processing
        # another one. A read must still make this observed state durable.
        service.transition(record, "SUCCEEDED")
        result = await service.get(record.data["dispatch_id"])
        assert result["state"] == "SUCCEEDED"
        assert repository.payload["records"][record.data["dispatch_id"]]["data"]["state"] == "SUCCEEDED"
    asyncio.run(scenario())


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("read", ["get", "reconcile"])
def test_unchanged_active_or_paused_observation_does_not_rewrite_journal(clock, stale, read):
    from ev_fleet.contracts.dispatch import CommandAcknowledgement

    async def scenario():
        async with Cluster(clock) as cluster:
            repository = MemoryRepository()
            cluster.dispatch.repository = repository
            await cluster.dispatch.persist()
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            command = (await cluster.dispatch.commands("ev-1"))["commands"][0]
            await cluster.dispatch.acknowledge(dispatch["dispatch_id"], CommandAcknowledgement(
                command_id=command["command_id"], vehicle_id="ev-1", accepted=True))
            await cluster.telemetry(state="RELOCATING", dispatch_id=dispatch["dispatch_id"])
            if stale:
                clock.advance(31)
            before = await cluster.dispatch.get(dispatch["dispatch_id"])
            saves = repository.save_count
            clock.advance(1)
            if read == "get":
                after = await cluster.dispatch.get(dispatch["dispatch_id"])
            else:
                await cluster.dispatch.reconcile_once()
                after = cluster.dispatch.store.get(dispatch["dispatch_id"]).view()
            assert after["updated_at"] == before["updated_at"]
            assert repository.save_count == saves
    asyncio.run(scenario())


def test_fresh_progress_updates_timestamp_even_when_journey_state_is_unchanged(clock):
    from ev_fleet.contracts.dispatch import CommandAcknowledgement, DispatchEvent

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            command = (await cluster.dispatch.commands("ev-1"))["commands"][0]
            await cluster.dispatch.acknowledge(dispatch["dispatch_id"], CommandAcknowledgement(
                command_id=command["command_id"], vehicle_id="ev-1", accepted=True))
            await cluster.telemetry(state="RELOCATING", dispatch_id=dispatch["dispatch_id"])
            before = await cluster.dispatch.get(dispatch["dispatch_id"])
            clock.advance(1)
            await cluster.telemetry(state="RELOCATING", dispatch_id=dispatch["dispatch_id"])
            refreshed = await cluster.dispatch.get(dispatch["dispatch_id"])
            assert refreshed["updated_at"] > before["updated_at"]
            clock.advance(1)
            progressed = await cluster.dispatch.event(dispatch["dispatch_id"], DispatchEvent(
                command_id=command["command_id"], vehicle_id="ev-1", sequence=0,
                observed_at=clock.now(), kind="PROGRESS", distance_travelled_km=1))
            assert progressed["updated_at"] > refreshed["updated_at"]
    asyncio.run(scenario())
