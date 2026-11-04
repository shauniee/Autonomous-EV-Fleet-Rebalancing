"""Read-only internal assessment must use full cached links, never map sampling."""
import asyncio
from datetime import datetime, timezone

import httpx

from ev_fleet.rebalancing.app import create_app
from ev_fleet.rebalancing.traffic import build_speed_index

NOW = datetime(2026, 11, 4, tzinfo=timezone.utc)


def test_internal_route_assessment_uses_full_feed_and_public_map_stays_bounded(monkeypatch):
    monkeypatch.setenv("TRAFFIC_SOURCE", "DISABLED")
    monkeypatch.setenv("TRAFFIC_CACHE_BACKEND", "INLINE")
    close = {"latitude": 1.311, "longitude": 103.811}
    close_end = {"latitude": 1.3113, "longitude": 103.8113}
    far = {"latitude": 1.32, "longitude": 103.82}
    far_end = {"latitude": 1.3203, "longitude": 103.8203}
    # All links fall in roughly the same map display cell. The map selects
    # extreme speeds on the FAR road, not this middle-speed nearby link.
    speeds = [
        {"id": str(i), "road": "FAR ROAD", "start": far, "end": far_end,
         "min_kph": 5 if i % 2 else 90, "max_kph": 5 if i % 2 else 90}
        for i in range(800)
    ]
    speeds.append({"id": "near", "road": "NEAR ROAD", "start": close,
                   "end": close_end, "min_kph": 45, "max_kph": 45})
    snapshot = {
        "source": "LTA_DATAMALL", "status": "FRESH",
        "generated_at": NOW.isoformat(), "feeds": {},
        "speeds": speeds, "incidents": [], "roadworks": [],
        "_speed_index": build_speed_index(speeds),
    }
    app = create_app()
    async def check():
        async with app.router.lifespan_context(app):
            app.state.traffic_monitor = type("Cached", (), {
                "cached_snapshot": lambda self: snapshot
            })()
            async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://rebalancing.test") as client:
                projected = await client.get("/api/v1/traffic/conditions")
                assert projected.status_code == 200, projected.text
                public = projected.json()
                assert len(public["speeds"]) < len(speeds)
                assert "_speed_index" not in public
                assert "_speed_display" not in public
                result = await client.post("/api/v1/traffic/assess-route", json={
                    "geometry": [close, close_end], "distance_km": .05,
                    "duration_seconds": 90,
                })
                assert result.status_code == 200, result.text
                assessment = result.json()
                assert assessment["matched_speed_segments"] == 1
                assert assessment["status"] == "FRESH"
                status = await client.get("/api/v1/traffic/cache-status")
                assert status.status_code == 200
                assert "speeds" not in status.json()
                assert status.json()["source"] == "LTA_DATAMALL"
    asyncio.run(check())
