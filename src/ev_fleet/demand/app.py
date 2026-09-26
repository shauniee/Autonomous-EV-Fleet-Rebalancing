from contextlib import asynccontextmanager
from fastapi import Query
from ev_fleet.common.logging import base_app
from ev_fleet.contracts.demand import Zone, ZoneState, DemandUpdate, ChargingUpdate
from .store import DemandStore, seed


def create_app(store=None, seed_zones=True):
    app = base_app("Singapore Demand Service")
    state = store or DemandStore()
    app.state.store = state

    @asynccontextmanager
    async def lifespan(app):
        if seed_zones and not state.zones:
            await seed(state)
        yield

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

    return app


app = create_app()
