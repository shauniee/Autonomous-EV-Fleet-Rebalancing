"""Cache is best-effort; never used to authorise dispatches or parking holds."""
import asyncio
from uuid import uuid4

import httpx
from redis.exceptions import ConnectionError

from ev_fleet.common.dashboard_cache import DashboardCache
from ev_fleet.contracts.facilities import FacilitiesSnapshot, ParkingHoldRequest
from ev_fleet.facilities.app import create_app as facilities_app
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.fleet.app import create_app as fleet_app
from ev_fleet.fleet.store import FleetStore
from ev_fleet.contracts.vehicles import VehicleRegistration


class FakeRedis:
    def __init__(self):
        self.data = {}
        self.get_count = 0
        self.set_count = 0
        self.fail_reads = False
        self.fail_writes = False

    async def get(self, key):
        self.get_count += 1
        if self.fail_reads:
            raise ConnectionError("injected Redis outage")
        return self.data.get(key)

    async def set(self, key, value, ex=None):
        self.set_count += 1
        if self.fail_writes:
            raise ConnectionError("injected Redis outage")
        assert ex in (1, 2)
        self.data[key] = value
        return True

    async def aclose(self):
        return None


def test_facility_dashboard_cache_hit_and_versioned_invalidation():
    async def scenario():
        redis = FakeRedis()
        state = FacilitiesStore()
        cache = DashboardCache(redis, 2, "test-facilities")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=facilities_app(state, cache)),
            base_url="http://test") as client:
            path = "/api/v1/dashboard/facilities"
            first = await client.get(path)
            second = await client.get(path)
            assert first.headers["X-Dashboard-Cache"] == "MISS"
            assert second.headers["X-Dashboard-Cache"] == "HIT"
            assert redis.get_count == 2
            assert redis.set_count == 1
            assert first.json() == second.json()
            site = BY_ID["mock-cbd-01"]
            claim = ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id="ev-cache-test",
                expected_zone_id="cbd", expected_position=site.position)
            reserved = await client.post(f"/api/v1/facilities/{site.facility_id}/holds",
                                         json=claim.model_dump(mode="json"))
            assert reserved.status_code == 201
            updated = await client.get(path)
            assert updated.headers["X-Dashboard-Cache"] == "MISS"
            before = next(f for f in first.json()["facilities"] if f["facility_id"] == site.facility_id)
            after = next(f for f in updated.json()["facilities"] if f["facility_id"] == site.facility_id)
            assert after["parking_available"] == before["parking_available"] - 1
            # Operational read does not receive cached metadata.
            authoritative = await client.get(f"/api/v1/facilities/{site.facility_id}")
            assert "X-Dashboard-Cache" not in authoritative.headers
            assert authoritative.json()["parking_available"] == after["parking_available"]
    asyncio.run(scenario())


def test_redis_outage_fails_open_without_serving_stale_value():
    async def scenario():
        redis = FakeRedis()
        state = FacilitiesStore()
        cache = DashboardCache(redis, 2, "test-outage", circuit_seconds=100)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=facilities_app(state, cache)),
            base_url="http://test") as client:
            initial = await client.get("/api/v1/dashboard/facilities")
            assert initial.headers["X-Dashboard-Cache"] == "MISS"
            redis.fail_reads = True
            bypass = await client.get("/api/v1/dashboard/facilities")
            assert bypass.status_code == 200
            assert bypass.headers["X-Dashboard-Cache"] == "BYPASS"
            assert len(bypass.json()["facilities"]) == 15
            calls = redis.get_count
            again = await client.get("/api/v1/dashboard/facilities")
            assert again.headers["X-Dashboard-Cache"] == "BYPASS"
            assert redis.get_count == calls
    asyncio.run(scenario())


def test_fleet_dashboard_version_change_invalidates_cached_snapshot():
    async def scenario():
        state = FleetStore()
        redis = FakeRedis()
        app = fleet_app(store=state, cache=DashboardCache(redis, 1, "test-fleet"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as client:
            before = await client.get("/api/v1/dashboard/vehicles")
            again = await client.get("/api/v1/dashboard/vehicles")
            assert before.headers["X-Dashboard-Cache"] == "MISS"
            assert again.headers["X-Dashboard-Cache"] == "HIT"
            await state.register(VehicleRegistration(
                vehicle_id="new-ev", battery_capacity_kwh=60,
                consumption_kwh_per_km=0.18))
            after = await client.get("/api/v1/dashboard/vehicles")
            assert after.headers["X-Dashboard-Cache"] == "MISS"
            assert len(after.json()["vehicles"]) == 1
            authoritative = await client.get("/api/v1/vehicles")
            assert len(authoritative.json()["vehicles"]) == 1
            assert "X-Dashboard-Cache" not in authoritative.headers
    asyncio.run(scenario())
