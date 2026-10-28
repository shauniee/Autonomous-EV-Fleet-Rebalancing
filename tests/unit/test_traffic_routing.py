"""Offline verification of LTA traffic feeds, ranking, alternatives, and safeguards."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.routing import RoadRoute
from ev_fleet.rebalancing.traffic import TrafficMonitor, assess_traffic, normalize, rank_cost
from ev_fleet.dispatch.traffic_routes import choose_traffic_route
from ev_fleet.common.settings import Settings
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.rebalancing.batch import rank_pairs
from tests.phase2_support import Cluster

START = {"latitude": 1.30, "longitude": 103.80}
END = {"latitude": 1.30, "longitude": 103.82}
NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)


def speed(road="FIRST ROAD", start=START, end=END, minimum=10, maximum=18):
    return {"id": "link-1", "road": road, "start": start,
            "end": end, "min_kph": minimum, "max_kph": maximum}


def incident(position=START, kind="Road Block"):
    return {"id": "incident-1", "type": kind.upper(), "position": position,
            "message": "Fixture event"}


def traffic(speeds=None, incidents=None, roadworks=None, status="FRESH"):
    return {"source": "LTA_DATAMALL", "status": status, "generated_at": NOW.isoformat(),
            "speeds": speeds or [], "incidents": incidents or [], "roadworks": roadworks or []}


def route(start=START, end=END, km=4):
    return RoadRoute(fetched_at=NOW, geometry=[start, end],
                     distance_km=km, duration_seconds=600)


def test_ingestion_normalises_and_rejects_invalid_records():
    valid = {"LinkID": 1, "RoadName": "First Road", "MinimumSpeed": "10",
             "MaximumSpeed": 18, "StartLat": 1.3, "StartLon": 103.8,
             "EndLat": 1.3, "EndLon": 103.82}
    assert normalize("speeds", [valid, {**valid, "StartLat": 10}]) == [speed()]
    assert normalize("incidents", [{"IncidentID": "i", "Type": "Road Block",
        "Latitude": 1.30, "Longitude": 103.8, "Message": "Road closed"}])[0]["type"] == "ROAD BLOCK"
    assert normalize("roadworks", [{"EventID": "w", "RoadName": "First Road",
        "StartDate": "2026-10-27", "EndDate": "2026-10-29"}])[0]["road"] == "FIRST ROAD"
    assert normalize("roadworks", [{"RoadName": "First Road", "StartDate": "bad", "EndDate": "2026-10-30"}]) == []


def test_traffic_monitor_caches_lta_pages_and_rejects_stale_missing_key():
    hits = []
    def handler(request):
        hits.append(request.url.path)
        assert request.headers["AccountKey"] == "test-secret"
        if request.url.path.endswith("TrafficIncidents"):
            return httpx.Response(200, json={"value": [{"IncidentID":"i","Type":"Road Block",
                "Latitude": 1.3, "Longitude": 103.8, "Message":"Closed"}]})
        return httpx.Response(200, json={"value": []})

    async def scenario():
        time = [NOW]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            monitor = TrafficMonitor(client, source="LTA_DATAMALL", account_key="test-secret",
                                     clock=lambda: time[0])
            first = await monitor.snapshot()
            assert first["status"] == "FRESH"
            assert len(first["incidents"]) == 1
            assert len(hits) == 3
            assert (await monitor.snapshot())["status"] == "FRESH"
            assert len(hits) == 3
            time[0] += timedelta(minutes=2)
            next_read = await monitor.snapshot()
            assert next_read["status"] == "FRESH"
            assert len(hits) == 4  # only 90-second incidents refresh
            time[0] += timedelta(days=3)
            stale = await monitor.snapshot()
            assert stale["status"] == "FRESH"  # re-fetched all fresh
            assert len(hits) == 7
            missing = TrafficMonitor(client, source="LTA_DATAMALL", account_key="")
            assert (await missing.snapshot())["status"] == "MISSING_KEY"
            disabled = TrafficMonitor(client, source="DISABLED")
            assert (await disabled.snapshot())["status"] == "DISABLED"
    asyncio.run(scenario())


def test_incident_closure_blocks_route_and_roadworks_only_add_delay():
    closed = traffic([speed()], [incident()])
    result = assess_traffic([START, END], 4, 600, closed, NOW)
    assert result["blocked"] is True
    assert rank_cost(result, 1.0) is None
    works = traffic([speed()], [], [{"id":"works", "road":"FIRST ROAD",
        "start_date":"2026-10-27", "end_date":"2026-10-29"}])
    work_result = assess_traffic([START, END], 4, 600, works, NOW)
    assert work_result["blocked"] is False
    assert work_result["roadworks"] == 1
    assert work_result["estimated_duration_seconds"] > 600
    assert assess_traffic([START, END], 4, 600,
        {"source": "DISABLED"}, NOW)["status"] == "UNKNOWN"


def test_traffic_alternate_avoids_incident_without_offroad_connector(monkeypatch):
    from ev_fleet.dispatch import traffic_routes
    detour = {"latitude": 1.305, "longitude": 103.81}
    primary = route()
    alternative = RoadRoute(source="OSRM", fetched_at=NOW,
        geometry=[START, detour, END], distance_km=4.5, duration_seconds=750)
    async def alternatives(*args, **kwargs):
        return [alternative]
    monkeypatch.setattr(traffic_routes, "_osrm_alternatives", alternatives)
    feed = traffic([speed(start=START, end={"latitude":1.30,"longitude":103.805})],
                   [incident(position={"latitude":1.30,"longitude":103.81})])
    result = asyncio.run(choose_traffic_route(None, primary, START, END, .05,
        feed, NOW, osrm_url="http://osrm.internal:5000"))
    assert result.source == "OSRM"
    assert result.rerouted is True
    assert result.geometry[1].latitude == pytest.approx(1.305)
    assert result.traffic_assessment["blocked"] is False


def test_every_route_blocked_rejects_before_claim():
    async def run():
        with pytest.raises(HTTPException) as exc:
            await choose_traffic_route(None, route(), START, END, .05,
                traffic([speed()], [incident(position=START)]), NOW, osrm_url="")
        assert exc.value.detail == "TRAFFIC_ROUTE_BLOCKED_NO_SAFE_ALTERNATIVE"
    asyncio.run(run())


def test_traffic_aware_matching_changes_ranking_without_holding_bays(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            site = BY_ID["mock-bishan-01"]
            p = site.position.model_dump()
            near = {**p, "longitude": p["longitude"] - .001}
            far = {**p, "longitude": p["longitude"] + .005}
            await cluster.vehicle("near", soc=60)
            await cluster.vehicle("far", soc=60)
            await cluster.telemetry("near", soc=60, position=near)
            await cluster.telemetry("far", soc=60, position=far)
            fleet = await cluster.fleet.snapshot()
            zone = (await cluster.demand.get("bishan")).model_dump(mode="json")
            settings = Settings()
            base = rank_pairs(fleet, zone, [site], settings, clock.now(), 1)
            assert base[0]["vehicle_id"] == "near"
            feed = traffic([speed(start=near, end=p, minimum=5, maximum=10)],
                           [incident(position=near)])
            chosen = rank_pairs(fleet, zone, [site], settings, clock.now(), 1,
                                traffic=feed)
            assert chosen[0]["vehicle_id"] == "far"
            assert not cluster.facilities.holds
    asyncio.run(scenario())
