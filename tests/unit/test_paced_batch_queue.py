"""Durable paced batch admission guards, using isolated in-process services."""
import asyncio
from datetime import datetime
from uuid import UUID, uuid4, uuid5

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.contracts.dispatch import BatchApproval, DispatchApproval
from ev_fleet.dispatch.service import DispatchService
from tests.dispatch_support import Cluster
from tests.unit.test_persistence import MemoryRepository


async def queued(cluster, quantity=1):
    cluster.dispatch.settings = cluster.dispatch.settings.model_copy(update={"dispatch_in_flight_limit": 4})
    for index in range(quantity):
        await cluster.vehicle(f"queued-{index}")
    response = await cluster.client.post(
        "http://rebalancing.test/api/v1/batch-recommendations",
        json={"target_zone_id": "cbd", "quantity": quantity})
    assert response.status_code == 201, response.text
    assert response.json()["assignments"], response.text
    body = BatchApproval(request_id=uuid4(),
                         batch_recommendation_id=response.json()["batch_recommendation_id"])
    return body, await cluster.dispatch.enqueue_batch(body)


def test_http_enqueue_is_prompt_durable_and_get_never_admits(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            async def blocked(*args, **kwargs):
                raise AssertionError("enqueue/GET must not prepare or acquire")
            monkeypatch.setattr(cluster.dispatch, "prepare", blocked)
            monkeypatch.setattr(cluster.dispatch, "acquire", blocked)
            await cluster.vehicle("prompt-ev")
            recommendation = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            body = BatchApproval(request_id=uuid4(),
                batch_recommendation_id=recommendation.json()["batch_recommendation_id"])
            accepted = await asyncio.wait_for(cluster.client.post(
                "http://dispatch.test/api/v1/batches", json=body.model_dump(mode="json")), .5)
            assert accepted.status_code == 202, accepted.text
            job = accepted.json()
            replay = await asyncio.wait_for(cluster.client.post(
                "http://dispatch.test/api/v1/batches", json=body.model_dump(mode="json")), .5)
            assert replay.status_code == 202, replay.text
            assert replay.json()["status"] == "QUEUED"
            assert replay.json()["job_status"] == "QUEUED"
            item = job["items"][0]
            child = cluster.dispatch.store.get(item["dispatch_id"])
            assert child.approval_intent["recommendation"]["selected_vehicle_id"] == item["vehicle_id"]
            assert cluster.dispatch.persisted_snapshot["records"][item["dispatch_id"]]["approval_intent"]
            await cluster.dispatch.reconcile_once()
            assert (await cluster.dispatch.get_batch(body.request_id))["items"][0]["state"] == "QUEUED"
            assert not cluster.facilities.holds
    asyncio.run(scenario())


def test_queue_restarts_with_frozen_intent_and_same_child_id(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            repository = MemoryRepository()
            repository.payload = cluster.dispatch.current_snapshot()
            service = DispatchService(cluster.client, cluster.config, clock, repository)
            await service.start()
            item = job["items"][0]
            child = service.store.get(item["dispatch_id"])
            assert child.data["request_id"] == str(uuid5(body.request_id, item["recommendation_id"]))
            assert child.approval_intent["recommendation"]["recommendation_id"] == item["recommendation_id"]
            clock.advance(11)  # Original TTL expired; accepted authorization remains live.
            await cluster.telemetry(item["vehicle_id"])
            await service.queue_once()
            assert child.claimed, child.view()
            assert datetime.fromisoformat(child.claim_request["approve_before"]) == datetime.fromisoformat(job["expires_at"])
            await service.queue_once()
            assert len(cluster.facilities.holds) == 1
            await service.close()
    asyncio.run(scenario())


def test_transient_preparation_backoff_then_success_and_terminal_policy(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            original = cluster.dispatch.prepare
            attempts = 0
            async def flaky(record):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise HTTPException(504, "DEPENDENCY_TIMEOUT")
                await original(record)
            monkeypatch.setattr(cluster.dispatch, "prepare", flaky)
            await cluster.dispatch.queue_once()
            first = (await cluster.dispatch.get_batch(body.request_id))["items"][0]
            assert first["queue_status"] == "WAITING" and first["attempts"] == 1
            await cluster.dispatch.queue_once()
            assert attempts == 1
            clock.advance(2)
            await cluster.dispatch.queue_once()
            assert (await cluster.dispatch.get_batch(body.request_id))["items"][0]["queue_status"] == "ADMITTED"
            assert len(cluster.facilities.holds) == 1
    asyncio.run(scenario())


def test_queued_intent_expires_without_remote_mutation_and_close_is_safe(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster, 2)
            first, second = job["items"]
            closed = await cluster.dispatch.close_batch_item(body.request_id, UUID(first["recommendation_id"]))
            assert closed["items"][0]["queue_status"] == "CANCELLED"
            clock.advance(1801)
            await cluster.dispatch.queue_once()
            result = await cluster.dispatch.get_batch(body.request_id)
            assert result["job_status"] == "EXPIRED"
            assert result["items"][1]["state"] == "REJECTED"
            assert not cluster.facilities.holds
            with pytest.raises(HTTPException):
                await cluster.dispatch.batch_intent(body.request_id, UUID(second["recommendation_id"]))
    asyncio.run(scenario())


def test_global_cap_counts_unknown_single_claim_and_rate_limits_new_admissions(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster, 5)
            for index in range(4):
                await cluster.dispatch.queue_once()
                assert sum(r.claimed for r in cluster.dispatch.store.records.values()) == index + 1
                await cluster.dispatch.queue_once()
                assert sum(r.claimed for r in cluster.dispatch.store.records.values()) == index + 1
                clock.advance(1)
            uncertain = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            uncertain.data.update(state="FAILED", claim_held=None)
            await cluster.dispatch.queue_once()
            assert sum(r.claimed for r in cluster.dispatch.store.records.values()) == 4
            await cluster.vehicle("manual-ev")
            recommendation = await cluster.recommend()
            with pytest.raises(HTTPException) as error:
                await cluster.dispatch.approve(DispatchApproval(request_id=uuid4(),
                    recommendation_id=recommendation["recommendation_id"]))
            assert error.value.status_code == 503
    asyncio.run(scenario())


def test_lost_claim_reconciles_same_id_after_authorization_expiry_without_new_mutations(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            item = job["items"][0]
            cluster.failures.append({"method": "PUT", "path": "/claims/", "action": "lose_response"})
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(item["dispatch_id"])
            exact = dict(child.claim_request)
            assert child.data["claim_held"] is None
            with pytest.raises(HTTPException) as error:
                await cluster.dispatch.close_batch_item(body.request_id, UUID(item["recommendation_id"]))
            assert error.value.status_code == 409
            clock.advance(1801)
            mutations = []
            original = cluster.route
            async def tracked(request):
                if request.method in {"POST", "PUT"}:
                    mutations.append(str(request.url))
                return await original(request)
            monkeypatch.setattr(cluster.client, "_transport", httpx.MockTransport(tracked))
            await cluster.dispatch.queue_once()
            assert child.claimed
            assert child.claim_request == exact
            assert not mutations
            assert len(cluster.facilities.holds) == 1
    asyncio.run(scenario())


def test_lost_parking_response_expiry_never_starts_a_fresh_claim(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            cluster.failures.append({"method": "POST", "path": "/holds", "action": "lose_response"})
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            assert child.claim_request and child.data["claim_held"] is None
            clock.advance(1801)
            original = cluster.route
            async def forbid_new_effects(request):
                assert not (request.method == "PUT" and "/claims/" in request.url.path)
                assert not (request.method == "POST" and request.url.path.endswith("/holds"))
                return await original(request)
            monkeypatch.setattr(cluster.client, "_transport", httpx.MockTransport(forbid_new_effects))
            await cluster.dispatch.queue_once()
            assert child.data["state"] == "REJECTED"
            assert child.data["parking_status"] == "RELEASED"
            assert not (await cluster.fleet.get(child.data["vehicle_id"]))["active_claim"]
    asyncio.run(scenario())


def test_policy_rejection_is_terminal_and_intent_callback_does_not_deadlock(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            item = job["items"][0]
            child = cluster.dispatch.store.get(item["dispatch_id"])
            async with child.lock:
                async with cluster.dispatch.admission_lock:
                    intent = await asyncio.wait_for(cluster.client.get(
                        f"http://dispatch.test/api/v1/batches/{body.request_id}/items/{item['recommendation_id']}/intent"), .5)
                    assert intent.status_code == 200, intent.text
            original = cluster.dispatch.prepare
            calls = 0
            async def reject(record):
                nonlocal calls
                calls += 1
                raise HTTPException(409, "ROUTE_BELOW_OPERATIONAL_RESERVE")
            monkeypatch.setattr(cluster.dispatch, "prepare", reject)
            await cluster.dispatch.queue_once()
            clock.advance(30)
            await cluster.dispatch.queue_once()
            assert calls == 1
            result = await cluster.dispatch.get_batch(body.request_id)
            assert result["items"][0]["queue_status"] == "REJECTED"
            assert result["job_status"] == "COMPLETED"
            assert not cluster.facilities.holds
    asyncio.run(scenario())


def test_runtime_race_waits_with_exact_known_hold_and_never_changes_claim_identity(clock, monkeypatch):
    from ev_fleet.contracts.vehicles import SimulatorRuntimeReport
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            vehicle_id = job["items"][0]["vehicle_id"]
            original = cluster.fleet.claim
            tripped = False
            process_id = uuid4()
            def report(sequence, mode):
                return SimulatorRuntimeReport(vehicle_id=vehicle_id, process_id=process_id,
                    sequence=sequence, initialized=True, live=True, ready=mode == "RUNNING",
                    mode=mode, heartbeat_age_seconds=0, heartbeat_timeout_seconds=5,
                    checkpoint_revision=0)
            async def race(name, dispatch_id, request):
                nonlocal tripped
                if not tripped:
                    tripped = True
                    await cluster.fleet.report_runtime(vehicle_id, report(0, "RETRYING"))
                return await original(name, dispatch_id, request)
            monkeypatch.setattr(cluster.fleet, "claim", race)
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            exact = dict(child.claim_request)
            first = (await cluster.dispatch.get_batch(body.request_id))["items"][0]
            assert first["queue_status"] == "WAITING", first
            assert child.data["parking_status"] == "RESERVED"
            assert not (await cluster.fleet.get(vehicle_id))["active_claim"]
            await cluster.fleet.report_runtime(vehicle_id, report(1, "RUNNING"))
            clock.advance(2)
            await cluster.dispatch.queue_once()
            assert child.claimed
            assert child.claim_request == exact
            assert len(cluster.facilities.holds) == 1
    asyncio.run(scenario())


def test_completed_job_stays_completed_after_authorization_deadline(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            await cluster.telemetry(child.data["vehicle_id"], soc=70, state="ARRIVED",
                dispatch_id=child.data["dispatch_id"], position=child.data["command"]["target_position"])
            await cluster.dispatch.get(child.data["dispatch_id"])
            assert (await cluster.dispatch.get_batch(body.request_id))["job_status"] == "COMPLETED"
            clock.advance(1801)
            result = await cluster.dispatch.get_batch(body.request_id)
            assert result["job_status"] == "COMPLETED"
            assert result["completed_count"] == 1
    asyncio.run(scenario())


def test_cancel_waiting_for_admission_cannot_be_revived_by_worker(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            item = job["items"][0]
            await cluster.dispatch.admission_lock.acquire()
            closing = asyncio.create_task(cluster.dispatch.close_batch_item(
                body.request_id, UUID(item["recommendation_id"])))
            await asyncio.sleep(0)
            worker = asyncio.create_task(cluster.dispatch.queue_once())
            await asyncio.sleep(0)
            cluster.dispatch.admission_lock.release()
            await asyncio.wait_for(asyncio.gather(closing, worker), 2)
            child = cluster.dispatch.store.get(item["dispatch_id"])
            assert child.approval_intent["queue_status"] == "CANCELLED"
            assert child.approval_intent["attempts"] == 0
            assert child.data["state"] == "REJECTED"
            assert not cluster.facilities.holds
    asyncio.run(scenario())


def test_checkpoint_wait_spanning_expiry_cannot_create_a_new_hold(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            entered, release = asyncio.Event(), asyncio.Event()
            original = cluster.dispatch.persist
            calls = 0
            async def blocked():
                nonlocal calls
                calls += 1
                if calls == 2:
                    entered.set()
                    await release.wait()
                await original()
            monkeypatch.setattr(cluster.dispatch, "persist", blocked)
            worker = asyncio.create_task(cluster.dispatch.queue_once())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                clock.advance(1801)
            finally:
                release.set()
                await asyncio.wait_for(worker, 2)
            assert not cluster.facilities.holds
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            assert child.data["state"] == "REJECTED"
    asyncio.run(scenario())


@pytest.mark.parametrize("reason", ["ONEMAP_TIMEOUT", "ONEMAP_UNAVAILABLE", "ONEMAP_RATE_LIMITED", "CRDS_INPUT_TIMEOUT"])
def test_known_transient_route_and_input_failures_retry_automatically(clock, monkeypatch, reason):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            original = cluster.dispatch.prepare
            calls = 0
            async def transient(record):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise HTTPException(503, reason)
                await original(record)
            monkeypatch.setattr(cluster.dispatch, "prepare", transient)
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            assert child.approval_intent["queue_status"] == "WAITING"
            assert not child.claim_request
            clock.advance(2)
            await cluster.dispatch.queue_once()
            assert child.claimed
    asyncio.run(scenario())


def test_batch_get_reads_committed_rows_and_replay_waits_for_acceptance_commit(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            child.approval_intent.update(queue_status="CANCELLED")
            child.data.update(state="REJECTED", failure_reason="UNCOMMITTED")
            observed = await cluster.dispatch.get_batch(body.request_id)
            assert observed["items"][0]["queue_status"] == "QUEUED"
            # Simulate cancelled initial enqueue before it entered persist_lock:
            # RAM has the exact human request, but the committed frame does not.
            cluster.dispatch.persisted_snapshot = {**cluster.dispatch.persisted_snapshot,
                "batches": {}, "records": {}, "requests": {}, "recommendations": {},
                "batch_recommendations": {}}
            entered, release = asyncio.Event(), asyncio.Event()
            original = cluster.dispatch.repository.save
            async def block_save(*args, **kwargs):
                entered.set()
                await release.wait()
                await original(*args, **kwargs)
            monkeypatch.setattr(cluster.dispatch.repository, "save", block_save)
            replay = asyncio.create_task(cluster.dispatch.enqueue_batch(body))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                assert not replay.done()
            finally:
                release.set()
                await asyncio.wait_for(replay, 2)
            assert str(body.request_id) in cluster.dispatch.persisted_snapshot["batches"]
    asyncio.run(scenario())


def test_terminal_queue_rejection_keeps_slot_until_known_hold_cleanup_reconciles(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            async def reject(*args):
                raise HTTPException(409, "BELOW_OPERATIONAL_RESERVE")
            monkeypatch.setattr(cluster.fleet, "claim", reject)
            cluster.failures.append({"method": "POST", "path": "/release", "action": "offline"})
            await cluster.dispatch.queue_once()
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            assert child.data["state"] == "REJECTED"
            assert child.data["parking_status"] == "RESERVED"
            assert (await cluster.dispatch.get_batch(body.request_id))["job_status"] == "RUNNING"
            await cluster.dispatch.reconcile_once()
            assert child.data["parking_status"] == "RELEASED"
            assert (await cluster.dispatch.get_batch(body.request_id))["job_status"] == "COMPLETED"
    asyncio.run(scenario())


def test_queued_crds_revalidation_uses_committed_callback_after_original_ttl(clock, monkeypatch):
    monkeypatch.setenv("CRDS_MODE", "ACTIVE")
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch.test")
    async def scenario():
        from ev_fleet.contracts.demand import DemandUpdate
        async with Cluster(clock) as cluster:
            for zone_id in cluster.demand.zones:
                zone = await cluster.demand.get(zone_id)
                await cluster.demand.update(zone_id, "demand", DemandUpdate(
                    expected_version=zone["demand_version"], observed_at=clock.now(),
                    desired_available_vehicles=10 if zone_id == "cbd" else 0))
            for index in range(8):
                await cluster.vehicle(f"extra-{index}", soc=90)
            body, job = await queued(cluster)
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            assert child.approval_intent["recommendation"]["crds"] is not None
            clock.advance(11)
            await cluster.telemetry(child.data["vehicle_id"])
            await asyncio.wait_for(cluster.dispatch.queue_once(), 5)
            assert child.claimed, child.view()
            assert child.data["revalidated_metrics"]["crds"]["approval_allowed"]
    asyncio.run(scenario())


def test_enqueue_replay_returns_the_committed_frame_after_its_barrier(clock, monkeypatch):
    async def scenario():
        async with Cluster(clock) as cluster:
            body, job = await queued(cluster)
            child = cluster.dispatch.store.get(job["items"][0]["dispatch_id"])
            original = cluster.dispatch.persist_if_changed
            async def advance_after_commit():
                result = await original()
                # Another task may observe completion immediately after the
                # barrier; that observation has not crossed its own commit yet.
                child.claimed = True
                child.data.update(state="SUCCEEDED", claim_held=False)
                child.approval_intent.update(queue_status="ADMITTED")
                return result
            monkeypatch.setattr(cluster.dispatch, "persist_if_changed", advance_after_commit)
            replay = await cluster.dispatch.enqueue_batch(body)
            assert replay["items"][0]["state"] == "QUEUED"
            assert replay["admitted_count"] == 0
            assert replay["completed_count"] == 0
            assert replay["job_status"] == "QUEUED"
    asyncio.run(scenario())


def test_group_queues_frozen_jobs_without_acquiring_and_tracks_durable_progress(clock, monkeypatch):
    from ev_fleet.contracts.dispatch import BatchApprovalGroupRequest, queued_batch_approval_id
    from tests.unit.test_recommendation_requests import MemorySnapshot

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("group-paced-ev")
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            batch_id = UUID(response.json()["batch_recommendation_id"])
            cluster.dispatch.repository = MemorySnapshot("dispatch", {})
            await cluster.dispatch.repository.start()
            original_load = cluster.dispatch.load_batch_preview

            async def load(batch_id, *, queued):
                return await original_load(batch_id, queued=False)

            async def forbidden(*args, **kwargs):
                pytest.fail("group admission must freeze jobs without synchronous approval")

            monkeypatch.setattr(cluster.dispatch, "load_batch_preview", load)
            monkeypatch.setattr(cluster.dispatch, "approve_batch", forbidden)
            queue = cluster.dispatch.batch_groups
            body = BatchApprovalGroupRequest(group_id=uuid4(), batch_recommendation_ids=[batch_id])
            await queue.submit(body)
            await queue.process(queue.records[str(body.group_id)])
            result = queue.detail(body.group_id)
            assert result.status == "RUNNING"
            assert result.plans[0].status == "RUNNING"
            assert result.plans[0].approval.job_status == "QUEUED"
            assert result.plans[0].request_id == queued_batch_approval_id(batch_id)
            assert not cluster.facilities.holds
            assert queue.page(0, 10, unresolved=True).groups == [result]
            assert cluster.dispatch.lookup_batch_approvals([result.plans[0].request_id])["batches"][0]["job_status"] == "QUEUED"
            record = next(iter(cluster.dispatch.store.records.values()))
            record.approval_intent["queue_status"] = "CANCELLED"
            cluster.dispatch.transition(record, "REJECTED", "OPERATOR_CLOSED_UNSTARTED_APPROVAL")
            # Polling must not publish changes until they commit.
            assert queue.detail(body.group_id) == result
            assert cluster.dispatch.lookup_batch_approvals([result.plans[0].request_id])["batches"][0]["job_status"] == "QUEUED"
            await cluster.dispatch.persist()
            finished = queue.detail(body.group_id)
            assert finished.status == "COMPLETED"
            assert finished.plans[0].status == "REJECTED"
            assert finished.plans[0].approval.job_status == "COMPLETED"
            assert queue.page(0, 10, unresolved=True).groups == []
    asyncio.run(scenario())


def test_queued_plan_enforces_active_crds_and_durably_replays_refusal(clock):
    from ev_fleet.contracts.dispatch import queued_batch_approval_id
    from tests.unit.test_recommendation_requests import MemorySnapshot

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("advisory-queue-ev")
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            batch_id = UUID(response.json()["batch_recommendation_id"])
            cluster.dispatch.repository = MemorySnapshot("dispatch", {})
            await cluster.dispatch.repository.start()
            request = BatchApproval(request_id=queued_batch_approval_id(batch_id),
                                    batch_recommendation_id=batch_id)
            with pytest.raises(HTTPException) as error:
                await cluster.dispatch.enqueue_batch(request)
            assert error.value.detail == "CRDS_ADVISORY_ONLY"
            assert not cluster.dispatch.store.records
            lookup = cluster.dispatch.lookup_batch_approvals([request.request_id])
            assert lookup["batches"] == []
            assert lookup["refusals"][0]["reason_code"] == "CRDS_ADVISORY_ONLY"
            restored = DispatchService(cluster.client, cluster.config, clock,
                                       cluster.dispatch.repository)
            await restored.start()
            clock.advance(1801)
            with pytest.raises(HTTPException) as replay:
                await restored.enqueue_batch(request)
            assert replay.value.detail == "CRDS_ADVISORY_ONLY"
    asyncio.run(scenario())
