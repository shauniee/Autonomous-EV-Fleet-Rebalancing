"""Durable checkpoint groups: receipts, read isolation and exact replay."""
import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.common.record_persistence import RecordSnapshotRepository
from ev_fleet.common.settings import SimulatorRecoverySettings
from ev_fleet.contracts.recovery import SimulatorCheckpoint, SimulatorCheckpointRequest, SimulatorSessionRequest
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.fleet.store import FleetStore


async def checkpoint_group(database_url, clock):
    bootstrap = SnapshotRepository("fleet", database_url)
    await bootstrap.start()
    await bootstrap.close()
    ids = ["group-"+str(i) for i in range(50)]
    namespace = "group-"+uuid4().hex
    repo = RecordSnapshotRepository(namespace, "vehicles", database_url)
    store = FleetStore(clock=clock, repository=repo,
        recovery_settings=SimulatorRecoverySettings(enabled=True, vehicle_ids=ids))
    await store.start()
    requests = []
    for vehicle_id in ids:
        await store.register(VehicleRegistration(vehicle_id=vehicle_id, battery_capacity_kwh=60, consumption_kwh_per_km=.18))
        lease = await store.simulator_recovery.session(vehicle_id, SimulatorSessionRequest(
            request_id=uuid4(), action="ACQUIRE", expected_fleet_instance_id=store.instance_id,
            expected_control_version=store.record(vehicle_id)["control_version"],
            expected_generation=0, expected_owner_revision=0, expected_checkpoint_revision=0))
        physical = TelemetryUpdate(session_id=lease.session_id, sequence=0, observed_at=clock.now(),
            position={"latitude": 1.35, "longitude": 103.85}, soc_pct=80,
            odometer_km=0, heading_deg=0, speed_kph=0)
        requests.append((vehicle_id, SimulatorCheckpointRequest(request_id=uuid4(),
            session_id=lease.session_id, generation=lease.generation, expected_checkpoint_revision=0,
            checkpoint=SimulatorCheckpoint(vehicle_id=vehicle_id, fleet_instance_id=store.instance_id, physical=physical),
            publish_telemetry=True)))
    return store, repo, requests


class PausedPool:
    def __init__(self, pool, lost=False):
        self.pool, self.lost = pool, lost
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.sample_groups = []

    @asynccontextmanager
    async def acquire(self):
        async with self.pool.acquire() as connection:
            owner = self
            class PausedConnection:
                async def execute(self, query, *args):
                    owner.entered.set()
                    await owner.release.wait()
                    result = await connection.execute(query, *args)
                    if owner.lost:
                        raise ConnectionResetError("group commit response lost")
                    return result
            yield PausedConnection()


async def renewal_bodies(store, bodies):
    await asyncio.gather(*(store.simulator_recovery.checkpoint(key, body) for key, body in bodies))
    requests = []
    for key, original in bodies:
        record = store.record(key)
        owner = record["simulator_owner"]
        requests.append((key, SimulatorSessionRequest(
            request_id=uuid4(), action="RENEW", expected_fleet_instance_id=store.instance_id,
            expected_control_version=record["control_version"],
            expected_generation=owner["generation"], expected_owner_revision=owner["revision"],
            expected_checkpoint_revision=1, session_id=original.session_id)))
    return requests


@pytest.mark.parametrize("operation", ["checkpoint", "session"])
@pytest.mark.parametrize("cancel_first", [False, True])
def test_group_receipts_wait_for_commit_and_cancelled_caller_replays(
        telemetry_database_url, clock, cancel_first, operation):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        if operation == "session":
            bodies = await renewal_bodies(store, bodies)
        submit = getattr(store.simulator_recovery, operation)
        pool = repo.pool
        paused = PausedPool(pool)
        repo.pool = paused
        version = store.version
        tasks = [asyncio.create_task(submit(key, body)) for key, body in bodies]
        try:
            await asyncio.wait_for(paused.entered.wait(), 5)
            assert not any(task.done() for task in tasks)
            before = await store.snapshot()
            assert all((v["telemetry"] is None) == (operation == "checkpoint") for v in before["vehicles"])
            if cancel_first:
                tasks[0].cancel()
                with pytest.raises(asyncio.CancelledError):
                    await tasks[0]
            paused.release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            successes = [r for r in results if not isinstance(r, BaseException)]
            errors = [i for i,r in enumerate(results) if isinstance(r, BaseException)]
            assert len(successes)==50-int(cancel_first)
            assert errors==([0] if cancel_first else [])
            if cancel_first:
                assert isinstance(results[0], asyncio.CancelledError)
            assert all(r.checkpoint_revision==1 for r in successes)
            repo.pool = pool
            replay = await submit(*bodies[0])
            assert replay.checkpoint_revision==1 and replay.durable
            if operation == "session":
                assert replay.owner_revision == 2
                assert replay == await submit(*bodies[0])
            assert store.version==version+50
            after = await store.snapshot()
            assert all(v["telemetry"]["soc_pct"]==80 for v in after["vehicles"])
            async with pool.acquire() as connection:
                count = await connection.fetchval("SELECT count(*) FROM telemetry_samples WHERE vehicle_id=ANY($1::text[]) AND session_id=ANY($2::uuid[])",
                    [key for key,_ in bodies], [body.session_id for _,body in bodies])
            assert count==50
        finally:
            paused.release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            repo.pool = pool
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["checkpoint", "session"])
def test_ambiguous_group_commit_fences_every_receipt_and_restores_all_rows(telemetry_database_url, clock, operation):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        if operation == "session":
            bodies = await renewal_bodies(store, bodies)
        submit = getattr(store.simulator_recovery, operation)
        pool = repo.pool
        paused = PausedPool(pool, lost=True)
        repo.pool = paused
        tasks = [asyncio.create_task(submit(key, body)) for key, body in bodies]
        try:
            await asyncio.wait_for(paused.entered.wait(), 5)
            paused.release.set()
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(r, HTTPException) and r.status_code==503 for r in results)
            assert not store.ready
            with pytest.raises(HTTPException):
                await store.snapshot()
            restored = RecordSnapshotRepository(repo.namespace, "vehicles", telemetry_database_url)
            await restored.start()
            try:
                records = (await restored.load())["vehicles"]
                assert len(records)==50
                assert all(record["telemetry"].soc_pct==80 for record in records.values())
                if operation == "session":
                    assert all(record["simulator_owner"]["revision"] == 2 for record in records.values())
            finally:
                await restored.close()
        finally:
            paused.release.set()
            repo.pool = pool
            await store.close()
    asyncio.run(scenario())



def test_invalid_group_item_does_not_poison_other_vehicle_checkpoints(telemetry_database_url, clock):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        key, body = bodies[0]
        bodies[0] = (key, body.model_copy(update={"expected_checkpoint_revision": 99}))
        version = store.version
        try:
            results = await asyncio.gather(*(store.simulator_recovery.checkpoint(k,b) for k,b in bodies), return_exceptions=True)
            assert isinstance(results[0], HTTPException) and results[0].status_code==409
            assert all(r.checkpoint_revision==1 for r in results[1:])
            assert store.ready and store.version==version+49
            assert (await store.get(key))["telemetry"] is None
            states = [await store.get(k) for k,_ in bodies[1:]]
            assert all(v["telemetry"]["soc_pct"]==80 for v in states)
        finally:
            await store.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("operation", ["checkpoint", "session"])
def test_checkpoint_group_rechecks_expired_ownership_after_queueing(telemetry_database_url, clock, operation):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        if operation == "session":
            bodies = await renewal_bodies(store, bodies)
        submit = getattr(store.simulator_recovery, operation)
        version = store.version
        await store.lock.acquire()
        tasks = [asyncio.create_task(submit(k,b)) for k,b in bodies]
        try:
            await asyncio.sleep(.01)
            clock.advance(31)
        finally:
            store.lock.release()
        try:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(r,HTTPException) and r.status_code==409 for r in results)
            assert store.ready and store.version==version
            assert all((v["telemetry"] is None) == (operation == "checkpoint")
                       for v in (await store.snapshot())["vehicles"])
        finally:
            await store.close()
    asyncio.run(scenario())


def test_cancelling_group_writer_fences_all_waiting_receipts(telemetry_database_url, clock):
    from contextlib import suppress

    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        pool = repo.pool
        paused = PausedPool(pool)
        repo.pool = paused
        tasks = [asyncio.create_task(store.simulator_recovery.checkpoint(k,b)) for k,b in bodies]
        try:
            await asyncio.wait_for(paused.entered.wait(), 5)
            writer = store.simulator_recovery.checkpoint_writer.task
            writer.cancel()
            with suppress(asyncio.CancelledError):
                await writer
            results = await asyncio.gather(*tasks, return_exceptions=True)
            assert all(isinstance(r,HTTPException) and r.status_code==503 for r in results)
            assert not store.ready
        finally:
            paused.release.set()
            repo.pool = pool
            with suppress(asyncio.CancelledError):
                await store.close()
    asyncio.run(scenario())



def test_same_vehicle_replay_conflict_and_next_checkpoint_keep_queue_order(telemetry_database_url, clock):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        key, first = bodies[0]
        conflict = first.model_copy(deep=True)
        conflict.checkpoint.physical.soc_pct = 79
        following = first.model_copy(deep=True, update={"request_id": uuid4(), "expected_checkpoint_revision": 1})
        following.checkpoint.physical.sequence = 1
        version = store.version
        try:
            results = await asyncio.gather(*(store.simulator_recovery.checkpoint(key,b)
                for b in [first, first, conflict, following]), return_exceptions=True)
            assert results[0] == results[1]
            assert results[0].checkpoint_revision==1
            assert isinstance(results[2], HTTPException) and results[2].status_code==409
            assert results[3].checkpoint_revision==2
            assert store.ready and store.version==version+2
            assert (await store.get(key))["telemetry"]["soc_pct"]==80
        finally:
            await store.close()
    asyncio.run(scenario())


def test_group_backlog_is_bounded_and_graceful_close_drains_accepted_work(telemetry_database_url, clock):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        pool = repo.pool
        paused = PausedPool(pool)
        repo.pool = paused
        first = asyncio.create_task(store.simulator_recovery.checkpoint(*bodies[0]))
        second = None
        closing = None
        try:
            await asyncio.wait_for(paused.entered.wait(), 5)
            writer = store.simulator_recovery.checkpoint_writer
            writer.max_pending = 1
            second = asyncio.create_task(store.simulator_recovery.checkpoint(*bodies[1]))
            await asyncio.sleep(0)
            with pytest.raises(HTTPException) as rejected:
                await store.simulator_recovery.checkpoint(*bodies[2])
            assert rejected.value.status_code==503
            assert rejected.value.detail=="SIMULATOR_CHECKPOINT_BACKLOG"
            closing = asyncio.create_task(store.simulator_recovery.close())
            await asyncio.sleep(0)
            assert not closing.done()
            paused.release.set()
            assert (await first).checkpoint_revision==1
            assert (await second).checkpoint_revision==1
            await closing
            assert store.ready
            with pytest.raises(HTTPException):
                await store.simulator_recovery.checkpoint(*bodies[3])
        finally:
            paused.release.set()
            await asyncio.gather(*(task for task in [first,second,closing] if task is not None), return_exceptions=True)
            repo.pool = pool
            await store.close()
    asyncio.run(scenario())



def test_fifty_lease_renewals_share_one_commit(telemetry_database_url, clock):
    async def scenario():
        store, repo, bodies = await checkpoint_group(telemetry_database_url, clock)
        bodies = await renewal_bodies(store, bodies)
        async with repo.pool.acquire() as connection:
            revision = await connection.fetchval(
                "SELECT revision FROM service_snapshots WHERE namespace=$1", repo.namespace)
        try:
            results = await asyncio.gather(*(store.simulator_recovery.session(key, body)
                                           for key, body in bodies))
            assert all(receipt.owner_revision == 2 and receipt.generation == 1 for receipt in results)
            async with repo.pool.acquire() as connection:
                after = await connection.fetchval(
                    "SELECT revision FROM service_snapshots WHERE namespace=$1", repo.namespace)
            assert after - revision == 1
            restored = (await repo.load())["vehicles"]
            assert len(restored) == 50
            assert all(record["simulator_owner"]["revision"] == 2 for record in restored.values())
        finally:
            await store.close()
    asyncio.run(scenario())



def test_same_vehicle_renewal_then_checkpoint_preserves_both_updates(telemetry_database_url, clock):
    async def scenario():
        store, repo, originals = await checkpoint_group(telemetry_database_url, clock)
        renewals = await renewal_bodies(store, originals)
        key, renewal = renewals[0]
        _, original = originals[0]
        checkpoint = original.model_copy(deep=True, update={
            "request_id": uuid4(), "expected_checkpoint_revision": 1})
        checkpoint.checkpoint.physical.sequence = 1
        version = store.version
        try:
            renewed, saved = await asyncio.gather(
                store.simulator_recovery.session(key, renewal),
                store.simulator_recovery.checkpoint(key, checkpoint))
            assert renewed.owner_revision == 2 and saved.checkpoint_revision == 2
            assert store.version == version + 2
            record = (await repo.load())["vehicles"][key]
            assert record["simulator_owner"]["revision"] == 2
            assert record["simulator_checkpoint"]["revision"] == 2
        finally:
            await store.close()
    asyncio.run(scenario())
