import asyncio
from contextlib import asynccontextmanager
import httpx
from fastapi import HTTPException, Response
from ev_fleet.common.dashboard_cache import DashboardCache

from ev_fleet.common.logging import base_app
from ev_fleet.contracts.facilities import FacilitiesSnapshot, Facility, ParkingHoldRequest, ParkingHold, VehicleParking
from ev_fleet.demand.store import SINGAPORE_ZONES
from .store import FacilitiesStore
from .datamall import DataMallClient, poll_feed


def create_app(store=None, cache=None):
    app = base_app("Parking and Charging Simulation Service")
    state = store or FacilitiesStore()
    app.state.store = state
    app.state.persistence = state.repository
    display_cache = cache if cache is not None else DashboardCache.from_env('facilities', state.instance_id, 2)
    app.state.dashboard_cache = display_cache
    zone_ids = {item[0] for item in SINGAPORE_ZONES}

    @asynccontextmanager
    async def lifespan(app):
        await state.start()
        app.state.feed_tasks = []
        if state.settings.source == "LTA_DATAMALL" and state.settings.account_key.get_secret_value():
            client = httpx.AsyncClient()
            app.state.datamall_client = client
            provider = DataMallClient(client, state.settings.account_key)
            app.state.feed_tasks = [asyncio.create_task(poll_feed(state, provider, name))
                                    for name in ("parking", "charging")]
        try:
            yield
        finally:
            for task in app.state.feed_tasks:
                task.cancel()
            await asyncio.gather(*app.state.feed_tasks, return_exceptions=True)
            if getattr(app.state, "datamall_client", None):
                await app.state.datamall_client.aclose()
            await display_cache.aclose()
            await state.close()

    app.router.lifespan_context = lifespan

    @app.get("/api/v1/facilities", response_model=FacilitiesSnapshot)
    async def facilities(zone_id: str | None = None):
        if zone_id is not None and zone_id not in zone_ids:
            raise HTTPException(404, "ZONE_NOT_FOUND")
        return await state.view(zone_id)

    @app.get("/api/v1/dashboard/facilities", response_model=FacilitiesSnapshot)
    async def dashboard_facilities(response: Response):
        async def producer():
            return await state.view()
        # Availability expires with wall time, independently of version updates.
        if state.settings.source == "LTA_DATAMALL":
            response.headers["X-Dashboard-Cache"] = "BYPASS"
            return await producer()
        result, status = await display_cache.get_or_compute(
            f"facilities:{state.version}", producer, FacilitiesSnapshot)
        response.headers["X-Dashboard-Cache"] = status
        return result

    @app.get("/api/v1/facilities/{facility_id}", response_model=Facility)
    async def facility(facility_id: str):
        return await state.get(facility_id)

    @app.post("/api/v1/facilities/{facility_id}/holds", status_code=201, response_model=ParkingHold)
    async def reserve(facility_id: str, body: ParkingHoldRequest):
        return await state.reserve(facility_id, body)

    @app.get("/api/v1/holds/{dispatch_id}", response_model=ParkingHold)
    async def hold(dispatch_id: str):
        return await state.get_hold(dispatch_id)

    @app.get("/api/v1/vehicles/{vehicle_id}/parking", response_model=VehicleParking)
    async def vehicle_parking(vehicle_id: str):
        return await state.vehicle_parking(vehicle_id)

    @app.post("/api/v1/holds/{dispatch_id}/complete", response_model=ParkingHold)
    async def complete(dispatch_id: str):
        return await state.change(dispatch_id, "OCCUPIED")

    @app.post("/api/v1/holds/{dispatch_id}/release", response_model=ParkingHold)
    async def release(dispatch_id: str):
        return await state.change(dispatch_id, "RELEASED")

    return app


app = create_app()
