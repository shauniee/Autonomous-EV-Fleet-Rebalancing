"""Optional OneMap REST adapter. Road endpoints are physical EV coordinates.

Road-only movement never introduces an off-road connector segment. Road matching
is an *idle simulator spawn-time* operation, never an in-flight correction.
"""
import os
import httpx
from fastapi import HTTPException
from pydantic import BaseModel, SecretStr
from typing import Literal

from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.routing import RoadRoute

# These are search limits, NOT Fleet's 50 m arrival tolerance.
MAX_INITIAL_ROAD_MATCH_KM = .20
MAX_FACILITY_ACCESS_KM = .15


class RoutingSettings(BaseModel):
    provider: Literal["SIMULATED", "ONEMAP"] = "SIMULATED"
    access_token: SecretStr = SecretStr("")

    @classmethod
    def from_env(cls):
        return cls(provider=os.getenv("ROUTING_PROVIDER", "SIMULATED").upper(),
                   access_token=os.getenv("ONEMAP_ACCESS_TOKEN", ""))


def decode_polyline(encoded):
    """Decode OneMap's precision-5 encoded polyline into WGS84 positions."""
    if not isinstance(encoded, str) or len(encoded) > 500000:
        raise ValueError("invalid geometry")
    position = [0, 0]
    points = []
    index = 0
    while index < len(encoded):
        for axis in range(2):
            value = shift = 0
            while True:
                if index >= len(encoded) or shift > 30:
                    raise ValueError("truncated geometry")
                digit = ord(encoded[index]) - 63
                index += 1
                if not 0 <= digit <= 63:
                    raise ValueError("invalid geometry character")
                value |= (digit & 31) << shift
                shift += 5
                if digit < 32:
                    break
            position[axis] += ~(value >> 1) if value & 1 else value >> 1
        points.append({"latitude": position[0] / 100000, "longitude": position[1] / 100000})
        if len(points) > 50000:
            raise ValueError("geometry too large")
    return points


async def _onemap_route(client, settings, origin, destination):
    if not settings.access_token.get_secret_value():
        raise HTTPException(503, "ONEMAP_TOKEN_MISSING")
    try:
        response = await client.get("https://www.onemap.gov.sg/api/public/routingsvc/route",
            params={"start": f"{origin['latitude']},{origin['longitude']}",
                    "end": f"{destination['latitude']},{destination['longitude']}", "routeType": "drive"},
            headers={"Authorization": settings.access_token.get_secret_value()},
            timeout=5, follow_redirects=False)
        if response.status_code in (401, 403):
            raise HTTPException(503, "ONEMAP_AUTH_FAILED")
        if response.status_code == 429:
            raise HTTPException(503, "ONEMAP_RATE_LIMITED")
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") != 0:
            raise HTTPException(409, "ONEMAP_NO_ROUTE")
        summary = payload["route_summary"]
        route = RoadRoute(fetched_at=utcnow(), geometry=decode_polyline(payload["route_geometry"]),
            distance_km=float(summary["total_distance"]) / 1000,
            duration_seconds=summary["total_time"], instructions=[
                {"text": str(step[9] or step[1] or step[0]), "distance_m": float(step[2])}
                for step in payload.get("route_instructions", [])])
        geometry = [point.model_dump() for point in route.geometry]
        length = sum(distance_km(a, b) for a, b in zip(geometry, geometry[1:]))
        if (route.distance_km > 0 and length <= 0) or (route.distance_km == 0 and length > .000001):
            raise ValueError("empty route path")
        return route
    except httpx.TimeoutException:
        raise HTTPException(504, "ONEMAP_TIMEOUT") from None
    except httpx.HTTPError:
        raise HTTPException(503, "ONEMAP_UNAVAILABLE") from None
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        raise HTTPException(503, "ONEMAP_INVALID_RESPONSE") from None


async def driving_route(client, settings, origin, destination, tolerance_km, *, facility=False):
    if settings.provider == "SIMULATED":
        return None
    route = await _onemap_route(client, settings, origin, destination)
    # An EV must already be on the road at journey start. Do not append a
    # connector or enlarge the strict origin/physical-arrival tolerance.
    if distance_km(origin, route.geometry[0].model_dump()) > tolerance_km:
        raise HTTPException(409, "ONEMAP_ORIGIN_NOT_ON_ROAD")
    # A facility centre is not a road entrance. Use OneMap's drive-route end
    # as the physical target only if it is reasonably close to that facility.
    # Road endpoint is stored in claim/command/parking hold, NOT animated from
    # the route endpoint to the geographic facility centre.
    limit = MAX_FACILITY_ACCESS_KM if facility else tolerance_km
    if distance_km(destination, route.geometry[-1].model_dump()) > limit:
        raise HTTPException(409, "ONEMAP_FACILITY_NO_ROAD_ACCESS" if facility
                            else "ONEMAP_ENDPOINT_TOO_FAR")
    return route


async def road_match_spawn(client, settings, origin):
    """Resolve an idle simulated EV's initial position to a OneMap drive road.

    This is deliberately separate from driving_route and never invoked after
    the EV has begun an active journey. Return an error instead of retaining a
    known off-road position or inserting an artificial road connector.
    """
    if settings.provider == "SIMULATED":
        return {"position": origin, "source": "SIMULATED"}
    # Multiple small probes increase the odds of a routable road without
    # silently expanding the maximum permissible initial placement offset.
    for offset in ((.002, 0), (0, .002), (-.002, 0)):
        target = {"latitude": origin["latitude"] + offset[0],
                  "longitude": origin["longitude"] + offset[1]}
        try:
            route = await _onemap_route(client, settings, origin, target)
        except HTTPException as exc:
            if exc.detail == "ONEMAP_NO_ROUTE":
                continue
            raise
        matched = route.geometry[0].model_dump()
        if distance_km(origin, matched) <= MAX_INITIAL_ROAD_MATCH_KM:
            return {"position": matched, "source": "ONEMAP"}
    raise HTTPException(409, "ONEMAP_NO_SAFE_SPAWN_ROAD")
