import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException
from tests.dispatch_support import Cluster
from ev_fleet.contracts.dispatch import CommandAcknowledgement, DispatchEvent
from ev_fleet.dispatch.service import DispatchService
from tests.unit.test_persistence import MemoryRepository
import httpx


@pytest.mark.parametrize("failure", ["stale", "offline", "low_soc", "busy", "target_instance_changed", "expired"])
def test_approval_revalidates_current_state(clock, failure):
    async def scenario():
        async with Cluster(clock, recommendation_ttl=120 if failure in ("stale", "offline") else 10) as cluster:
            await cluster.vehicle()
            rec = await cluster.recommend()
            if failure == "low_soc": await cluster.telemetry(soc=19)
            if failure == "busy": await cluster.telemetry(state="RELOCATING", dispatch_id=uuid4())
            if failure in ("stale", "offline", "expired"):
                clock.advance({"stale": 15, "offline": 30, "expired": 11}[failure])
            if failure == "target_instance_changed":
                cluster.demand.instance_id = str(uuid4())
            with pytest.raises(HTTPException) as caught:
                await cluster.approve(rec)
            assert caught.value.status_code == 409
            if failure == "target_instance_changed":
                assert caught.value.detail == "TARGET_CHANGED"
            assert (await cluster.fleet.get("ev-1"))["active_claim"] is None
    asyncio.run(scenario())


def test_lost_claim_response_and_expired_retry_are_reconciled(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            cluster.failures.append({"method": "PUT", "path": "/claims/", "action": "lose_response"})
            dispatch, body = await cluster.approve()
            assert dispatch["state"] == "UNKNOWN"
            assert dispatch["revalidated_metrics"]["approval_decision"]["status"] == "UNKNOWN"
            assert (await cluster.fleet.get("ev-1"))["active_claim"]
            clock.advance(11)
            result = await cluster.dispatch.approve(body)
            assert result["state"] == "PENDING" and result["dispatch_id"] == dispatch["dispatch_id"]
            assert result["revalidated_metrics"]["approval_decision"]["status"] == "APPROVED"
            assert len(cluster.fleet.vehicles["ev-1"]["claims"]) == 1
    asyncio.run(scenario())


def test_ack_timeout_offline_relocation_and_reconnect(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            await cluster.dispatch.commands("ev-1")
            clock.advance(15)
            await cluster.telemetry()
            result = await cluster.dispatch.get(key)
            assert result["state"] == "UNKNOWN" and result["failure_reason"] == "ACK_TIMEOUT"
            assert result["claim_held"]
            assert (await cluster.dispatch.commands("ev-1"))["commands"][0] == dispatch["command"]
            # Matching telemetry proves execution even with no ACK response.
            await cluster.telemetry(state="RELOCATING", dispatch_id=key)
            assert (await cluster.dispatch.get(key))["state"] == "IN_PROGRESS"
            clock.advance(30)
            result = await cluster.dispatch.get(key)
            assert result["state"] == "UNKNOWN" and result["claim_held"]
            await cluster.telemetry(state="RELOCATING", dispatch_id=key, soc=79)
            assert (await cluster.dispatch.get(key))["state"] == "IN_PROGRESS"
    asyncio.run(scenario())


def test_lost_release_response_and_fleet_restart_remain_safe(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            await cluster.dispatch.commands("ev-1")
            await cluster.telemetry(state="ARRIVED", dispatch_id=key, position=dispatch["command"]["target_position"])
            cluster.failures.append({"method": "POST", "path": "/release", "action": "lose_response"})
            assert (await cluster.dispatch.get(key))["state"] == "UNKNOWN"
            assert (await cluster.dispatch.get(key))["state"] == "SUCCEEDED"
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            cluster.fleet.vehicles.clear()  # Simulated in-memory Fleet restart loses claims.
            cluster.fleet.instance_id = str(uuid4())
            result = await cluster.dispatch.get(dispatch["dispatch_id"])
            assert result["state"] == "UNKNOWN"
            assert (await cluster.dispatch.commands("ev-1"))["commands"] == []
    asyncio.run(scenario())


def test_lost_ack_response_is_safe_to_retry(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            await cluster.dispatch.commands("ev-1")
            url = "http://dispatch.test/api/v1/dispatches/" + dispatch["dispatch_id"] + "/acknowledgements"
            body = {"command_id": dispatch["command"]["command_id"], "vehicle_id": "ev-1", "accepted": True}
            cluster.failures.append({"method": "POST", "path": "/acknowledgements", "action": "lose_response"})
            with pytest.raises(httpx.ReadTimeout):
                await cluster.client.post(url, json=body)
            response = await cluster.client.post(url, json=body)
            assert response.status_code == 200 and response.json()["state"] == "ACKNOWLEDGED"
            assert len(cluster.fleet.vehicles["ev-1"]["claims"]) == 1
    asyncio.run(scenario())


def test_failed_execution_retains_claim_and_blocks_new_recommendation(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            await cluster.dispatch.commands("ev-1")
            await cluster.dispatch.acknowledge(dispatch["dispatch_id"], CommandAcknowledgement(
                command_id=dispatch["command"]["command_id"], vehicle_id="ev-1", accepted=True))
            event = DispatchEvent(command_id=dispatch["command"]["command_id"], vehicle_id="ev-1",
                sequence=0, observed_at=clock.now(), kind="FAILED", distance_travelled_km=1, reason="BATTERY_DEPLETED")
            result = await cluster.dispatch.event(dispatch["dispatch_id"], event)
            assert result["state"] == "FAILED" and result["claim_held"]
            assert result["failure_reason"] == "BATTERY_DEPLETED"
            assert result["revalidated_metrics"]["approval_decision"] == dispatch["revalidated_metrics"]["approval_decision"]
            assert result["revalidated_metrics"]["approval_decision"]["status"] == "APPROVED"
            assert (await cluster.recommend())["selected_vehicle_id"] is None
    asyncio.run(scenario())


def test_command_never_polled_still_times_out_without_releasing(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            clock.advance(16)
            await cluster.telemetry()
            result = await cluster.dispatch.get(dispatch["dispatch_id"])
            assert result["state"] == "UNKNOWN" and result["failure_reason"] == "ACK_TIMEOUT"
            assert result["delivery_attempts"] == 0 and result["claim_held"]
            assert (await cluster.dispatch.commands("ev-1"))["commands"][0] == dispatch["command"]
    asyncio.run(scenario())

def test_rejection_evidence_matches_conflict_status_lookup_and_reload(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            repository = MemoryRepository()
            cluster.dispatch.repository = repository
            await cluster.vehicle()
            recommendation = await cluster.recommend()
            await cluster.telemetry(soc=19)
            request_id = str(uuid4())
            body = {"request_id": request_id, "recommendation_id": recommendation["recommendation_id"]}
            base = "http://dispatch.test/api/v1"
            rejected = await cluster.client.post(f"{base}/dispatches", json=body)
            assert rejected.status_code == 409
            error = rejected.json()["error"]
            assert error["code"] == "ROUTE_BELOW_OPERATIONAL_RESERVE"
            assert error["request_id"] == rejected.headers["X-Request-ID"]
            evidence = error["approval_decision"]
            assert evidence["status"] == "REJECTED" and evidence["vehicle_id"] == "ev-1"
            assert evidence["arrival_soc_pct"] < evidence["reserve_pct"]
            status = await cluster.client.get(f"{base}/approvals/{request_id}")
            retry = await cluster.client.post(f"{base}/dispatches", json=body)
            assert status.status_code == 200 and retry.status_code == 202
            for response in (status, retry):
                assert response.json()["approval_status"] == "REJECTED"
                assert response.json()["revalidated_metrics"]["approval_decision"] == evidence
            restored = DispatchService(cluster.client, cluster.config, clock, repository)
            await restored.start()
            assert restored.approval_view(request_id)["revalidated_metrics"]["approval_decision"] == evidence
            assert not cluster.fleet.vehicles["ev-1"]["claims"]
            assert not cluster.facilities.holds

    asyncio.run(scenario())
