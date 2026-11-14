"""Queued CRDS validation uses verified durable Dispatch authorization."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest

from ev_fleet.contracts.demand import DemandUpdate
from tests.routing_fixture import fixture_route
from tests.unit.test_requested_quantity import operator_cluster, request_batch


@pytest.mark.parametrize("corrupt", [None, "batch_request_id", "dispatch_id", "recommendation_id", "expires_at",
    "policy", "model", "naive_expiry", "content_expiry"])
def test_queued_revalidation_requires_matching_live_authoritative_intent(clock, monkeypatch, corrupt):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=1) as cluster:
            batch = await request_batch(cluster, quantity=1)
            assignment = batch["assignments"][0]
            recommendation_id = assignment["recommendation_id"]
            response = await cluster.client.get(
                "http://rebalancing.test/api/v1/recommendations/" + recommendation_id)
            response.raise_for_status()
            recommendation = response.json()
            batch_id, dispatch_id = str(uuid4()), str(uuid4())
            expires = clock.now() + timedelta(minutes=30)
            recommendation["expires_at"] = expires.isoformat()
            intent = {"batch_request_id": batch_id, "dispatch_id": dispatch_id,
                "recommendation_id": recommendation_id, "expires_at": expires.isoformat(),
                "recommendation": recommendation}
            if corrupt == "expires_at":
                intent["expires_at"] = (clock.now() - timedelta(seconds=1)).isoformat()
                intent["recommendation"]["expires_at"] = intent["expires_at"]
            elif corrupt == "policy":
                intent["recommendation"]["crds"]["parameters"]["minimum_gain_vehicle_minutes"] += 1
            elif corrupt == "model":
                intent["recommendation"]["crds"]["scenario_model_version"] = "changed-model"
            elif corrupt == "naive_expiry":
                intent["expires_at"] = expires.replace(tzinfo=None).isoformat()
                intent["recommendation"]["expires_at"] = intent["expires_at"]
            elif corrupt == "content_expiry":
                intent["recommendation"]["expires_at"] = (expires + timedelta(seconds=1)).isoformat()
            elif corrupt:
                intent[corrupt] = str(uuid4())
            original = cluster.route
            seen = []
            async def respond(request):
                if request.url.path.endswith("/intent"):
                    seen.append(request.url.path)
                    return httpx.Response(200, json=deepcopy(intent))
                return await original(request)
            monkeypatch.setattr(cluster.client, "_transport", httpx.MockTransport(respond))
            clock.advance(121)
            await cluster.telemetry(assignment["vehicle_id"], soc=90)
            for zone_id in cluster.demand.zones:
                zone = await cluster.demand.get(zone_id)
                await cluster.demand.update(zone_id, "demand", DemandUpdate(
                    expected_version=zone["demand_version"], observed_at=clock.now(),
                    desired_available_vehicles=zone["demand"]["desired_available_vehicles"]))
            body = {"recommendation_id": recommendation_id, "dispatch_id": dispatch_id,
                "route": fixture_route({"latitude": 1.35, "longitude": 103.8513},
                    assignment["target_position"]).model_dump(mode="json"),
                "expected_planning_instance_id": str(cluster.dispatch.planning_instance_id),
                "expected_planning_revision": cluster.dispatch.planning_revision,
                "approved_batch_request_id": batch_id}
            reply = await cluster.client.post("http://rebalancing.test/api/v1/crds/revalidate", json=body)
            assert seen == [f"/api/v1/batches/{batch_id}/items/{recommendation_id}/intent"]
            if corrupt is None:
                assert reply.status_code == 200, reply.text
                assert reply.json()["allowed"] is True, reply.text
                direct = dict(body)
                direct.pop("approved_batch_request_id")
                old = await cluster.client.post("http://rebalancing.test/api/v1/crds/revalidate", json=direct)
                assert old.status_code == 404  # Direct approval never inherits queue authorization.
            elif corrupt == "expires_at":
                assert reply.status_code == 409, reply.text
                assert reply.json()["error"]["code"] == "APPROVED_INTENT_EXPIRED"
            elif corrupt in ("policy", "model"):
                assert reply.status_code == 409, reply.text
                assert reply.json()["error"]["code"] == "CRDS_POLICY_CHANGED"
            else:
                assert reply.status_code == 503, reply.text
                assert reply.json()["error"]["code"] == (
                    "APPROVED_INTENT_INVALID" if corrupt == "naive_expiry"
                    else "APPROVED_INTENT_IDENTITY_MISMATCH")
    asyncio.run(scenario())
