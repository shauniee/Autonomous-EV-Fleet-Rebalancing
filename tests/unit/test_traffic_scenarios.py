"""Synthetic traffic events are explicit, safe, bounded and never live LTA claims."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from ev_fleet.contracts.routing import RoadRoute
from ev_fleet.rebalancing.scenarios import (
    TrafficScenarios, TrafficScenarioInput, allowed_control,
)
from ev_fleet.rebalancing.traffic import assess_traffic, rank_cost

A = {"latitude": 1.3, "longitude": 103.8}
B = {"latitude": 1.305, "longitude": 103.8}
C = {"latitude": 1.305, "longitude": 103.81}

class Clock:
    def __init__(self):
        self.value = datetime(2026, 10, 30, 5, tzinfo=timezone.utc)
    def now(self):
        return self.value

def route():
    return RoadRoute(source="ONEMAP", fetched_at=Clock().now(),
        geometry=[A, B, C], distance_km=2, duration_seconds=240)

def request(kind, *, segment=0, speed=12, version=0, expires=30):
    return TrafficScenarioInput(
        dispatch_id=uuid4(), segment_index=segment, kind=kind,
        speed_kph=None if kind == "ROAD_CLOSED" else speed,
        duration_minutes=expires, expected_version=version)

def disabled_feed():
    return {"source":"DISABLED", "status":"DISABLED", "generated_at":Clock().now().isoformat(),
            "speeds":[], "incidents":[], "roadworks":[], "feeds":{}}

def live_feed():
    return {"source":"LTA_DATAMALL", "status":"FRESH",
            "generated_at":Clock().now().isoformat(),
            "speeds":[{"id":"speed-live", "road":"FIRST ROAD", "start":A, "end":B,
                       "min_kph":20, "max_kph":30}],
            "incidents":[{"id":"incident-live", "type":"ACCIDENT", "position":B,
                          "message":"Observed incident"}],
            "roadworks":[{"id":"work-live", "road":"FIRST ROAD",
                          "start_date":"2026-10-01", "end_date":"2026-11-01"}],
            "feeds":{"speeds":{"status":"FRESH", "observed_at":Clock().now().isoformat(),
                                "count":1, "error":None}}}

def test_guard_denies_anonymous_and_disabled_mutations(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SIMULATION_ENABLED", "false")
    monkeypatch.setenv("TRAFFIC_SIMULATION_CONTROL_KEY", "long-secret-for-test")
    with pytest.raises(HTTPException) as err:
        allowed_control("long-secret-for-test")
    assert err.value.status_code == 403
    monkeypatch.setenv("TRAFFIC_SIMULATION_ENABLED", "true")
    with pytest.raises(HTTPException):
        allowed_control("wrong-secret")
    allowed_control("long-secret-for-test")

def test_scenario_can_recommend_different_route_and_close_only_segment(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SIMULATION_ENABLED", "true")
    store = TrafficScenarios(Clock())
    initial = store.create(request("CONGESTION", speed=10), route())
    assert initial["version"] == 1
    merged = store.merged_snapshot(disabled_feed())
    assert merged["source"] == "SIMULATION"
    assert merged["mode"] == "SIMULATION"
    assert merged["simulated_events"][0]["source"] == "SIMULATION"
    slow = assess_traffic([A, B, C], 2, 240, merged)
    clear = assess_traffic([C, {"latitude":1.315,"longitude":103.81}], 2, 240, merged)
    assert slow["delay_factor"] == 4
    assert slow["estimated_duration_seconds"] == 960
    assert clear["delay_factor"] == 1
    assert rank_cost(slow, 1) > rank_cost(clear, 1)
    closed = store.create(request("ROAD_CLOSED", version=1), route())
    report = assess_traffic([A, B, C], 2, 240, store.merged_snapshot(disabled_feed()))
    assert report["blocked"] is True
    assert rank_cost(report, 1) is None
    assert closed["version"] == 2
    with pytest.raises(HTTPException) as conflict:
        store.clear(1)
    assert conflict.value.status_code == 409
    store.delete(closed["scenarios"][-1]["id"], 2)
    assert assess_traffic([A, B, C], 2, 240, store.merged_snapshot(disabled_feed()))["blocked"] is False

def test_active_simulation_retains_live_lta_as_separate_observations(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SIMULATION_ENABLED", "true")
    store = TrafficScenarios(Clock())
    store.create(request("ROADWORKS", speed=15), route())
    merged = store.merged_snapshot(live_feed())
    assert merged["source"] == "SIMULATION"
    assert merged["mode"] == "LIVE_AND_SIMULATION"
    assert merged["simulated_events"][0]["source"] == "SIMULATION"
    assert merged["live_observations"]["source"] == "LTA_DATAMALL"
    assert merged["live_observations"]["incidents"][0]["message"] == "Observed incident"
    assert all(row["id"] != "incident-live" for row in merged["incidents"])

def test_rejects_unknown_or_unroutable_or_outside_segments():
    store = TrafficScenarios(Clock())
    with pytest.raises(HTTPException):
        store.create(request("CONGESTION", segment=5), route())
    with pytest.raises(HTTPException):
        store.create(request("ROAD_CLOSED"), None)
    with pytest.raises(HTTPException):
        store.create(request("ROADWORKS", speed=None), route())
    with pytest.raises(ValidationError):
        RoadRoute(source="ONEMAP", fetched_at=Clock().now(),
            geometry=[{"latitude":2.0,"longitude":103.8}, {"latitude":2.01,"longitude":103.8}],
            distance_km=1, duration_seconds=100)

def test_expired_synthetic_conditions_revert_to_explicit_disabled(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SIMULATION_ENABLED", "true")
    clock = Clock()
    store = TrafficScenarios(clock)
    store.create(request("ROADWORKS", speed=20, expires=1), route())
    clock.value += timedelta(minutes=2)
    assert store.merged_snapshot(disabled_feed())["mode"] == "DISABLED"
    assert store.list()["scenarios"] == []
