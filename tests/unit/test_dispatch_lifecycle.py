import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.dispatch import DispatchApproval, CommandAcknowledgement, DispatchEvent
from tests.dispatch_support import Cluster


def ack(dispatch, accepted=True):
    return CommandAcknowledgement(command_id=dispatch["command"]["command_id"], vehicle_id="ev-1", accepted=accepted)


def test_recommendation_does_not_dispatch_and_approval_is_idempotent(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            before = await cluster.fleet.get("ev-1")
            rec = await cluster.recommend()
            assert not cluster.dispatch.store.records
            assert await cluster.fleet.get("ev-1") == before
            dispatch, body = await cluster.approve(rec)
            assert dispatch["state"] == "PENDING" and dispatch["claim_held"]
            assert (await cluster.dispatch.approve(body))["dispatch_id"] == dispatch["dispatch_id"]
            for conflicting in [DispatchApproval(request_id=body.request_id, recommendation_id=uuid4()),
                                DispatchApproval(request_id=uuid4(), recommendation_id=body.recommendation_id)]:
                with pytest.raises(HTTPException) as caught:
                    await cluster.dispatch.approve(conflicting)
                assert caught.value.status_code == 409
    asyncio.run(scenario())


def test_concurrent_approvals_only_one_claim(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            recs = [await cluster.recommend(), await cluster.recommend()]
            results = await asyncio.gather(*(cluster.approve(rec) for rec in recs), return_exceptions=True)
            assert sum(isinstance(item, tuple) for item in results) == 1
            assert sum(record.claimed for record in cluster.dispatch.store.records.values()) == 1
    asyncio.run(scenario())


def test_lifecycle_events_duplicates_and_arrival(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            first = await cluster.dispatch.commands("ev-1")
            assert first == await cluster.dispatch.commands("ev-1")
            accepted = await cluster.dispatch.acknowledge(key, ack(dispatch))
            assert accepted["state"] == "ACKNOWLEDGED"
            assert await cluster.dispatch.acknowledge(key, ack(dispatch)) == accepted
            with pytest.raises(HTTPException):
                await cluster.dispatch.acknowledge(key, ack(dispatch, False))
            event = DispatchEvent(command_id=dispatch["command"]["command_id"], vehicle_id="ev-1",
                sequence=0, observed_at=clock.now(), kind="PROGRESS", distance_travelled_km=1)
            progress = await cluster.dispatch.event(key, event)
            assert progress["state"] == "IN_PROGRESS"
            assert await cluster.dispatch.event(key, event) == progress
            with pytest.raises(HTTPException):
                await cluster.dispatch.event(key, event.model_copy(update={"distance_travelled_km": 2}))
            arrival = event.model_copy(update={"sequence": 1, "kind": "ARRIVED", "distance_travelled_km": 10})
            assert (await cluster.dispatch.event(key, arrival))["state"] == "ARRIVAL_PENDING"
            with pytest.raises(HTTPException):
                await cluster.dispatch.event(key, event.model_copy(update={"sequence": 2, "distance_travelled_km": 11}))
            await cluster.telemetry(state="ARRIVED", dispatch_id=key, position=dispatch["command"]["target_position"], soc=77)
            result = await cluster.dispatch.get(key)
            assert result["state"] == "SUCCEEDED" and not result["claim_held"]
            assert result["arrival_soc_pct"] == 77
            assert (await cluster.dispatch.event(key, arrival))["state"] == "SUCCEEDED"
    asyncio.run(scenario())


def test_explicit_rejection_releases_only_with_matching_idle_telemetry(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            await cluster.dispatch.commands("ev-1")
            result = await cluster.dispatch.acknowledge(key, ack(dispatch, False))
            assert result["claim_held"]
            await cluster.telemetry(dispatch_id=key, state="IDLE", soc=10)
            result = await cluster.dispatch.get(key)
            assert result["state"] == "REJECTED" and not result["claim_held"]
    asyncio.run(scenario())


def test_unconfirmed_admission_blocks_overlap_but_allows_another_vehicle(clock, monkeypatch):
    monkeypatch.setenv("CRDS_MODE", "DISABLED")

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1")
            first = await cluster.recommend()
            overlap = await cluster.recommend()
            cluster.failures.append({"method": "PUT", "path": "/claims/", "action": "offline"})
            uncertain, body = await cluster.approve(first)
            assert uncertain["state"] == "UNKNOWN"
            status_url = f"http://dispatch.test/api/v1/approvals/{body.request_id}"
            status = await cluster.client.get(status_url)
            assert status.json()["approval_status"] == "UNKNOWN"
            assert (await cluster.client.post(status_url + "/close")).status_code == 409

            with pytest.raises(HTTPException, match="APPROVAL_RESOURCE_UNRESOLVED"):
                await cluster.approve(overlap)

            await cluster.telemetry("ev-1", availability="UNAVAILABLE")
            await cluster.vehicle("ev-2")
            admitted, _ = await cluster.approve()
            assert admitted["vehicle_id"] == "ev-2" and admitted["claim_held"]
            assert (await cluster.client.get(status_url)).json()["approval_status"] == "UNKNOWN"

    asyncio.run(scenario())
