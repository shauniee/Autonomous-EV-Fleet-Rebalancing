"""Durable queued intent and exact child IDs survive an actual PostgreSQL reload."""
import asyncio
from uuid import uuid4

from ev_fleet.common.record_persistence import RecordSnapshotRepository
from ev_fleet.dispatch.service import DispatchService
from tests.dispatch_support import Cluster
from tests.unit.test_paced_batch_queue import queued


def test_paced_authorization_reload_admits_once_after_original_ttl(telemetry_database_url, clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            old = cluster.dispatch
            namespace = "paced-queue-" + uuid4().hex
            repository = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
            await repository.start()
            old.repository = repository
            await old.persist()
            body, job = await queued(cluster, 2)
            original_ids = [item["dispatch_id"] for item in job["items"]]
            await old.close()  # Retire the old writer before starting its replacement.
            restored = DispatchService(cluster.client, old.settings, clock,
                RecordSnapshotRepository(namespace, "records", telemetry_database_url))
            await restored.start()
            cluster.dispatch = restored
            cluster.apps["dispatch.test"].state.service = restored
            cluster.apps["dispatch.test"].state.persistence = restored.repository
            try:
                clock.advance(11)
                for item in job["items"]:
                    await cluster.telemetry(item["vehicle_id"])
                await restored.queue_once()
                clock.advance(1)
                await restored.queue_once()
                result = await restored.enqueue_batch(body)
                assert result["admitted_count"] == 2 and result["queued_count"] == 0
                assert [item["dispatch_id"] for item in result["items"]] == original_ids
                assert len(cluster.facilities.holds) == 2
                snapshot = await restored.repository.load()
                assert len(snapshot["records"]) == 2
                assert all(record["claimed"] for record in snapshot["records"].values())
                assert all(record["approval_intent"]["queue_status"] == "ADMITTED"
                           for record in snapshot["records"].values())
            finally:
                await restored.close()
    asyncio.run(scenario())
