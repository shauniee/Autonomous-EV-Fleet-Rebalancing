import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from ev_fleet.common.persistence import SnapshotRepository, decode_state, encode_state
from ev_fleet.common.settings import CrdsSettings
from ev_fleet.contracts.crds import CrdsMetadata, CrdsParameters
from ev_fleet.contracts.recommendations import QueuedRecommendationRequest
from ev_fleet.rebalancing import app
from ev_fleet.rebalancing.request_queue import RecommendationQueue
from ev_fleet.rebalancing.store import RecommendationStore


class MemorySnapshot(SnapshotRepository):
    def __init__(self, namespace, snapshots):
        super().__init__(namespace, "memory")
        self.snapshots = snapshots

    async def start(self):
        self.pool = object()

    async def close(self):
        self.pool = None

    async def save(self, payload, **kwargs):
        self.snapshots[self.namespace] = deepcopy(encode_state(payload))

    async def load(self):
        return decode_state(deepcopy(self.snapshots.get(self.namespace)))


def metadata():
    return CrdsMetadata(
        decision="HOLD", reason_code="NO_USEFUL_MOVE", parameters=CrdsParameters(),
        scenario_model_version="test", scenario_count=1, epoch_id=uuid4(),
        input_fingerprint="test", source_references={},
    )


def test_queue_api_idempotency_capacity_and_restart(clock, monkeypatch):
    snapshots = {}
    monkeypatch.setattr(SnapshotRepository, "from_env", lambda namespace: MemorySnapshot(namespace, snapshots))
    monkeypatch.setenv("TRAFFIC_CACHE_BACKEND", "INLINE")
    monkeypatch.setenv("TRAFFIC_SOURCE", "DISABLED")
    started = asyncio.Event()
    release = asyncio.Event()
    planned = []

    async def plan_one(*args, **kwargs):
        planned.append((args[5], kwargs["vehicle_ids"], kwargs["planning_mode"]))
        started.set()
        await release.wait()

    async def plan_batch(*args, **kwargs):
        decision = metadata()
        decision.source_references["decision_evidence"] = {
            "vehicles": {"ev-2": {"reason": "VEHICLE_MISSING"}},
        }
        return [], decision, "NO_USEFUL_MOVE"

    monkeypatch.setattr(app, "plan_one", plan_one)
    monkeypatch.setattr(app, "plan_batch", plan_batch)
    settings = CrdsSettings(request_queue_capacity=2, request_queue_max_wait_seconds=10)
    route = "/api/v1/recommendation-requests"
    first = {"request_id": str(uuid4()), "target_zone_id": "cbd", "quantity": 2, "vehicle_ids": ["ev-2", "ev-1"],
             "planning_mode": "FULFIL_REQUEST"}
    second = {"request_id": str(uuid4()), "target_zone_id": "punggol", "quantity": 1}
    with TestClient(app.create_app(clock=clock, crds_settings=settings)) as client:
        try:
            assert client.post(route, json=first).status_code == 202
            async def wait_until_started():
                await asyncio.wait_for(started.wait(), timeout=2)

            client.portal.call(wait_until_started)
            reordered = client.post(route, json={**first, "vehicle_ids": ["ev-1", "ev-2"]})
            assert reordered.json()["status"] == "EVALUATING"
            assert reordered.json()["vehicle_ids"] == ["ev-1", "ev-2"]
            assert client.post(route, json={**first, "vehicle_ids": ["ev-1", "ev-3"]}).status_code == 409
            for invalid in ({"quantity": 3}, {"vehicle_ids": []}, {"vehicle_ids": ["ev-1", "ev-1"]},
                            {"vehicle_id": "ev-1", "quantity": 1, "vehicle_ids": ["ev-2"]},
                            {"vehicle_ids": ["ev-1", "invalid id"]}):
                assert client.post(route, json={**first, **invalid}).status_code == 422
            assert client.post(route, json=second).status_code == 202
            assert client.post(route, json={**second, "request_id": str(uuid4())}).status_code == 429
            page = client.get(route, params={"limit": 1}).json()
            assert page["requests"][0]["request_id"] == second["request_id"]
            assert page["next_offset"] == 1
            assert client.get(route, params={"status": "EVALUATING"}).json()["requests"][0]["request_id"] == first["request_id"]
            clock.advance(11)
            client.portal.call(release.set)

            async def wait_until_finished():
                async with asyncio.timeout(2):
                    while True:
                        if snapshots["rebalancing.recommendation_requests"]:
                            saved = decode_state(snapshots["rebalancing.recommendation_requests"])
                            if saved[second["request_id"]]["record"].status == "DEFERRED":
                                return
                        await asyncio.sleep(0)

            client.portal.call(wait_until_finished)
        finally:
            client.portal.call(release.set)
    with TestClient(app.create_app(clock=clock, crds_settings=settings)) as client:
        finished = client.get(f'{route}/{first["request_id"]}').json()
        assert finished["status"] == "HOLD"
        assert finished["batch"]["crds"]["reason_code"] == "NO_USEFUL_MOVE"
        assert client.get(f'{route}/{second["request_id"]}').json()["reason_code"] == "QUEUE_WAIT_EXCEEDED"
        clock.advance(121)
        history = client.get(f'{route}/{first["request_id"]}').json()
        assert history["batch"] is None
        assert history["decision"] == "HOLD"
        assert history["vehicle_reasons"] == {"ev-2": "VEHICLE_MISSING"}
    assert planned == [("cbd", ["ev-1", "ev-2"], "FULFIL_REQUEST")]


def test_checkpoint_resumes_publication_without_replanning(clock):
    async def scenario():
        repository = MemorySnapshot("requests", {})
        queue = RecommendationQueue(clock, repository, capacity=2, max_wait_seconds=10)
        request = QueuedRecommendationRequest(request_id=uuid4(), target_zone_id="cbd", quantity=1)
        await queue.submit(request)
        key = str(request.request_id)
        batch_id = str(uuid4())
        child_id = str(uuid4())
        expires_at = (clock.now() + timedelta(seconds=120)).isoformat()
        decision = metadata().model_copy(update={"decision": "MOVE", "approval_allowed": True})
        batch = {
            "batch_recommendation_id": batch_id, "target_zone_id": "cbd",
            "requested_count": 1, "recommended_count": 1, "unfulfilled_count": 0,
            "created_at": clock.now().isoformat(), "expires_at": expires_at,
            "assignments": [{
                "recommendation_id": child_id, "vehicle_id": "ev-1",
                "facility_id": "cbd-1", "facility_name": "CBD parking",
                "target_position": {"latitude": 1.28, "longitude": 103.85},
                "requires_charging": False, "distance_km": 3, "predicted_arrival_soc_pct": 60,
            }],
            "crds": decision.model_dump(mode="json"),
            "explanation": "One EV", "assumptions": [],
        }

        async def evaluate(_):
            return {"batch": batch, "children": [{"recommendation_id": child_id, "expires_at": expires_at}]}

        store = RecommendationStore(clock, repository=MemorySnapshot("recommendations", {}))

        async def interrupted(checkpoint):
            await store.put_many(checkpoint["children"])
            raise RuntimeError("publication interrupted")

        with pytest.raises(RuntimeError, match="publication interrupted"):
            await queue.process(queue.entries[key], evaluate, interrupted)

        recovered = RecommendationQueue(clock, repository, capacity=2, max_wait_seconds=10)
        await recovered.start()
        batches = RecommendationStore(clock, repository=MemorySnapshot("batches", {}))

        async def unexpected_evaluation(_):
            pytest.fail("checkpoint should prevent replanning")

        async def publish(checkpoint):
            await store.put_many(checkpoint["children"])
            await batches.put_many([{**checkpoint["batch"], "recommendation_id": batch_id}])

        await recovered.process(recovered.entries[key], unexpected_evaluation, publish)
        result = await recovered.get(key)
        assert result.status == "READY"
        assert str(result.batch_recommendation_id) == batch_id
        assert result.assignments[0].vehicle_id == "ev-1"
        assert list(store.records) == [child_id]
        assert (await batches.get(batch_id))["recommendation_id"] == batch_id
        clock.advance(120)
        assert (await recovered.get(key)).status == "EXPIRED"

    asyncio.run(scenario())


def test_queue_is_disabled_without_durable_storage(clock, monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("TRAFFIC_CACHE_BACKEND", "INLINE")
    monkeypatch.setenv("TRAFFIC_SOURCE", "DISABLED")
    route = "/api/v1/recommendation-requests"
    with TestClient(app.create_app(clock=clock, crds_settings=CrdsSettings())) as client:
        assert client.get(route).json()["enabled"] is False
        result = client.post(route, json={
            "request_id": str(uuid4()), "target_zone_id": "cbd", "quantity": 1,
        })
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "RECOMMENDATION_QUEUE_DISABLED"


def test_queued_approval_refusal_is_durable_and_retry_keeps_original_reason(clock, monkeypatch):
    from fastapi import HTTPException
    from ev_fleet.contracts.dispatch import BatchApproval, BatchApprovalLookup, queued_batch_approval_id
    from ev_fleet.dispatch import service

    async def scenario():
        snapshots = {}
        batch_id = uuid4()
        request_id = queued_batch_approval_id(batch_id)
        body = BatchApproval(request_id=request_id, batch_recommendation_id=batch_id)
        calls = 0

        async def expired(*args, **kwargs):
            nonlocal calls
            calls += 1
            raise HTTPException(404, "RECOMMENDATION_MISSING_OR_EXPIRED")

        monkeypatch.setattr(service, "request_json", expired)
        first = service.DispatchService(None, clock=clock, repository=MemorySnapshot("dispatch", snapshots))
        await first.start()
        with pytest.raises(HTTPException, match="RECOMMENDATION_MISSING_OR_EXPIRED"):
            await first.approve_batch(body)

        restored = service.DispatchService(None, clock=clock, repository=MemorySnapshot("dispatch", snapshots))
        await restored.start()
        lookup = BatchApprovalLookup.model_validate(restored.lookup_batch_approvals([request_id, uuid4()]))
        assert lookup.refusals[0].batch_recommendation_id == batch_id
        assert lookup.refusals[0].reason_code == "RECOMMENDATION_MISSING_OR_EXPIRED"
        assert lookup.batches == []
        with pytest.raises(HTTPException, match="RECOMMENDATION_MISSING_OR_EXPIRED"):
            await restored.approve_batch(body)
        assert calls == 1

    asyncio.run(scenario())


def test_queue_accepts_fifty_selected_evs_and_preserves_planning_mode():
    request = QueuedRecommendationRequest(request_id=uuid4(), target_zone_id="cbd",
        quantity=50, vehicle_ids=[f"ev-{index}" for index in range(50)],
        planning_mode="FULFIL_REQUEST")
    assert request.quantity == 50
    assert request.vehicle_ids is not None
    assert len(request.vehicle_ids) == 50
    assert request.planning_mode == "FULFIL_REQUEST"
    with pytest.raises(ValueError):
        QueuedRecommendationRequest.model_validate({**request.model_dump(), "quantity": 51})
