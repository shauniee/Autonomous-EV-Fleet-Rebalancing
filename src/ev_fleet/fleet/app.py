from ev_fleet.common.logging import base_app
from ev_fleet.common.dashboard_cache import DashboardCache
from fastapi import Response
from ev_fleet.contracts.vehicles import VehicleRegistration, TelemetryUpdate, SessionRequest, VehicleState, FleetSnapshot
from .store import FleetStore
from uuid import UUID
from ev_fleet.contracts.dispatch import ClaimRequest, ClaimRelease, VehicleClaim
from ev_fleet.contracts.rentals import RentalStart, RentalReturn, RentalRecord
from ev_fleet.contracts.charging_tasks import ChargingTaskCreate, ChargingTaskUpdate


def create_app(store=None, cache=None):
    app = base_app("Fleet State Service")
    state = store or FleetStore()
    app.state.store = state
    app.state.persistence = state.repository
    display_cache = cache if cache is not None else DashboardCache.from_env('fleet', state.instance_id, 1)
    app.state.dashboard_cache = display_cache

    @app.post("/api/v1/vehicles", status_code=201, response_model=VehicleState)
    async def register(body: VehicleRegistration):
        return await state.register(body)

    @app.get("/api/v1/vehicles", response_model=FleetSnapshot)
    async def vehicles():
        return await state.snapshot()

    @app.get("/api/v1/dashboard/vehicles", response_model=FleetSnapshot)
    async def dashboard_vehicles(response: Response):
        result, status = await display_cache.get_or_compute(
            f"vehicles:{state.version}", state.snapshot, FleetSnapshot)
        response.headers["X-Dashboard-Cache"] = status
        return result

    @app.get("/api/v1/vehicles/{vehicle_id}", response_model=VehicleState)
    async def vehicle(vehicle_id: str):
        return await state.get(vehicle_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/telemetry-sessions")
    async def session(vehicle_id: str, body: SessionRequest):
        return await state.session(vehicle_id, body.request_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/telemetry")
    async def telemetry(vehicle_id: str, body: TelemetryUpdate):
        return await state.telemetry(vehicle_id, body)

    @app.post("/api/v1/vehicles/{vehicle_id}/rentals", status_code=201, response_model=RentalRecord)
    async def start_rental(vehicle_id: str, body: RentalStart):
        return await state.start_rental(vehicle_id, body)

    @app.get("/api/v1/vehicles/{vehicle_id}/rentals/{rental_id}", response_model=RentalRecord)
    async def get_rental(vehicle_id: str, rental_id: UUID):
        return await state.get_rental(vehicle_id, rental_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/rentals/{rental_id}/return", response_model=RentalRecord)
    async def return_rental(vehicle_id: str, rental_id: UUID, body: RentalReturn):
        return await state.return_rental(vehicle_id, rental_id, body)

    @app.get("/api/v1/charging-tasks")
    async def charging_tasks():
        return await state.list_charging_tasks()

    @app.post("/api/v1/charging-tasks", status_code=201)
    async def create_charging_task(body: ChargingTaskCreate):
        return await state.create_charging_task(body)

    @app.post("/api/v1/charging-tasks/{task_id}/transition")
    async def transition_charging_task(task_id: UUID, body: ChargingTaskUpdate):
        return await state.update_charging_task(task_id, body)

    @app.put("/api/v1/vehicles/{vehicle_id}/claims/{dispatch_id}", response_model=VehicleClaim)
    async def claim(vehicle_id: str, dispatch_id: UUID, body: ClaimRequest):
        return await state.claim(vehicle_id, dispatch_id, body)

    @app.get("/api/v1/vehicles/{vehicle_id}/claims/{dispatch_id}", response_model=VehicleClaim)
    async def get_claim(vehicle_id: str, dispatch_id: UUID):
        return await state.get_claim(vehicle_id, dispatch_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/claims/{dispatch_id}/release", response_model=VehicleClaim)
    async def release(vehicle_id: str, dispatch_id: UUID, body: ClaimRelease):
        return await state.release(vehicle_id, dispatch_id, body)

    @app.on_event("shutdown")
    async def stop_display_cache():
        await display_cache.aclose()
        await state.close()

    @app.on_event("startup")
    async def restore_state():
        await state.start()

    return app


app = create_app()
