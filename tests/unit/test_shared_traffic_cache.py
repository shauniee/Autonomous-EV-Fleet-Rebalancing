"""Shared traffic cache stays fresh, bounded and fail-closed under Redis faults."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
import httpx
from redis.exceptions import RedisError

from ev_fleet.rebalancing.traffic import FEEDS, TrafficMonitor
from ev_fleet.rebalancing.shared_traffic import (
    TrafficCachePublisher, RedisTrafficReader, META_KEY, _decode_rows,
)

START = datetime(2026, 11, 3, 4, tzinfo=timezone.utc)
SPEED = {"id": "1", "road": "TEST ROAD",
         "start": {"latitude": 1.3, "longitude": 103.8},
         "end": {"latitude": 1.31, "longitude": 103.81},
         "min_kph": 10, "max_kph": 19}
INCIDENT = {"id": "a", "type": "ACCIDENT",
            "position": {"latitude": 1.3, "longitude": 103.8},
            "message": "Fixture incident"}
WORKS = {"id": "w", "road": "TEST ROAD",
         "start_date": "2026-11-03", "end_date": "2026-11-06"}


class FakePipeline:
    def __init__(self, store):
        self.store = store
        self.actions = []
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_):
        return False
    def set(self, *args, **kwargs):
        self.actions.append(("set", args, kwargs))
    def hset(self, *args, **kwargs):
        self.actions.append(("hset", args, kwargs))
    async def execute(self):
        for method, args, kwargs in self.actions:
            await getattr(self.store, method)(*args, **kwargs)


class FakeRedis:
    def __init__(self):
        self.blobs = {}
        self.hashes = {}
        self.broken = False
        self.blob_reads = 0
    def check(self):
        if self.broken:
            raise RedisError("unavailable")
    def pipeline(self, transaction=True):
        self.check()
        assert transaction
        return FakePipeline(self)
    async def set(self, key, value, ex=None):
        self.check()
        assert ex is not None and ex > 0
        self.blobs[key] = value
    async def hset(self, key, field, value):
        self.check()
        self.hashes.setdefault(key, {})[field.encode()] = value.encode()
    async def hget(self, key, field):
        self.check()
        return self.hashes.get(key, {}).get(field.encode())
    async def hgetall(self, key):
        self.check()
        return dict(self.hashes.get(key, {}))
    async def get(self, key):
        self.check()
        self.blob_reads += 1
        return self.blobs.get(key)
    async def aclose(self):
        pass


def test_shared_cache_versions_and_freshness_without_extra_blob_reads():
    async def scenario():
        redis = FakeRedis()
        clock = [START]
        writer = TrafficCachePublisher(redis)
        reader = RedisTrafficReader(redis, source="LTA_DATAMALL", clock=lambda: clock[0])
        for name, rows in (("speeds", [SPEED]), ("incidents", [INCIDENT]),
                           ("roadworks", [WORKS])):
            await writer.success(name, rows, START)
        await reader.refresh()
        snap = reader.cached_snapshot()
        assert snap["status"] == "FRESH"
        assert snap["speeds"] == [SPEED]
        assert snap["incidents"] == [INCIDENT]
        assert redis.blob_reads == 3

        # Same observation data at the next five-minute refresh is not decoded again.
        clock[0] += timedelta(minutes=5)
        assert await writer.success("speeds", [SPEED], clock[0])
        await reader.refresh()
        assert redis.blob_reads == 3
        assert reader.cached_snapshot()["feeds"]["speeds"]["status"] == "FRESH"
        assert reader.cached_snapshot()["feeds"]["incidents"]["status"] == "STALE"
        assert reader.cached_snapshot()["incidents"] == []

        # An upstream failure retains the last successful timestamp. It cannot
        # make data newer than it is, and must show the original failure code.
        await writer.error("speeds", "FETCH_FAILED")
        clock[0] += timedelta(minutes=16)
        await reader.refresh()
        expired = reader.cached_snapshot()
        assert expired["status"] == "PARTIAL"
        assert expired["speeds"] == []
        assert expired["feeds"]["speeds"]["status"] == "STALE"
        assert expired["feeds"]["speeds"]["error"] == "FETCH_FAILED"
        assert expired["roadworks"] == [WORKS]

        # Redis outage never changes an unknown road to a clear one.
        redis.broken = True
        await reader.refresh()
        down = reader.cached_snapshot()
        assert down["speeds"] == []
        assert down["incidents"] == []
        assert down["feeds"]["speeds"]["error"] == "CACHE_UNAVAILABLE"
        assert down["status"] == "PARTIAL"
    asyncio.run(scenario())


def test_corrupt_or_missing_blob_is_unknown_not_fresh():
    async def scenario():
        redis = FakeRedis()
        writer = TrafficCachePublisher(redis)
        await writer.success("speeds", [SPEED], START)
        # Metadata exists but the payload is missing (e.g. Redis LRU eviction).
        redis.blobs.clear()
        reader = RedisTrafficReader(redis, source="LTA_DATAMALL", clock=lambda: START)
        await reader.refresh()
        report = reader.cached_snapshot()
        assert report["feeds"]["speeds"]["status"] == "CACHE_INVALID"
        assert report["speeds"] == []
        with pytest.raises(ValueError):
            _decode_rows(b"not zlib", "a" * 64)
    asyncio.run(scenario())


def test_only_collector_does_lta_fetching_and_v4_path_is_used():
    calls = []
    def handler(request):
        calls.append(str(request.url))
        assert request.headers["AccountKey"] == "fixture-key"
        return httpx.Response(200, json={"value": []})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            monitor = TrafficMonitor(http, source="LTA_DATAMALL",
                                     account_key="fixture-key", clock=lambda: START)
            result = await monitor._fetch("speeds")
            assert result == []
        redis = FakeRedis()
        reader = RedisTrafficReader(redis, source="LTA_DATAMALL",
                                    clock=lambda: START)
        await reader.refresh()
        assert reader.cached_snapshot()["status"] == "PARTIAL"
        assert len(calls) == 1
        assert "/v4/TrafficSpeedBands" in calls[0]
    asyncio.run(scenario())


def test_shared_cache_disabled_mode_never_serves_live_data():
    reader = RedisTrafficReader(source="DISABLED", clock=lambda: START)
    assert reader.cached_snapshot()["source"] == "DISABLED"
    assert reader.cached_snapshot()["speeds"] == []
    assert FEEDS["speeds"][1] == 300
    assert FEEDS["incidents"][1] == 90
