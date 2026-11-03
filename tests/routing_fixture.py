"""Deterministic route authority used ONLY by isolated offline tests.

The real Dispatch and EV code still require a OneMap road route. This fake
provides an explicit on-road contract fixture and never calls live OneMap.
It must not be imported by application code or configured in Kubernetes.
"""
from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.routing import RoadRoute


def fixture_route(origin, destination):
    distance = distance_km(origin, destination)
    return RoadRoute(
        source="ONEMAP",
        fetched_at=utcnow(),
        geometry=[origin, destination],
        distance_km=distance,
        duration_seconds=distance * 120.0,  # 30 km/h synthetic test pace
    )


async def fixture_driving_route(client, settings, origin, destination,
                                tolerance_km, *, facility=False):
    if origin is None:
        return None
    return fixture_route(origin, destination)
