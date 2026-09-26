import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.dispatch import ClaimRequest, ClaimRelease
from tests.phase2_support import Cluster


def test_concurrent_claims_and_idempotency(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            vehicle = await cluster.fleet.get("ev-1")
            body = ClaimRequest(command_id=uuid4(), expected_instance_id=cluster.fleet.instance_id,
                expected_version=vehicle["state_version"], target_zone_id="cbd",
                target_position={"latitude": 1.283, "longitude": 103.8513},
                approve_before=clock.now() + timedelta(seconds=10), min_reserve_pct=20,
                route_factor=1.3, energy_margin_factor=1.15)
            ids = [uuid4(), uuid4()]
            results = await asyncio.gather(*(cluster.fleet.claim("ev-1", key, body) for key in ids), return_exceptions=True)
            assert sum(isinstance(item, HTTPException) for item in results) == 1
            winner = next(item for item in results if isinstance(item, dict))
            clock.advance(11)
            assert await cluster.fleet.claim("ev-1", winner["dispatch_id"], body) == winner
            await cluster.telemetry(availability="AVAILABLE")
            assert (await cluster.fleet.get("ev-1"))["effective_availability"] == "UNAVAILABLE"
            recommendation = await cluster.recommend()
            assert recommendation["selected_vehicle_id"] is None
    asyncio.run(scenario())


def test_arrival_release_requires_matching_fresh_position(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            dispatch, _ = await cluster.approve()
            release = ClaimRelease(command_id=dispatch["command"]["command_id"], outcome="ARRIVED")
            with pytest.raises(HTTPException):
                await cluster.fleet.release("ev-1", dispatch["dispatch_id"], release)
            await cluster.telemetry(state="ARRIVED", dispatch_id=dispatch["dispatch_id"])
            with pytest.raises(HTTPException):
                await cluster.fleet.release("ev-1", dispatch["dispatch_id"], release)
            await cluster.telemetry(state="ARRIVED", dispatch_id=dispatch["dispatch_id"], position=dispatch["command"]["target_position"])
            released = await cluster.fleet.release("ev-1", dispatch["dispatch_id"], release)
            assert released == await cluster.fleet.release("ev-1", dispatch["dispatch_id"], release)
            state = await cluster.fleet.get("ev-1")
            assert state["current_zone_id"] == "cbd" and state["active_claim"] is None
            assert state["effective_availability"] == "UNAVAILABLE"
            await cluster.telemetry(position=dispatch["command"]["target_position"])
            assert (await cluster.fleet.get("ev-1"))["effective_availability"] == "AVAILABLE"
    asyncio.run(scenario())


def test_fleet_rechecks_soc_under_claim_lock(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle(soc=20)
            vehicle = await cluster.fleet.get("ev-1")
            body = ClaimRequest(command_id=uuid4(), expected_instance_id=cluster.fleet.instance_id,
                expected_version=vehicle["state_version"], target_zone_id="cbd",
                target_position={"latitude": 1.283, "longitude": 103.8513},
                approve_before=clock.now() + timedelta(seconds=10), min_reserve_pct=20,
                route_factor=1.3, energy_margin_factor=1.15)
            with pytest.raises(HTTPException) as caught:
                await cluster.fleet.claim("ev-1", uuid4(), body)
            assert caught.value.detail == "BELOW_OPERATIONAL_RESERVE"
    asyncio.run(scenario())
