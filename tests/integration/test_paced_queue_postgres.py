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


def test_group_metadata_and_paced_job_progress_survive_partitioned_reload(
        telemetry_database_url, clock, monkeypatch):
    from uuid import UUID
    from ev_fleet.contracts.dispatch import BatchApprovalGroupRequest, queued_batch_approval_id

    async def scenario():
        async with Cluster(clock) as cluster:
            old = cluster.dispatch
            namespace = "paced-group-" + uuid4().hex
            old.repository = RecordSnapshotRepository(namespace, "records", telemetry_database_url)
            await old.repository.start()
            await old.persist()
            await cluster.vehicle("persisted-group-ev")
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            batch_id = UUID(response.json()["batch_recommendation_id"])
            original_load = old.load_batch_preview

            async def load(batch_id, *, queued):
                return await original_load(batch_id, queued=False)

            monkeypatch.setattr(old, "load_batch_preview", load)
            request = BatchApprovalGroupRequest(group_id=uuid4(), batch_recommendation_ids=[batch_id])
            await old.batch_groups.submit(request)
            await old.batch_groups.process(old.batch_groups.records[str(request.group_id)])
            before = old.batch_groups.detail(request.group_id)
            child_id = before.plans[0].approval.items[0].dispatch_id
            assert before.status == "RUNNING"
            assert before.plans[0].approval.job_status == "QUEUED"
            assert not cluster.facilities.holds
            await old.close()
            restored = DispatchService(cluster.client, old.settings, clock,
                RecordSnapshotRepository(namespace, "records", telemetry_database_url))
            await restored.start()
            cluster.dispatch = restored
            cluster.apps["dispatch.test"].state.service = restored
            cluster.apps["dispatch.test"].state.persistence = restored.repository
            try:
                assert restored.batch_groups.detail(request.group_id) == before
                assert restored.batch_groups.page(0, 10, unresolved=True).groups == [before]
                clock.advance(11)
                await cluster.telemetry("persisted-group-ev")
                await restored.queue_once()
                after = restored.batch_groups.detail(request.group_id)
                assert after.status == "RUNNING"
                assert after.plans[0].approval.admitted_count == 1
                assert after.plans[0].approval.items[0].dispatch_id == child_id
                assert after.plans[0].request_id == queued_batch_approval_id(batch_id)
                assert len(cluster.facilities.holds) == 1
                snapshot = await restored.repository.load()
                assert snapshot["batch_approval_groups"][str(request.group_id)]["preflight_done"]
                assert snapshot["records"][str(child_id)]["approval_intent"]["queue_status"] == "ADMITTED"
            finally:
                await restored.close()
    asyncio.run(scenario())
