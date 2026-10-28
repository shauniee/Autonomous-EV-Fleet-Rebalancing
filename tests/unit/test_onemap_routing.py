"""Offline scaffolding checks; no credentials or OneMap requests are used."""
import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException

from ev_fleet.dispatch.routing import RoutingSettings, decode_polyline, driving_route
from ev_fleet.simulator.movement import Movement


def test_decode_documented_onemap_polyline_and_reject_truncation():
    points = decode_polyline("{u`GktxxR?G")
    assert points[0] == pytest.approx({"latitude": 1.31950, "longitude": 103.84214})
    assert points[1] == pytest.approx({"latitude": 1.31950, "longitude": 103.84218})
    with pytest.raises(ValueError):
        decode_polyline("?")


def test_disabled_mode_never_calls_provider_and_missing_token_fails_explicitly():
    assert asyncio.run(driving_route(None, RoutingSettings(), None, None, .05)) is None
    with pytest.raises(HTTPException) as error:
        asyncio.run(driving_route(None, RoutingSettings(provider="ONEMAP"), None, None, .05))
    assert error.value.detail == "ONEMAP_TOKEN_MISSING"


def test_simulator_follows_road_corner_using_route_duration_and_distance():
    origin = {"latitude": 1.30, "longitude": 103.80}
    corner = {"latitude": 1.30, "longitude": 103.81}
    destination = {"latitude": 1.31, "longitude": 103.81}
    route = {"geometry": [origin, corner, destination], "distance_km": 2, "duration_seconds": 200}
    command = {"command_id": str(uuid4()), "dispatch_id": str(uuid4()),
               "target_position": destination, "route_factor": 1.3,
               "energy_margin_factor": 1.15, "min_reserve_pct": 20, "route": route}
    moving = Movement(origin, 80)
    assert moving.accept(command)
    moving.start()
    moving.advance(50, 30)
    assert moving.position["latitude"] == pytest.approx(origin["latitude"])
    assert origin["longitude"] < moving.position["longitude"] < corner["longitude"]
    assert moving.distance == pytest.approx(.5)
    moving.advance(150, 30)
    assert moving.position == destination and moving.state == "ARRIVED"
    assert moving.soc_pct == pytest.approx(79.4)
