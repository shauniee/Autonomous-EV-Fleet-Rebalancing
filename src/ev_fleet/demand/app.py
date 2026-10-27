import asyncio
from contextlib import asynccontextmanager, suppress
import httpx
from fastapi import Query, Response
from ev_fleet.common.dashboard_cache import DashboardCache
from ev_fleet.common.logging import base_app
from ev_fleet.contracts.demand import Zone, ZoneState, DemandUpdate, ChargingUpdate
from .store import DemandStore, seed
from .settings import DemandSettings
from .simulation import DemandSimulator
from .clients import fleet_snapshot
from .aggregation import compose_heatmap
from ev_fleet.contracts.heatmap import HeatMapResponse, DemandSimulationStatus


def create_app(store=None, seed_zones=True, settings=None, client=None, cache=None):
    app = base_app("Singapore Demand Service")
    config = settings or DemandSettings.from_env()
    state = store or DemandStore(manual_hold_seconds=config.manual_hold_seconds)
    state.manual_hold_seconds = config.manual_hold_seconds
    app.state.store = state
    display_cache = cache if cache is not None else DashboardCache.from_env('demand', state.instance_id, 1)
    app.state.dashboard_cache = display_cache
    simulator = DemandSimulator(state, config)
    app.state.simulator = simulator
    app.state.settings = config

    @asynccontextmanager
    async def lifespan(app):
        if seed_zones and not state.zones:
            await seed(state)
        async with httpx.AsyncClient(timeout=config.fleet_timeout_seconds) as owned:
            app.state.client = client or owned
            task = None
            if config.simulation_enabled:
                await simulator.tick()
                task = asyncio.create_task(simulator.run())
            try:
                yield
            finally:
                await display_cache.aclose()
                if task:
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

    app.router.lifespan_context = lifespan

    @app.post("/api/v1/zones", status_code=201, response_model=ZoneState)
    async def create(body: Zone):
        return await state.create(body)

    @app.get("/api/v1/zones")
    async def zones():
        return await state.snapshot()

    @app.get("/api/v1/zones/{zone_id}", response_model=ZoneState)
    async def zone(zone_id: str):
        return await state.get(zone_id)

    @app.put("/api/v1/zones/{zone_id}/demand")
    async def demand(zone_id: str, body: DemandUpdate):
        return await state.update(zone_id, "demand", body)

    @app.put("/api/v1/zones/{zone_id}/charging")
    async def charging(zone_id: str, body: ChargingUpdate):
        return await state.update(zone_id, "charging", body)

    @app.get("/api/v1/hotspots")
    async def hotspots(threshold: int = Query(default=5, ge=1)):
        snapshot = await state.snapshot()
        return {"snapshot_at": snapshot["snapshot_at"], "threshold": threshold,
                "basis": "simulated desired available vehicles; not unmet demand",
                "hotspots": [zone for zone in snapshot["zones"] if zone["demand"]
                             and zone["demand"]["desired_available_vehicles"] >= threshold]}

    async def heat_map_view():
        # Each read has one demand snapshot and one Fleet snapshot. No cross-service transaction.
        local = await state.composition_snapshot()
        fleet, error = await fleet_snapshot(app.state.client, config)
        return compose_heatmap(local, fleet, error, config, state.clock.now())

    @app.get("/api/v1/dashboard/heat-map", response_model=HeatMapResponse)
    async def dashboard_heat_map(response: Response):
        result, status = await display_cache.get_or_compute(
            f"heat-map:{state.version}", heat_map_view, HeatMapResponse)
        response.headers["X-Dashboard-Cache"] = status
        return result

    @app.get("/api/v1/heat-map", response_model=HeatMapResponse)
    async def heat_map():
        return await heat_map_view()

    @app.get("/api/v1/heat-map/hotspots", response_model=HeatMapResponse)
    async def capacity_hotspots():
        view = await heat_map_view()
        view["zones"] = [zone for zone in view["zones"] if zone["hotspot_severity"] in ("WARNING", "CRITICAL")]
        return view

    @app.get("/api/v1/demand-simulation", response_model=DemandSimulationStatus)
    async def simulation_status():
        return await simulator.status()

    return app


app = create_app()
