"""Optional OSRM alternative selection; route switches happen before dispatch only.

Never repair a blocked route with straight lines, change an issued command,
or substitute an unchecked alternative. OSRM must be self-hosted or a permitted
provider configured server-side via OSRM_ROUTING_URL.
"""
import os
import httpx
from fastapi import HTTPException
from ev_fleet.common.travel import distance_km
from ev_fleet.common.parking import MAX_FACILITY_ROAD_ACCESS_KM
from ev_fleet.contracts.routing import RoadRoute
from ev_fleet.contracts.common import utcnow
from ev_fleet.rebalancing.traffic import assess_traffic


def _candidate(route, traffic, now):
    evaluation = assess_traffic(
        [point.model_dump() for point in route.geometry],
        route.distance_km, route.duration_seconds, traffic, now)
    if evaluation["blocked"]:
        return None
    minutes = evaluation["estimated_duration_seconds"]
    if minutes is None:
        minutes = route.duration_seconds
    return minutes + 180*(evaluation["risk"] or 0), evaluation


async def _osrm_alternatives(client, origin, destination, *, base_url):
    if not base_url:
        return []
    # Configuration is server-owned. Never construct network URLs from a user
    # request or LTA message; avoid SSRF via attacker-controlled destinations.
    coords = (f"{origin['longitude']},{origin['latitude']};"
              f"{destination['longitude']},{destination['latitude']}")
    try:
        response = await client.get(base_url.rstrip("/") + "/route/v1/driving/" + coords,
            params={"alternatives": "3", "steps": "false",
                    "overview": "full", "geometries": "geojson"},
            timeout=8, follow_redirects=False)
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != "Ok":
            return []
        results = []
        for row in payload.get("routes", [])[:4]:
            points = row["geometry"]["coordinates"]
            geometry = [{"latitude": float(p[1]), "longitude": float(p[0])} for p in points]
            results.append(RoadRoute(source="OSRM", fetched_at=utcnow(),
                geometry=geometry, distance_km=float(row["distance"])/1000,
                duration_seconds=float(row["duration"])))
        return results
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        # A missing alternate cannot override a blocked-route safety decision.
        return []


async def choose_traffic_route(client, primary, origin, destination, tolerance_km,
                               traffic, now, *, facility=False, osrm_url=None):
    """Choose the least-disrupted validated road route at dispatch approval.

    OneMap remains primary. OSRM offers distinct *candidate* geometries and
    must be explicitly configured. No output if a reported closure affects
    every available route. Caller must revalidate Fleet claim and SOC.
    """
    if not primary or traffic.get("source") not in ("LTA_DATAMALL", "SIMULATION"):
        return primary
    endpoint_limit = MAX_FACILITY_ROAD_ACCESS_KM if facility else tolerance_km
    base = _candidate(primary, traffic, now)
    choices = []
    if base is not None:
        choices.append((base[0], primary, base[1]))
    url = osrm_url if osrm_url is not None else os.getenv("OSRM_ROUTING_URL", "")
    if url:
        for alt in await _osrm_alternatives(client, origin, destination, base_url=url):
            if (distance_km(origin, alt.geometry[0].model_dump()) > .005
                    or distance_km(destination, alt.geometry[-1].model_dump()) > endpoint_limit):
                continue
            result = _candidate(alt, traffic, now)
            if result is not None:
                choices.append((result[0], alt, result[1]))
    if not choices:
        raise HTTPException(409, "TRAFFIC_ROUTE_BLOCKED_NO_SAFE_ALTERNATIVE")
    choices.sort(key=lambda row: (row[0], row[1].distance_km, row[1].source))
    _, selected, evaluation = choices[0]
    return selected.model_copy(update={
        "duration_seconds": evaluation["estimated_duration_seconds"] or selected.duration_seconds,
        "traffic_assessment": evaluation,
        "rerouted": selected.source != primary.source,
    })
