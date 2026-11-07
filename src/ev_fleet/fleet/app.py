from contextlib import asynccontextmanager
from ev_fleet.common.logging import base_app
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.vehicles import DashboardFleetSnapshot, SimulatorRuntimeReport
from ev_fleet.common.dashboard_cache import DashboardCache
from fastapi import Response, Query
from ev_fleet.contracts.vehicles import VehicleRegistration, TelemetryUpdate, SessionRequest, VehicleState, FleetSnapshot
from .store import FleetStore
from uuid import UUID
from ev_fleet.contracts.dispatch import ClaimRequest, ClaimRelease, VehicleClaim
from ev_fleet.contracts.rentals import RentalStart, RentalReturn, RentalRecord
from ev_fleet.contracts.charging_tasks import ChargingTaskCreate, ChargingTaskUpdate
from ev_fleet.contracts.recovery import (SimulatorSessionRequest, SimulatorSession, SimulatorCheckpointRequest,
    SimulatorCheckpointReceipt, SimulatorCheckpointSnapshot, SimulatorRestoreRequest, SimulatorIdleRestoreRequest, SimulatorStopRequest)


def create_app(store=None, cache=None):
    app = base_app("Fleet State Service")
    state = store or FleetStore()
    app.state.store = state
    app.state.persistence = state
    display_cache = cache if cache is not None else DashboardCache.from_env('fleet', state.instance_id, 1)
    app.state.dashboard_cache = display_cache

    @asynccontextmanager
    async def lifespan(app):
        repository = state.repository
        if isinstance(repository, SnapshotRepository):
            repository.enable_recovery_exit()
        try:
            await state.start()
            yield
        finally:
            try:
                await display_cache.aclose()
            finally:
                await state.close()
                if isinstance(repository, SnapshotRepository):
                    repository.disable_recovery_exit()

    app.router.lifespan_context = lifespan

    @app.post("/api/v1/vehicles", status_code=201, response_model=VehicleState)
    async def register(body: VehicleRegistration):
        return await state.register(body)

    @app.get("/api/v1/vehicles", response_model=FleetSnapshot)
    async def vehicles():
        return await state.snapshot()

    @app.get("/api/v1/dashboard/vehicles", response_model=DashboardFleetSnapshot)
    async def dashboard_vehicles(response: Response):
        async def producer():
            return await state.snapshot(include_runtime=True)

        result, status = await display_cache.get_or_compute(
            f"vehicles-runtime:{state.version}", producer, DashboardFleetSnapshot)
        response.headers["X-Dashboard-Cache"] = status
        return result

    @app.get("/api/v1/vehicles/{vehicle_id}", response_model=VehicleState)
    async def vehicle(vehicle_id: str):
        return await state.get(vehicle_id)

    @app.get("/api/v1/vehicles/{vehicle_id}/telemetry-history")
    async def telemetry_history(vehicle_id: str, limit: int = Query(default=100, ge=1, le=500)):
        return await state.telemetry_history(vehicle_id, limit)

    @app.post("/api/v1/vehicles/{vehicle_id}/telemetry-sessions")
    async def session(vehicle_id: str, body: SessionRequest):
        return await state.session(vehicle_id, body.request_id)

    @app.post("/api/v1/vehicles/{vehicle_id}/telemetry")
    async def telemetry(vehicle_id: str, body: TelemetryUpdate):
        return await state.telemetry(vehicle_id, body)

    @app.put("/api/v1/vehicles/{vehicle_id}/simulator-runtime")
    async def simulator_runtime(vehicle_id: str, body: SimulatorRuntimeReport):
        return await state.report_runtime(vehicle_id, body)

    @app.post("/api/v1/vehicles/{vehicle_id}/simulator-sessions", response_model=SimulatorSession)
    async def simulator_session(vehicle_id: str, body: SimulatorSessionRequest | SimulatorRestoreRequest | SimulatorIdleRestoreRequest):
        return await state.simulator_recovery.session(vehicle_id, body)

    @app.post("/api/v1/vehicles/{vehicle_id}/simulator-stop", response_model=VehicleClaim)
    async def simulator_stop(vehicle_id: str, body: SimulatorStopRequest):
        return await state.simulator_recovery.stop_relocation(vehicle_id, body)

    @app.get("/api/v1/vehicles/{vehicle_id}/simulator-checkpoint", response_model=SimulatorCheckpointSnapshot)
    async def simulator_checkpoint_snapshot(vehicle_id: str):
        return await state.simulator_recovery.snapshot(vehicle_id)

    @app.put("/api/v1/vehicles/{vehicle_id}/simulator-checkpoint", response_model=SimulatorCheckpointReceipt)
    async def simulator_checkpoint(vehicle_id: str, body: SimulatorCheckpointRequest):
        return await state.simulator_recovery.checkpoint(vehicle_id, body)

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

    return app


app = create_app()
