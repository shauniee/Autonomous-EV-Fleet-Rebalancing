"""Regression coverage for full-feed route matching without quadratic work."""
from datetime import datetime, timezone
from random import Random

import pytest

from ev_fleet.rebalancing import traffic


def test_long_route_assessment_projects_each_point_once(monkeypatch):
    """Full-feed matching must not reproject the route for every observation."""
    geometry = [{"latitude": 1.3, "longitude": 103.8 + i / 100000}
                for i in range(600)]
    rows = [{"id": str(i), "road": "TEST ROAD",
             "start": {"latitude": 1.3 if i < 60 else 1.31,
                       "longitude": geometry[-1]["longitude"]},
             "end": {"latitude": 1.3 if i < 60 else 1.31,
                     "longitude": geometry[-1]["longitude"] + .0001},
             "min_kph": 20, "max_kph": 30} for i in range(120)]
    incidents = [{"id": "closure", "position": geometry[-1], "type": "ROAD BLOCK"}]
    calls = 0
    project = traffic._xy

    def counted_projection(point):
        nonlocal calls
        calls += 1
        return project(point)

    monkeypatch.setattr(traffic, "_xy", counted_projection)
    result = traffic.assess_traffic(geometry, 1, 100, {
        "source": "LTA_DATAMALL", "status": "FRESH",
        "speeds": rows, "incidents": incidents, "roadworks": [],
    }, datetime(2026, 11, 12, tzinfo=timezone.utc))

    assert result["matched_speed_segments"] == 60
    assert result["blocked"] is True
    assert result["incidents"] == 1
    assert calls <= len(geometry) + 2 * len(rows) + len(incidents)


@pytest.mark.parametrize("threshold", [.025, .10, .12, .20])
@pytest.mark.parametrize("fallback", [False, True])
def test_spatial_prefilter_preserves_exact_distance_decisions(monkeypatch, threshold, fallback):
    """Grid edges and bounded-memory fallback must never drop a closure."""
    if fallback:
        monkeypatch.setattr(traffic._RouteMatcher, "_MAX_ENTRIES", 0)
    random = Random(3104)
    geometry = [{"latitude": 1.3 + i / 100000, "longitude": 103.8 + i / 50000}
                for i in range(60)]
    geometry.insert(30, geometry[29])  # A zero-length road segment is valid.
    matcher = traffic._RouteMatcher(geometry)
    points = geometry + [{"latitude": 1.298 + random.random() * .006,
                          "longitude": 103.798 + random.random() * .008}
                         for _ in range(120)]
    for point in points:
        assert matcher.near(point, threshold) == traffic._near_route(point, geometry, threshold)


def test_broad_segment_fallback_preserves_nearby_roadblock():
    geometry = [{"latitude": 1.1, "longitude": 103.5},
                {"latitude": 2.5, "longitude": 104.1}]
    assessment = traffic.assess_traffic(geometry, 80, 7200, {
        "source": "LTA_DATAMALL", "status": "FRESH", "speeds": [],
        "incidents": [{"type": "ROAD BLOCK", "position": geometry[-1]}],
    })
    assert assessment["blocked"] is True
    assert assessment["incidents"] == 1
