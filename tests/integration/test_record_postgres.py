"""Partitioned storage contracts, only against a disposable loopback database."""
import asyncio
from copy import deepcopy
from uuid import uuid4

import pytest

from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.common import utcnow


def test_legacy_promotion_copies_every_record_and_round_trips_deletions(telemetry_database_url):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        namespace = "migration-" + uuid4().hex
        old = SnapshotRepository(namespace, telemetry_database_url)
        await old.start()
        original = {"version": 1, "records": {str(i): {"value": i} for i in range(50)}}
        await old.save(original)
        await old.close()
        repo = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
        await repo.start()
        assert await repo.load() == original
        updated = deepcopy(original)
        updated["version"] = 2
        updated["records"]["0"]["value"] = 900
        await repo.save(updated, changed_keys={"0"})
        await repo.close()
        restored = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
        await restored.start()
        assert await restored.load() == updated
        # Unchanged records are not encoded or written again.
        another = deepcopy(updated)
        another["version"] = 3
        another["records"]["0"]["value"] = 901
        del another["records"]["1"]
        another["records"]["2"] = object()
        await restored.save(another, changed_keys={"0"})
        expected = deepcopy(updated)
        expected["version"] = 3
        expected["records"]["0"]["value"] = 901
        del expected["records"]["1"]
        assert await restored.load() == expected
        assert list((await restored.load())["records"]) == list(expected["records"])
        await restored.close()
    asyncio.run(scenario())


def test_failed_record_commit_rolls_back_metadata_records_and_history(telemetry_database_url):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        bootstrap = SnapshotRepository("fleet", telemetry_database_url)
        await bootstrap.start()
        await bootstrap.close()
        namespace = "atomic-" + uuid4().hex
        repo = RecordSnapshotRepository(namespace, "vehicles", telemetry_database_url)
        await repo.start()
        original = await repo.load() or {"version": 1, "vehicles": {}}
        await repo.save(original)
        async with repo.pool.acquire() as connection:
            await connection.execute("""
                CREATE FUNCTION reject_record_commit() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF NEW.record_key = 'fail-commit' THEN RAISE EXCEPTION 'fixture commit rejected'; END IF;
                    RETURN NEW;
                END $$;
                CREATE CONSTRAINT TRIGGER reject_record_commit_trigger
                AFTER INSERT OR UPDATE ON snapshot_records DEFERRABLE INITIALLY DEFERRED
                FOR EACH ROW EXECUTE FUNCTION reject_record_commit();
            """)
        updated = deepcopy(original)
        updated["version"] += 1
        updated["vehicles"]["fail-commit"] = {"n": 2}
        sample = {"vehicle_id": "atomic-" + uuid4().hex, "session_id": uuid4(),
                  "sequence": 1, "observed_at": utcnow(), "received_at": utcnow(), "telemetry": {"soc_pct": 70}}
        try:
            with pytest.raises(Exception, match="fixture commit rejected"):
                await repo.save(updated, changed_keys={"fail-commit"}, telemetry_sample=sample)
            assert not repo.ready
            read = RecordSnapshotRepository(namespace, "vehicles", telemetry_database_url)
            await read.start()
            try:
                assert await read.load() == original
                async with read.pool.acquire() as connection:
                    count = await connection.fetchval("SELECT count(*) FROM telemetry_samples WHERE vehicle_id=$1", sample["vehicle_id"])
                assert count == 0
            finally:
                await read.close()
        finally:
            async with repo.pool.acquire() as connection:
                await connection.execute("DROP TRIGGER reject_record_commit_trigger ON snapshot_records; DROP FUNCTION reject_record_commit()")
            await repo.close()
    asyncio.run(scenario())


def test_partitioned_record_keys_are_isolated_between_namespaces(telemetry_database_url):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        repos = [RecordSnapshotRepository("isolation-" + uuid4().hex, "records", telemetry_database_url) for _ in range(2)]
        try:
            for index, repo in enumerate(repos):
                await repo.start()
                await repo.save({"records": {"same": {"value": index}}})
            assert [await repo.load() for repo in repos] == [
                {"records": {"same": {"value": 0}}}, {"records": {"same": {"value": 1}}}]
        finally:
            for repo in repos:
                await repo.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["missing", "extra", "version"])
def test_restore_rejects_an_inconsistent_record_manifest(telemetry_database_url, corruption):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        namespace = "corrupt-" + uuid4().hex
        repo = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
        await repo.start()
        try:
            await repo.save({"records": {"one": {"value": 1}}})
            async with repo.pool.acquire() as connection:
                if corruption == "missing":
                    await connection.execute("DELETE FROM snapshot_records WHERE namespace=$1", namespace)
                elif corruption == "extra":
                    await connection.execute("INSERT INTO snapshot_records VALUES ($1,'extra','{}'::jsonb)", namespace)
                else:
                    await connection.execute("UPDATE service_snapshots SET payload=jsonb_set(payload,'{__partitioned_snapshot__}','2') WHERE namespace=$1", namespace)
            with pytest.raises(RuntimeError, match="SNAPSHOT_"):
                await repo.load()
        finally:
            await repo.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["lost_response", "cancel_after_commit"])
def test_ambiguous_committed_write_is_fenced_and_restored_from_the_database(
        telemetry_database_url, failure):
    from contextlib import asynccontextmanager
    from ev_fleet.common.record_persistence import RecordSnapshotRepository

    async def scenario():
        namespace = "ambiguous-" + uuid4().hex
        repo = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
        await repo.start()
        await repo.save({"version": 1, "records": {"one": {"value": 1}}})
        pool = repo.pool
        committed = asyncio.Event()

        class LostResultConnection:
            def __init__(self, connection):
                self.connection = connection

            async def execute(self, *args):
                await self.connection.execute(*args)
                committed.set()
                if failure == "cancel_after_commit":
                    await asyncio.Event().wait()
                raise ConnectionResetError("response lost after server commit")

        class LostResultPool:
            @asynccontextmanager
            async def acquire(self):
                async with pool.acquire() as connection:
                    yield LostResultConnection(connection)

        repo.pool = LostResultPool()
        updated = {"version": 2, "records": {"one": {"value": 2}}}
        try:
            writer = asyncio.create_task(repo.save(updated, changed_keys={"one"}))
            await committed.wait()
            if failure == "cancel_after_commit":
                writer.cancel()
            with pytest.raises(asyncio.CancelledError if failure == "cancel_after_commit" else ConnectionResetError):
                await writer
            assert not repo.ready
            with pytest.raises(RuntimeError, match="PERSISTENCE_STATE_UNCERTAIN"):
                await repo.save({"version": 3, "records": {}})
            restored = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
            await restored.start()
            try:
                assert await restored.load() == updated
            finally:
                await restored.close()
        finally:
            repo.pool = pool
            await repo.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("service", ["fleet", "dispatch"])
def test_service_start_promotes_before_ready_and_fences_an_old_reader(telemetry_database_url, service):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository
    from ev_fleet.dispatch.service import DispatchService
    from ev_fleet.fleet.store import FleetStore

    async def scenario():
        namespace = "old-reader-" + uuid4().hex
        def store(repository):
            return (FleetStore(repository=repository) if service=="fleet"
                    else DispatchService(None, repository=repository))
        legacy = store(SnapshotRepository(namespace, telemetry_database_url))
        await legacy.start()
        await legacy.close()
        repo = RecordSnapshotRepository(namespace, "vehicles" if service=="fleet" else "records", telemetry_database_url)
        modern = store(repo)
        await modern.start()
        old = store(SnapshotRepository(namespace, telemetry_database_url))
        try:
            with pytest.raises(KeyError):
                await old.start()
        finally:
            # Failed startup must not invoke an old owner's shutdown save.
            await old.repository.close()
            await modern.close()
    asyncio.run(scenario())



def test_fifty_dispatch_observations_share_one_durable_frame(telemetry_database_url):
    from ev_fleet.common.record_persistence import RecordSnapshotRepository
    from ev_fleet.contracts.dispatch import DispatchApproval
    from ev_fleet.dispatch.service import DispatchService

    async def scenario():
        namespace = "observations-"+uuid4().hex
        repo = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
        service = DispatchService(None, repository=repo)
        await service.start()
        records = [await service.store.begin(DispatchApproval(request_id=uuid4(), recommendation_id=uuid4())) for _ in range(50)]
        await service.persist()
        async with repo.pool.acquire() as connection:
            revision = await connection.fetchval("SELECT revision FROM service_snapshots WHERE namespace=$1", namespace)
        try:
            for record in records:
                record.data["distance_travelled_km"] = 1
            await asyncio.gather(*(service.persist_read([record]) for record in records))
            async with repo.pool.acquire() as connection:
                after = await connection.fetchval("SELECT revision FROM service_snapshots WHERE namespace=$1", namespace)
            assert after-revision==1
            snapshot = await repo.load()
            assert len(snapshot["records"])==50
            assert all(r["data"]["distance_travelled_km"]==1 for r in snapshot["records"].values())
        finally:
            await service.close()
    asyncio.run(scenario())
