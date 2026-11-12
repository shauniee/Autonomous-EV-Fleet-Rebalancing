import asyncio
from datetime import timedelta
from uuid import uuid4

from fastapi import HTTPException
from fastapi.testclient import TestClient
import pytest

from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.dispatch import BatchApprovalGroupRequest, queued_batch_approval_id
from ev_fleet.contracts.recommendations import BatchRecommendation
from ev_fleet.dispatch.app import create_app
from ev_fleet.dispatch.service import DispatchService
from tests.unit.test_recommendation_requests import MemorySnapshot


def preview(clock, batch_id, vehicle):
    return BatchRecommendation(
        batch_recommendation_id=batch_id, target_zone_id="cbd", requested_count=1,
        recommended_count=1, unfulfilled_count=0, created_at=clock.now(),
        expires_at=clock.now() + timedelta(seconds=120), explanation="One EV", assumptions=[],
        assignments=[{
            "recommendation_id": uuid4(), "vehicle_id": vehicle, "facility_id": "cbd-parking",
            "facility_name": "CBD parking", "target_position": {"latitude": 1.28, "longitude": 103.85},
            "requires_charging": False, "distance_km": 3, "predicted_arrival_soc_pct": 70,
        }],
    )


def test_group_http_blocks_all_approvals_on_overlapping_vehicles(clock, monkeypatch):
    snapshots = {}
    monkeypatch.setattr(SnapshotRepository, "from_env", lambda name: MemorySnapshot(name, snapshots))
    first, second, third = uuid4(), uuid4(), uuid4()
    previews = {key: preview(clock, key, vehicle)
                for key, vehicle in [(first, "ev-1"), (second, "ev-1"), (third, "ev-2")]}

    async def load(self, batch_id, *, queued):
        return previews[batch_id]

    async def unexpected_approval(self, body):
        pytest.fail("conflicting group must not attempt any approval")

    monkeypatch.setattr(DispatchService, "load_batch_preview", load)
    monkeypatch.setattr(DispatchService, "approve_batch", unexpected_approval)
    path = "/api/v1/batch-approval-groups"
    group_id = str(uuid4())
    ids = list(map(str, [first, second, third]))
    payload = {"group_id": group_id, "batch_recommendation_ids": ids}
    with TestClient(create_app(clock=clock)) as client:
        assert client.post(path, json={**payload, "batch_recommendation_ids": [ids[0]] * 2}).status_code == 422
        accepted = client.post(path, json=payload)
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "QUEUED"
        assert client.post(path, json={**payload, "batch_recommendation_ids": ids[::-1]}).status_code == 409

        async def finished():
            async with asyncio.timeout(2):
                while client.app.state.service.batch_groups.detail(payload_model.group_id).status != "CONFLICT":
                    await asyncio.sleep(0.005)

        payload_model = BatchApprovalGroupRequest.model_validate(payload)
        client.portal.call(finished)
        result = client.get(f"{path}/{group_id}").json()
        assert result["conflicts"] == [{"vehicle_id": "ev-1", "batch_recommendation_ids": ids[:2]}]
        assert [plan["status"] for plan in result["plans"]] == ["SKIPPED"] * 3
        assert client.post(path, json=payload).json() == result
        assert client.get(path).json()["groups"] == [result]
        assert client.get(path, params={"unresolved": True}).json()["groups"] == []


def test_group_resumes_only_unfinished_plans_and_retries_unknown_with_same_ids(clock, monkeypatch):
    async def scenario():
        snapshots = {}
        first, second, third = uuid4(), uuid4(), uuid4()
        body = BatchApprovalGroupRequest(group_id=uuid4(), batch_recommendation_ids=[first, second, third])
        plans = {key: preview(clock, key, f"ev-{index}") for index, key in enumerate(body.batch_recommendation_ids)}
        calls = []

        async def load(self, batch_id, *, queued):
            return plans[batch_id]

        async def approve(self, request):
            key = request.batch_recommendation_id
            calls.append((key, request.request_id))
            if key == second and len(calls) == 2:
                raise asyncio.CancelledError
            if key == second and len(calls) == 3:
                raise HTTPException(503, "DEPENDENCY_TIMEOUT")
            if key == third:
                raise HTTPException(409, "BATCH_EXPIRED")
            return {"status": "APPROVED"}

        monkeypatch.setattr(DispatchService, "load_batch_preview", load)
        monkeypatch.setattr(DispatchService, "approve_batch", approve)
        first_service = DispatchService(None, clock=clock, repository=MemorySnapshot("dispatch", snapshots))
        await first_service.start()
        await first_service.batch_groups.submit(body)
        key = str(body.group_id)
        with pytest.raises(asyncio.CancelledError):
            await first_service.batch_groups.process(first_service.batch_groups.records[key])

        restored = DispatchService(None, clock=clock, repository=MemorySnapshot("dispatch", snapshots))
        await restored.start()
        queue = restored.batch_groups
        assert [plan.status for plan in queue.detail(body.group_id).plans] == ["APPROVED", "RUNNING", "QUEUED"]
        await queue.process(queue.records[key])
        result = queue.detail(body.group_id)
        assert result.status == "UNKNOWN"
        assert [plan.status for plan in result.plans] == ["APPROVED", "UNKNOWN", "REJECTED"]
        assert result.plans[2].reason_code == "BATCH_EXPIRED"
        assert queue.page(0, 10, unresolved=True).groups == [result]
        assert (await queue.submit(body)).status == "QUEUED"
        await queue.process(queue.records[key])
        assert queue.detail(body.group_id).status == "COMPLETED"
        assert calls == [(batch_id, queued_batch_approval_id(batch_id))
                         for batch_id in (first, second, second, third, second)]

    asyncio.run(scenario())
