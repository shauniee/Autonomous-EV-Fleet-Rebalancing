"""Batch approval under normal progress, without live routing or databases."""
import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.contracts.demand import DemandUpdate
from ev_fleet.contracts.dispatch import (
    BatchApproval, CommandAcknowledgement, DispatchEvent,
)
from ev_fleet.contracts.vehicles import TelemetryUpdate
from ev_fleet.simulator.movement import Movement
from tests.dispatch_support import Cluster


@asynccontextmanager
async def moving_cluster(clock, source):
    async with Cluster(clock, recommendation_ttl=120) as cluster:
        site = cluster.facilities.catalogue["mock-cbd-03"]
        cluster.facilities.catalogue[site.facility_id] = site.model_copy(
            update={"parking_capacity": 12, "parking_available": 12})
        for zone_id in cluster.demand.zones:
            zone = await cluster.demand.get(zone_id)
            await cluster.demand.update(zone_id, "demand", DemandUpdate(
                expected_version=zone["demand_version"],
                observed_at=clock.now(),
                desired_available_vehicles=12 if zone_id == "cbd" else 0))
        for index in range(9):
            await cluster.vehicle(f"batch-progress-{index}", soc=90)
        active, _ = await cluster.approve(await cluster.recommend())
        vehicle_id = active["vehicle_id"]
        command = (await cluster.dispatch.commands(vehicle_id))["commands"][0]
        movement = Movement(command["route"]["geometry"][0], 90)
        movement.accept(command)
        await cluster.dispatch.acknowledge(
            active["dispatch_id"], CommandAcknowledgement(
                command_id=command["command_id"], vehicle_id=vehicle_id,
                accepted=True))
        movement.start()
        event_sequence = -1

        async def progress(kind="PROGRESS"):
            nonlocal event_sequence
            movement.advance(1, 30)
            clock.advance(.1)  # The existing 10x simulation clock.
            cluster.sequences[vehicle_id] += 1
            await cluster.fleet.telemetry(vehicle_id, TelemetryUpdate(
                session_id=cluster.sessions[vehicle_id],
                sequence=cluster.sequences[vehicle_id],
                observed_at=clock.now(), **movement.telemetry()))
            if source == "event" or event_sequence < 0 or kind != "PROGRESS":
                event_sequence += 1
                await cluster.dispatch.event(
                    active["dispatch_id"], DispatchEvent(
                        command_id=command["command_id"],
                        vehicle_id=vehicle_id, sequence=event_sequence,
                        observed_at=clock.now(), kind=kind,
                        distance_travelled_km=movement.distance,
                        reason="SIMULATED_FAILURE" if kind == "FAILED" else None))
            else:
                record = cluster.dispatch.store.get(active["dispatch_id"])
                async with record.lock:
                    await cluster.dispatch.refresh(record)

        await progress()  # Establish IN_PROGRESS before testing steady motion.
        yield cluster, active, progress


def enable_crds(monkeypatch):
    monkeypatch.setenv("CRDS_MODE", "ACTIVE")
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch.test")


@pytest.mark.parametrize("source", ["event", "refresh"])
def test_six_long_routes_approve_while_another_ev_moves(clock, monkeypatch, source):
    enable_crds(monkeypatch)

    async def scenario():
        async with moving_cluster(clock, source) as (cluster, active, progress):
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 6})
            assert response.status_code == 201, response.text
            batch = response.json()
            assert batch["recommended_count"] == 6
            assert all(a["distance_km"] > 5 for a in batch["assignments"])
            original_route = cluster.route
            revalidations = 0

            async def respond(request):
                nonlocal revalidations
                reply = await original_route(request)
                if request.url.path == "/api/v1/crds/revalidate":
                    revalidations += 1
                    await progress()
                return reply

            monkeypatch.setattr(cluster.client, "_transport",
                                httpx.MockTransport(respond))
            approval = BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"])
            result = await cluster.dispatch.approve_batch(approval)
            assert result["status"] == "APPROVED", result
            assert len(result["items"]) == 6
            assert revalidations == 6  # Normal motion needs no planning retry.
            for item in result["items"]:
                record = cluster.dispatch.store.get(item["dispatch_id"])
                assert record.claimed
                assert record.data["command"]["route"]["distance_km"] > 5
            holds = len(cluster.facilities.holds)
            assert await cluster.dispatch.approve_batch(approval) == result
            assert len(cluster.facilities.holds) == holds
            assert (await cluster.fleet.get(active["vehicle_id"]))["active_claim"]
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["event", "refresh"])
@pytest.mark.parametrize("kind", ["FAILED", "ARRIVED"])
def test_progress_keeps_commitment_revision_but_failure_changes_it(clock, monkeypatch, source, kind):
    enable_crds(monkeypatch)

    async def scenario():
        async with moving_cluster(clock, source) as (cluster, active, progress):
            before = await cluster.dispatch.planning_snapshot()
            await progress()
            after = await cluster.dispatch.planning_snapshot()
            assert after.revision == before.revision
            old = next(d for d in before.dispatches
                       if str(d.dispatch_id) == active["dispatch_id"])
            new = next(d for d in after.dispatches
                       if str(d.dispatch_id) == active["dispatch_id"])
            assert new.distance_travelled_km > old.distance_travelled_km
            await progress(kind)
            assert cluster.dispatch.planning_revision > after.revision
    asyncio.run(scenario())


def test_progress_fix_does_not_approve_a_depleted_batch_vehicle(clock, monkeypatch):
    enable_crds(monkeypatch)

    async def scenario():
        async with moving_cluster(clock, "event") as (cluster, _, progress):
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 6})
            batch = response.json()
            depleted = batch["assignments"][0]["vehicle_id"]
            original_route = cluster.route
            changed = False

            async def respond(request):
                nonlocal changed
                reply = await original_route(request)
                if request.url.path == "/api/v1/crds/revalidate":
                    await progress()
                    if not changed:
                        changed = True
                        await cluster.telemetry(depleted, soc=1)
                return reply

            monkeypatch.setattr(cluster.client, "_transport",
                                httpx.MockTransport(respond))
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"]))
            assert result["status"] == "PARTIAL", result
            bad = next(i for i in result["items"] if i["vehicle_id"] == depleted)
            assert bad["approval_status"] == "REJECTED"
            assert (await cluster.fleet.get(depleted))["active_claim"] is None
            assert sum(i["approval_status"] == "APPROVED"
                       for i in result["items"]) == 5
    asyncio.run(scenario())


def test_progress_fix_preserves_batch_expiry(clock, monkeypatch):
    enable_crds(monkeypatch)

    async def scenario():
        async with moving_cluster(clock, "event") as (cluster, _, _):
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 6})
            batch = response.json()
            clock.advance(121)
            with pytest.raises(HTTPException, match="BATCH_EXPIRED|RECOMMENDATION_MISSING_OR_EXPIRED"):
                await cluster.dispatch.approve_batch(BatchApproval(
                    request_id=uuid4(),
                    batch_recommendation_id=batch["batch_recommendation_id"]))
    asyncio.run(scenario())


@pytest.mark.parametrize("source", ["event", "refresh"])
def test_sibling_startup_does_not_reject_the_next_batch_child(clock, monkeypatch, source):
    enable_crds(monkeypatch)

    async def scenario():
        async with moving_cluster(clock, source) as (cluster, active, _):
            response = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 6})
            batch = response.json()
            assert batch["recommended_count"] == 6
            original_route = cluster.route
            stages = []
            revalidations = 0

            async def respond(request):
                nonlocal revalidations
                reply = await original_route(request)
                if request.url.path != "/api/v1/crds/revalidate":
                    return reply
                revalidations += 1
                sibling = next((record for record in cluster.dispatch.store.records.values()
                    if record.data["dispatch_id"] != active["dispatch_id"]
                    and record.claimed
                    and record.data["state"] in ("PENDING", "SENT", "ACKNOWLEDGED")), None)
                if sibling is None:
                    return reply
                vehicle_id = sibling.data["vehicle_id"]
                command = sibling.data["command"]
                dispatch_id = sibling.data["dispatch_id"]
                if sibling.sent_tick is None:
                    await cluster.dispatch.commands(vehicle_id)
                    stages.append("SENT")
                elif sibling.ack is None:
                    await cluster.dispatch.acknowledge(
                        dispatch_id, CommandAcknowledgement(
                            command_id=command["command_id"],
                            vehicle_id=vehicle_id, accepted=True))
                    stages.append("ACKNOWLEDGED")
                else:
                    movement = Movement(command["route"]["geometry"][0], 90)
                    movement.accept(command)
                    movement.start()
                    movement.advance(1, 30)
                    clock.advance(.1)
                    cluster.sequences[vehicle_id] += 1
                    await cluster.fleet.telemetry(vehicle_id, TelemetryUpdate(
                        session_id=cluster.sessions[vehicle_id],
                        sequence=cluster.sequences[vehicle_id],
                        observed_at=clock.now(), **movement.telemetry()))
                    if source == "event":
                        await cluster.dispatch.event(dispatch_id, DispatchEvent(
                            command_id=command["command_id"],
                            vehicle_id=vehicle_id, sequence=0,
                            observed_at=clock.now(), kind="PROGRESS",
                            distance_travelled_km=movement.distance))
                    else:
                        async with sibling.lock:
                            await cluster.dispatch.refresh(sibling)
                    stages.append("IN_PROGRESS")
                return reply

            monkeypatch.setattr(cluster.client, "_transport", httpx.MockTransport(respond))
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"]))
            assert result["status"] == "APPROVED", result
            assert len(result["items"]) == 6
            assert revalidations == 6
            assert {"SENT", "ACKNOWLEDGED", "IN_PROGRESS"} <= set(stages)
    asyncio.run(scenario())
