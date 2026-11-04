"""Keep the physical road-route contract strict despite offline test stubs."""
import asyncio
from uuid import uuid4

from ev_fleet.dispatch.routing import RoutingSettings, driving_route
from ev_fleet.simulator.movement import Movement
from tests.routing_fixture import fixture_driving_route, fixture_route


ORIGIN = {"latitude": 1.35, "longitude": 103.8513}
TARGET = {"latitude": 1.283, "longitude": 103.8513}


def test_legacy_simulated_routing_is_not_an_authoritative_road_route():
    # Production deliberately refuses an approval without an actual road path.
    result = asyncio.run(driving_route(
        None, RoutingSettings(provider="SIMULATED"), ORIGIN, TARGET, .05))
    assert result is None


def test_moving_ev_still_rejects_missing_route():
    movement = Movement(ORIGIN, 80)
    command = {
        "command_id": str(uuid4()), "dispatch_id": str(uuid4()),
        "target_position": TARGET, "min_reserve_pct": 20,
        "route_factor": 1.3, "energy_margin_factor": 1.15,
    }
    assert movement.accept(command) is False
    assert movement.reason == "ONEMAP_ROUTE_REQUIRED"
    assert movement.state == "IDLE"


def test_offline_fixture_builds_explicit_road_contract():
    route = asyncio.run(fixture_driving_route(
        None, RoutingSettings(provider="SIMULATED"), ORIGIN, TARGET, .05))
    assert route == fixture_route(ORIGIN, TARGET).model_copy(
        update={"fetched_at": route.fetched_at})
    assert route.source == "ONEMAP"
    assert route.geometry[0].model_dump() == ORIGIN
    assert route.geometry[-1].model_dump() == TARGET
    assert route.distance_km > 0


def test_moving_ev_accepts_only_explicit_road_routed_osrm_alternative():
    origin, target = ORIGIN, TARGET
    route = fixture_route(origin, target).model_copy(update={"source": "OSRM"})
    command = {
        "command_id": str(uuid4()), "dispatch_id": str(uuid4()),
        "target_position": target, "min_reserve_pct": 20,
        "route_factor": 1.3, "energy_margin_factor": 1.15,
        "route": route.model_dump(mode="json"),
    }
    mover = Movement(origin, 80)
    assert mover.accept(command) is True
    mover.start()
    mover.advance(10, 30)
    assert mover.state in ("RELOCATING", "ARRIVED")

    unsupported = {**command, "command_id": str(uuid4()),
                   "route": {**command["route"], "source": "SIMULATED"}}
    rejected = Movement(origin, 80)
    assert rejected.accept(unsupported) is False
    assert rejected.reason == "ONEMAP_ROUTE_REQUIRED"
