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
