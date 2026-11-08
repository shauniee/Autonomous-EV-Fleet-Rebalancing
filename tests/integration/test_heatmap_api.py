import asyncio
from copy import deepcopy
import httpx
import pytest
from ev_fleet.demand.app import create_app
from ev_fleet.demand.settings import DemandSettings
from ev_fleet.demand.store import DemandStore
from tests.demand_heatmap_support import DemandCluster


class MemorySnapshotRepository:
    def __init__(self):
        self.payload = None

    async def start(self):
        pass

    async def load(self):
        return deepcopy(self.payload)

    async def save(self, payload):
        self.payload = deepcopy(payload)

    async def close(self):
        pass


def test_all_zones_response_and_capacity_hotspots_preserve_legacy_api(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            for key in cluster.demand.zones:
                await cluster.set_demand(key, 0)
            await cluster.set_demand("bishan", 1)
            await cluster.set_demand("cbd", 3)
            view = await cluster.heatmap()
            assert len(view["zones"]) == 6 and view["status"] == "COMPLETE"
            assert view["fleet_snapshot_version"] == cluster.fleet.version
            assert view["fleet_instance_id"] == cluster.fleet.instance_id
            by_zone = {row["zone_id"]: row for row in view["zones"]}
            assert by_zone["cbd"]["hotspot_severity"] == "CRITICAL"
            assert by_zone["bishan"]["hotspot_severity"] == "NORMAL"
            filtered = await cluster.heatmap("/api/v1/heat-map/hotspots")
            assert [row["zone_id"] for row in filtered["zones"]] == ["cbd"]
            legacy = await cluster.client.get("http://demand.test/api/v1/hotspots")
            assert legacy.json()["hotspots"] == []  # Legacy threshold still 5, not shortage.
            response = await cluster.client.get("http://demand.test/api/v1/demand-simulation")
            assert response.json()["enabled"] is False and response.json()["tick_count"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("failure,expected", [("unavailable", "UNAVAILABLE"), ("timeout", "UNAVAILABLE"),
                                              ("malformed", "INVALID"), ("stale", "STALE")])
def test_fleet_failure_returns_unknown_capacity_not_zero(clock, failure, expected):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.set_demand()
            raw = await cluster.fleet.snapshot()
            if failure == "stale":
                clock.advance(5)
            def handler(request):
                if failure == "unavailable": return httpx.Response(503)
                if failure == "timeout": raise httpx.ReadTimeout("test", request=request)
                return httpx.Response(200, json={} if failure == "malformed" else raw)
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                cluster.apps["demand.test"].state.client = client
                view = await cluster.heatmap()
            cbd = next(row for row in view["zones"] if row["zone_id"] == "cbd")
            assert view["status"] == "PARTIAL" and view["fleet_status"] == expected
            assert cbd["rental_demand"] == 10
            assert cbd["available_evs"] is None and cbd["current_shortage"] is None
            assert cbd["hotspot_severity"] == "UNKNOWN"
            await cluster.set_demand(value=11)  # Fleet failure does not block demand writes.
    asyncio.run(scenario())


def test_missing_and_stale_demand_are_not_zero(clock):
    async def scenario():
        async with DemandCluster(clock, demand_max_age_seconds=10) as cluster:
            await cluster.vehicle()
            view = await cluster.heatmap()
            assert all(row["rental_demand"] is None and row["hotspot_severity"] == "UNKNOWN" for row in view["zones"])
            await cluster.set_demand(value=5)
            clock.advance(10)
            await cluster.telemetry()
            view = await cluster.heatmap()
            cbd = next(row for row in view["zones"] if row["zone_id"] == "cbd")
            assert cbd["rental_demand"] == 5 and cbd["demand_status"] == "STALE"
            assert cbd["current_shortage"] is None and cbd["demand_level"] == "UNKNOWN"
    asyncio.run(scenario())


def test_lifespan_starts_paused_and_api_controls_simulation():
    async def scenario():
        app = create_app(settings=DemandSettings(simulation_enabled=True, update_interval_seconds=.02))
        simulator = app.state.simulator
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://demand.test") as client:
                status = (await client.get("/api/v1/demand-simulation")).json()
                assert status["enabled"] is False and status["running"] is False
                assert status["tick_count"] == 0

                started = await client.post("/api/v1/demand-simulation/start")
                assert started.status_code == 200
                assert started.json()["enabled"] is True
                assert started.json()["tick_count"] == 1
                assert all(zone["demand"] for zone in (await app.state.store.snapshot())["zones"])

                before = await app.state.store.snapshot()
                paused = await client.post("/api/v1/demand-simulation/pause")
                assert paused.status_code == 200
                assert paused.json()["enabled"] is False
                await asyncio.sleep(.03)
                assert (await app.state.store.snapshot())["zones"] == before["zones"]
        assert not simulator.running
    asyncio.run(scenario())


def test_operator_simulation_choice_survives_service_restart():
    async def scenario():
        repository = MemorySnapshotRepository()
        first_store = DemandStore(repository=repository)
        first_app = create_app(
            store=first_store,
            settings=DemandSettings(update_interval_seconds=60),
        )
        async with first_app.router.lifespan_context(first_app):
            transport = httpx.ASGITransport(app=first_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://demand.test"
            ) as client:
                started = (await client.post("/api/v1/demand-simulation/start")).json()
                assert started["enabled"] is True

        restarted_store = DemandStore(repository=repository)
        restarted_app = create_app(
            store=restarted_store,
            settings=DemandSettings(update_interval_seconds=60),
        )
        async with restarted_app.router.lifespan_context(restarted_app):
            transport = httpx.ASGITransport(app=restarted_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://demand.test"
            ) as client:
                status = (await client.get("/api/v1/demand-simulation")).json()
                assert status["enabled"] is True
                assert status["running"] is True
                assert status["tick_count"] == 1

                paused = (await client.post("/api/v1/demand-simulation/pause")).json()
                assert paused["enabled"] is False

        paused_store = DemandStore(repository=repository)
        paused_app = create_app(
            store=paused_store,
            settings=DemandSettings(update_interval_seconds=60),
        )
        async with paused_app.router.lifespan_context(paused_app):
            assert (await paused_app.state.simulator.status())["enabled"] is False

    asyncio.run(scenario())


def test_heatmap_timeout_does_not_hold_demand_lock(clock):
    async def scenario():
        async with DemandCluster(clock, fleet_timeout_seconds=.03) as cluster:
            await cluster.set_demand(value=3)
            started = asyncio.Event()
            async def slow(request):
                started.set()
                await asyncio.sleep(5)
                return httpx.Response(200, json={})
            async with httpx.AsyncClient(transport=httpx.MockTransport(slow)) as client:
                cluster.apps["demand.test"].state.client = client
                task = asyncio.create_task(cluster.heatmap())
                await asyncio.wait_for(started.wait(), 1)
                await asyncio.wait_for(cluster.set_demand(value=7), .5)
                view = await asyncio.wait_for(task, 1)
                assert "FLEET_TIMEOUT" in view["issues"]
                cbd = next(row for row in view["zones"] if row["zone_id"] == "cbd")
                assert cbd["rental_demand"] == 3  # One local snapshot, not mixed old/new fields.
                assert cbd["demand_version"] == 1
                assert (await cluster.demand.get("cbd"))["demand_version"] == 2
    asyncio.run(scenario())


def test_duplicate_fleet_vehicle_ids_are_invalid_not_double_counted(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            raw = await cluster.fleet.snapshot()
            raw["vehicles"].append(raw["vehicles"][0])
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))) as client:
                cluster.apps["demand.test"].state.client = client
                view = await cluster.heatmap()
                assert view["fleet_status"] == "INVALID"
                assert all(row["available_evs"] is None for row in view["zones"])
    asyncio.run(scenario())
