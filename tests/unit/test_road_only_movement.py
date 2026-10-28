"""OneMap road-only movement: no off-road origin/arrival connectors."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.routing import RoadRoute
from ev_fleet.contracts.dispatch import BatchApproval
from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.dispatch.routing import RoutingSettings, driving_route, road_match_spawn
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.simulator.movement import Movement
from tests.phase2_support import Cluster


def road_route(*points):
    return RoadRoute(
        fetched_at=datetime.now(timezone.utc), geometry=list(points),
        distance_km=max(.05, sum(distance_km(a, b) for a, b in zip(points, points[1:]))),
        duration_seconds=60,
    )


def test_facility_road_access_is_not_a_loosened_50m_arrival_tolerance(monkeypatch):
    from ev_fleet.dispatch import routing
    origin = {"latitude": 1.35, "longitude": 103.85}
    facility_centre = {"latitude": 1.351, "longitude": 103.852}
    road_end = {**facility_centre, "latitude": facility_centre["latitude"] + .00055}
    assert .05 < distance_km(facility_centre, road_end) < .15
    route = road_route(origin, road_end)

    async def fake_route(*args):
        return route

    monkeypatch.setattr(routing, "_onemap_route", fake_route)
    settings = RoutingSettings(provider="ONEMAP", access_token="test-token")
    result = asyncio.run(driving_route(None, settings, origin, facility_centre, .05, facility=True))
    assert result.geometry[-1].model_dump() == road_end
    with pytest.raises(HTTPException) as failed:
        asyncio.run(driving_route(None, settings, origin, facility_centre, .05))
    assert failed.value.detail == "ONEMAP_ENDPOINT_TOO_FAR"

    with pytest.raises(HTTPException) as failed_origin:
        asyncio.run(driving_route(None, settings, {**origin, "latitude": 1.35114}, facility_centre,
                                  .05, facility=True))
    assert failed_origin.value.detail == "ONEMAP_ORIGIN_NOT_ON_ROAD"


def test_far_facility_access_is_rejected_even_if_on_road(monkeypatch):
    from ev_fleet.dispatch import routing
    origin = {"latitude": 1.35, "longitude": 103.85}
    centre = {"latitude": 1.351, "longitude": 103.852}
    far = {**centre, "latitude": centre["latitude"] + .002}
    async def fake_route(*args):
        return road_route(origin, far)
    monkeypatch.setattr(routing, "_onemap_route", fake_route)
    with pytest.raises(HTTPException) as error:
        asyncio.run(driving_route(None, RoutingSettings(provider="ONEMAP",
            access_token="test-token"), origin, centre, .05, facility=True))
    assert error.value.detail == "ONEMAP_FACILITY_NO_ROAD_ACCESS"


def test_road_match_idle_spawn_once_before_journey(monkeypatch):
    from ev_fleet.dispatch import routing
    road = {"latitude": 1.35, "longitude": 103.85}
    offroad = {"latitude": 1.35113, "longitude": 103.85}
    assert .12 < distance_km(offroad, road) < .20
    calls = []
    async def fake_route(_client, _settings, start, end):
        calls.append((start, end))
        return road_route(road, end)
    monkeypatch.setattr(routing, "_onemap_route", fake_route)
    settings = RoutingSettings(provider="ONEMAP", access_token="test-token")
    result = asyncio.run(road_match_spawn(None, settings, offroad))
    assert result == {"position": road, "source": "ONEMAP"}
    assert len(calls) == 1
    assert asyncio.run(road_match_spawn(None, RoutingSettings(), offroad)) == {
        "position": offroad, "source": "SIMULATED",
    }


def test_no_road_match_outside_spawn_radius(monkeypatch):
    from ev_fleet.dispatch import routing
    origin = {"latitude": 1.35, "longitude": 103.85}
    far = {**origin, "latitude": origin["latitude"] + .003}
    async def fake_route(_client, _settings, start, end):
        return road_route(far, end)
    monkeypatch.setattr(routing, "_onemap_route", fake_route)
    with pytest.raises(HTTPException) as error:
        asyncio.run(road_match_spawn(None, RoutingSettings(provider="ONEMAP",
            access_token="test-token"), origin))
    assert error.value.detail == "ONEMAP_NO_SAFE_SPAWN_ROAD"


def test_parked_ev_stays_at_drivable_road_access_without_teleport():
    async def scenario():
        store = FacilitiesStore()
        site = BY_ID["mock-bishan-01"]
        centre = site.position.model_dump()
        access = {**centre, "latitude": centre["latitude"] + .00055}
        assert distance_km(centre, access) > .05
        dispatch_id = uuid4()
        hold = ParkingHoldRequest(dispatch_id=dispatch_id, vehicle_id="ev-road",
            expected_zone_id=site.zone_id, expected_position=centre,
            access_position=access, requires_charging=True)
        await store.reserve(site.facility_id, hold)
        await store.change(dispatch_id, "OCCUPIED")
        parked = await store.vehicle_parking("ev-road")
        assert parked["parking"]["position"] == access
        mover = Movement(access, 24)
        assert mover.park(parked["parking"])
        assert mover.position == access
        # The facility centre remains off-road and is NOT the vehicle position.
        assert distance_km(mover.position, centre) > .05
        with pytest.raises(HTTPException) as error:
            await store.reserve(site.facility_id, ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id="ev-far",
                expected_zone_id=site.zone_id, expected_position=centre,
                access_position={**centre, "latitude": centre["latitude"] + .003},
                requires_charging=True))
        assert error.value.detail == "PARKING_ROAD_ACCESS_TOO_FAR"
    asyncio.run(scenario())


def test_charging_approval_claims_access_road_and_not_facility_centre(clock, monkeypatch):
    from ev_fleet.dispatch import service
    async def scenario():
        async with Cluster(clock) as cluster:
            site = BY_ID["mock-bishan-01"]
            centre = site.position.model_dump()
            road_end = {**centre, "latitude": centre["latitude"] + .00055}
            start = {**centre, "longitude": centre["longitude"] - .001}
            await cluster.vehicle("ev-road", soc=28)
            await cluster.telemetry("ev-road", soc=28, position=start)
            async def fake_route(_client, _routing, origin, destination, tolerance, *, facility=False):
                assert facility is True
                assert origin == start and destination == centre
                assert tolerance == .05
                return road_route(start, road_end)
            monkeypatch.setattr(service, "driving_route", fake_route)
            proposal = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": site.zone_id, "quantity": 1, "purpose": "CHARGE",
                      "vehicle_id": "ev-road", "facility_id": site.facility_id})
            assert proposal.status_code == 201, proposal.text
            assert proposal.json()["recommended_count"] == 1
            approved = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(), batch_recommendation_id=proposal.json()["batch_recommendation_id"]))
            assert approved["status"] == "APPROVED", approved
            claim = (await cluster.fleet.get("ev-road"))["active_claim"]
            assert claim["target_position"] == road_end
            assert distance_km(claim["target_position"], centre) > .05
            pending = await cluster.facilities.get_hold(approved["items"][0]["dispatch_id"])
            assert pending["status"] == "RESERVED"
            await cluster.facilities.change(approved["items"][0]["dispatch_id"], "OCCUPIED")
            parked = await cluster.facilities.vehicle_parking("ev-road")
            assert parked["parking"]["position"] == road_end
    asyncio.run(scenario())
