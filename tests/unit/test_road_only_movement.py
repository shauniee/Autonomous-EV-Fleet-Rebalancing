"""OneMap road-only movement: no off-road origin/arrival connectors."""
import asyncio
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi import HTTPException

from ev_fleet.common.travel import distance_km
from ev_fleet.common.clock import Clock
from ev_fleet.contracts.routing import RoadRoute
from ev_fleet.contracts.dispatch import BatchApproval
from ev_fleet.contracts.facilities import ParkingHoldRequest, VehicleParking
from ev_fleet.dispatch.routing import RoutingSettings, driving_route, road_match_spawn
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.simulator.movement import Movement
from tests.dispatch_support import Cluster


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
        assert parked["parking"]["position"].model_dump() == access
        mover = Movement(access, 24)
        assert mover.park(VehicleParking.model_validate(parked).model_dump()["parking"])
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


def test_parking_refresh_keeps_verified_state_through_ten_second_failure_grace(monkeypatch):
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands

    async def scenario():
        tick = [100.0]
        monkeypatch.setattr(commands.time, "monotonic", lambda: tick[0])
        position = {"latitude": 1.436, "longitude": 103.786}
        parking = {
            "facility_id": "test-woodlands-parking", "name": "Test parking",
            "position": position, "zone_id": "woodlands", "source": "MOCK",
            "charging_allocated": False, "charging_power_kw": 7,
            "charging_target_soc_pct": 80,
        }
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-parking-refresh",
                          position, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)
        node.session = str(uuid4())
        node.initialized = True
        node.parking_verified = True
        node.parking_verified_at = tick[0]
        assert node.movement.park(parking)

        request_started = asyncio.Event()
        release_response = asyncio.Event()
        telemetry = []

        async def delayed_request(method, url, body=None):
            if method == "GET" and url.endswith("/parking"):
                request_started.set()
                await release_response.wait()
                return {"vehicle_id": node.vehicle_id, "parking": None, "pending": False}
            if method == "POST" and url.endswith("/telemetry"):
                telemetry.append(body)
                return {}
            raise AssertionError(f"Unexpected request: {method} {url}")

        node.request = delayed_request
        refresh = asyncio.create_task(node.refresh_parking())
        await request_started.wait()

        assert node.parking_verified is True
        assert node.movement.parking == parking
        await node.send_telemetry()
        assert telemetry[-1]["availability"] == "AVAILABLE"
        assert telemetry[-1]["parked_facility_id"] == parking["facility_id"]

        release_response.set()
        await refresh
        assert node.parking_verified is True
        assert node.movement.parking is None

        assert node.movement.park(parking)
        node.parking_verified = True

        async def failed_request(method, url, body=None):
            raise RuntimeError("Facilities response invalid")

        node.request = failed_request
        tick[0] = 110.0
        with pytest.raises(RuntimeError, match="Facilities response invalid"):
            await node.refresh_parking()
        assert node.parking_verified is True
        assert node.movement.parking == parking

        tick[0] = 110.001
        with pytest.raises(RuntimeError, match="Facilities response invalid"):
            await node.refresh_parking()
        assert node.parking_verified is False
        assert node.movement.parking is None

    asyncio.run(scenario())


def test_parking_refresh_has_no_grace_before_first_verification(monkeypatch):
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands

    async def scenario():
        monkeypatch.setattr(commands.time, "monotonic", lambda: 100.0)
        position = {"latitude": 1.436, "longitude": 103.786}
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-no-authority",
                          position, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)

        async def failed_request(method, url, body=None):
            raise RuntimeError("Facilities unavailable")

        node.request = failed_request
        with pytest.raises(RuntimeError, match="Facilities unavailable"):
            await node.refresh_parking()
        assert node.parking_verified is False
        assert node.movement.parking is None

    asyncio.run(scenario())


def test_valid_parking_response_resets_grace_and_pending_fails_closed(monkeypatch):
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands

    async def scenario():
        tick = [100.0]
        monkeypatch.setattr(commands.time, "monotonic", lambda: tick[0])
        position = {"latitude": 1.436, "longitude": 103.786}
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-reset-grace",
                          position, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)
        response = {"vehicle_id": node.vehicle_id, "parking": None, "pending": False}

        async def facilities_response(method, url, body=None):
            return response

        async def failed_request(method, url, body=None):
            raise RuntimeError("Facilities unavailable")

        node.request = facilities_response
        await node.refresh_parking()
        tick[0] = 109.0
        node.request = failed_request
        with pytest.raises(RuntimeError, match="Facilities unavailable"):
            await node.refresh_parking()
        assert node.parking_verified is True

        tick[0] = 109.0
        node.request = facilities_response
        await node.refresh_parking()
        tick[0] = 118.5
        node.request = failed_request
        with pytest.raises(RuntimeError, match="Facilities unavailable"):
            await node.refresh_parking()
        assert node.parking_verified is True

        response["pending"] = True
        node.request = facilities_response
        await node.refresh_parking()
        assert node.parking_verified is False
        tick[0] = 118.6
        node.request = failed_request
        with pytest.raises(RuntimeError, match="Facilities unavailable"):
            await node.refresh_parking()
        assert node.parking_verified is False

    asyncio.run(scenario())


def test_expired_parking_grace_fails_closed_before_next_refresh(monkeypatch):
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands

    async def scenario():
        tick = [100.0]
        monkeypatch.setattr(commands.time, "monotonic", lambda: tick[0])
        position = {"latitude": 1.436, "longitude": 103.786}
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-expired-grace",
                          position, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)
        node.session = str(uuid4())
        node.initialized = True
        telemetry = []

        async def request(method, url, body=None):
            if method == "GET" and url.endswith("/parking"):
                return {"vehicle_id": node.vehicle_id, "parking": None, "pending": False}
            if method == "POST" and url.endswith("/telemetry"):
                telemetry.append(body)
                return {}
            raise AssertionError(f"Unexpected request: {method} {url}")

        node.request = request
        await node.refresh_parking()
        tick[0] = 110.001
        await node.send_telemetry()
        assert telemetry[-1]["availability"] == "UNAVAILABLE"
        assert node.parking_verified is False

    asyncio.run(scenario())


def test_expired_parking_grace_stops_charging_without_refresh(monkeypatch):
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands

    class StopAfterTick(Exception):
        pass

    async def scenario():
        tick = [100.0]
        monkeypatch.setattr(commands.time, "monotonic", lambda: tick[0])
        position = {"latitude": 1.436, "longitude": 103.786}
        parking = {
            "facility_id": "test-charger", "name": "Test charger", "position": position,
            "zone_id": "woodlands", "source": "MOCK", "charging_allocated": True,
            "charging_power_kw": 50, "charging_target_soc_pct": 80, "cable_status": "CONNECTED",
        }
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-expired-charge",
                          position, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)

        async def parking_response(method, url, body=None):
            return {"vehicle_id": node.vehicle_id, "parking": parking, "pending": False}

        node.request = parking_response
        await node.refresh_parking()
        assert node.movement.charging_state == "CHARGING"
        sleep_calls = [0]

        async def one_tick(_seconds):
            if sleep_calls[0]:
                raise StopAfterTick
            sleep_calls[0] += 1
            tick[0] = 110.001

        monkeypatch.setattr(commands.asyncio, "sleep", one_tick)
        with pytest.raises(StopAfterTick):
            await node.movement_loop()
        assert node.movement.soc_pct == 70
        assert node.movement.charging_state == "NOT_CHARGING"
        assert node.parking_verified is False

    asyncio.run(scenario())


def test_relocation_invalidates_origin_parking_grace(monkeypatch):
    from ev_fleet.contracts.common import utcnow
    from ev_fleet.simulator.commands import MovingNode
    from ev_fleet.simulator import commands
    from tests.routing_fixture import fixture_route

    async def scenario():
        tick = [100.0]
        monkeypatch.setattr(commands.time, "monotonic", lambda: tick[0])
        origin = {"latitude": 1.436, "longitude": 103.786}
        destination = {"latitude": 1.437, "longitude": 103.787}
        node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-relocation-grace",
                          origin, 70, facilities_url="http://facilities.test",
                          facilities_grace_seconds=10)
        node.session = str(uuid4())
        node.initialized = True
        node.ready.set()
        command = {
            "dispatch_id": str(uuid4()), "command_id": str(uuid4()),
            "vehicle_id": node.vehicle_id, "fleet_instance_id": str(uuid4()),
            "target_zone_id": "woodlands", "target_position": destination,
            "issued_at": utcnow().isoformat(), "min_reserve_pct": 20,
            "route_factor": 1.3, "energy_margin_factor": 1.15,
            "route": fixture_route(origin, destination).model_dump(mode="json"),
        }
        phase = ["dispatch"]
        telemetry = []

        async def request(method, url, body=None):
            if method == "GET" and url.endswith("/parking"):
                if phase[0] == "destination-failure":
                    raise RuntimeError("Facilities unavailable at destination")
                return {"vehicle_id": node.vehicle_id, "parking": None, "pending": False}
            if method == "GET" and url.endswith("/commands"):
                return {"commands": [command]}
            if method == "POST" and url.endswith("/acknowledgements"):
                return {}
            if method == "GET" and f"/dispatches/{command['dispatch_id']}" in url:
                return {"state": "SUCCEEDED" if phase[0] == "destination-failure" else "IN_PROGRESS",
                        "claim_held": phase[0] != "destination-failure"}
            if method == "POST" and url.endswith("/events"):
                return {}
            if method == "POST" and url.endswith("/telemetry"):
                telemetry.append(body)
                return {}
            raise AssertionError(f"Unexpected request: {method} {url}")

        node.request = request
        await node.command_step()
        assert node.parking_verified is False
        assert node.parking_verified_at is None

        node.movement.state = "ARRIVED"
        node.movement.position = destination.copy()
        phase[0] = "destination-failure"
        tick[0] = 102.0
        with pytest.raises(RuntimeError, match="Facilities unavailable at destination"):
            await node.command_step()
        await node.send_telemetry()
        assert telemetry[-1]["availability"] == "UNAVAILABLE"

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
            assert parked["parking"]["position"].model_dump() == road_end
    asyncio.run(scenario())


def test_moving_ev_registers_at_road_before_first_fleet_telemetry(monkeypatch):
    from ev_fleet.dispatch import routing
    from ev_fleet.simulator.commands import MovingNode

    async def scenario():
        # MovingNode uses a real UTC timestamp. Test with a real wall clock,
        # not the fixed historical FakeClock fixture.
        async with Cluster(Clock()) as cluster:
            cluster.dispatch.routing = RoutingSettings(provider="ONEMAP", access_token="test-token")
            offroad = {"latitude": 1.35113, "longitude": 103.85}
            road = {"latitude": 1.35, "longitude": 103.85}
            calls = []
            async def fake_route(_client, _settings, origin, target):
                calls.append(origin)
                return road_route(road, target)

            monkeypatch.setattr(routing, "_onemap_route", fake_route)
            node = MovingNode("http://fleet.test", "http://dispatch.test", "ev-road-start",
                              offroad, 70, client=cluster.client)
            await node.send_telemetry()
            state = await cluster.fleet.get("ev-road-start")
            assert state["telemetry"]["position"] == road
            assert state["telemetry"]["soc_pct"] == 70
            assert node.movement.position == road
            assert len(calls) == 1  # Once during initialisation, never per telemetry.
            await node.send_telemetry()
            assert len(calls) == 1
            assert state["active_claim"] is None

    asyncio.run(scenario())


def test_restart_restores_persisted_occupied_parking_before_telemetry():
    from ev_fleet.simulator.commands import MovingNode

    async def scenario():
        async with Cluster(Clock()) as cluster:
            vehicle_id = "ev-restart-parked"
            site = BY_ID["mock-punggol-01"]
            parking_position = site.position.model_dump()
            restarted_position = {
                **parking_position,
                "latitude": parking_position["latitude"] + .001,
            }
            assert distance_km(restarted_position, parking_position) > .05
            await cluster.vehicle(vehicle_id, soc=72)
            await cluster.telemetry(vehicle_id, soc=72, position=restarted_position)
            dispatch_id = uuid4()
            await cluster.facilities.reserve(site.facility_id, ParkingHoldRequest(
                dispatch_id=dispatch_id,
                vehicle_id=vehicle_id,
                expected_zone_id=site.zone_id,
                expected_position=parking_position,
                access_position=parking_position,
                requires_charging=False,
            ))
            await cluster.facilities.change(dispatch_id, "OCCUPIED")

            node = MovingNode(
                "http://fleet.test",
                "http://dispatch.test",
                vehicle_id,
                restarted_position,
                72,
                client=cluster.client,
                facilities_url="http://facilities.test",
                road_match_initial=False,
            )
            await node.send_telemetry()

            state = await cluster.fleet.get(vehicle_id)
            assert node.movement.position == parking_position
            assert state["telemetry"]["position"] == parking_position
            assert state["telemetry"]["availability"] == "AVAILABLE"
            assert state["telemetry"]["parked_facility_id"] == site.facility_id

    asyncio.run(scenario())


def test_restart_repairs_legacy_occupied_parking_access_once_before_telemetry(monkeypatch):
    from ev_fleet.dispatch import routing
    from ev_fleet.simulator.commands import MovingNode

    async def scenario():
        async with Cluster(Clock()) as cluster:
            vehicle_id = "ev-legacy-parked"
            site = BY_ID["mock-punggol-01"]
            centre = site.position.model_dump()
            road_access = {
                **centre,
                "latitude": centre["latitude"] + .00055,
            }
            assert .05 < distance_km(centre, road_access) < .15
            await cluster.vehicle(vehicle_id, soc=72)
            await cluster.telemetry(vehicle_id, soc=72, position=centre)
            dispatch_id = uuid4()
            await cluster.facilities.reserve(site.facility_id, ParkingHoldRequest(
                dispatch_id=dispatch_id,
                vehicle_id=vehicle_id,
                expected_zone_id=site.zone_id,
                expected_position=centre,
                requires_charging=False,
            ))
            await cluster.facilities.change(dispatch_id, "OCCUPIED")
            cluster.dispatch.routing = RoutingSettings(
                provider="ONEMAP", access_token="test-token")
            route_calls = []

            async def fake_route(_client, _settings, origin, target):
                route_calls.append((origin, target))
                return road_route(road_access, target)

            monkeypatch.setattr(routing, "_onemap_route", fake_route)
            first = MovingNode(
                "http://fleet.test", "http://dispatch.test", vehicle_id,
                centre, 72, client=cluster.client,
                facilities_url="http://facilities.test")
            await first.send_telemetry()

            repaired = await cluster.facilities.vehicle_parking(vehicle_id)
            fleet_state = await cluster.fleet.get(vehicle_id)
            assert repaired["access_position_missing"] is False
            assert repaired["parking"]["position"].model_dump() == road_access
            assert first.movement.position == road_access
            assert fleet_state["telemetry"]["position"] == road_access
            assert len(route_calls) == 1

            restarted = MovingNode(
                "http://fleet.test", "http://dispatch.test", vehicle_id,
                centre, 72, client=cluster.client,
                facilities_url="http://facilities.test")
            await restarted.send_telemetry()
            assert restarted.movement.position == road_access
            assert len(route_calls) == 1

    asyncio.run(scenario())


def test_reject_route_start_that_would_create_an_offroad_connector():
    origin = {"latitude": 1.35, "longitude": 103.85}
    road_start = {"latitude": 1.35113, "longitude": 103.85}
    road_end = {"latitude": 1.352, "longitude": 103.85}
    route = road_route(road_start, road_end)
    cmd = {
        "command_id": str(uuid4()), "dispatch_id": str(uuid4()),
        "target_position": road_end, "route_factor": 1.3,
        "energy_margin_factor": 1.15, "min_reserve_pct": 20,
        "route": route.model_dump(mode="json"),
    }
    mover = Movement(origin, 70)
    with pytest.raises(ValueError, match="road-aligned"):
        mover.accept(cmd)
    assert mover.position == origin
    assert mover.command is None
