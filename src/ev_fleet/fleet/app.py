from ev_fleet.common.logging import base_app
from ev_fleet.contracts.vehicles import VehicleRegistration, TelemetryUpdate, SessionRequest, VehicleState, FleetSnapshot
from .store import FleetStore
from uuid import UUID
from ev_fleet.contracts.dispatch import ClaimRequest, ClaimRelease, VehicleClaim
from ev_fleet.contracts.rentals import RentalStart, RentalReturn, RentalRecord


def create_app(store=None):
    app = base_app("Fleet State Service")
    state = store or FleetStore()
    app.state.store = state

    @app.post("/api/v1/vehicles", status_code=201, response_model=VehicleState)
    async def register(body: VehicleRegistration):
        return await state.register(body)

    @app.get("/api/v1/vehicles", response_model=FleetSnapshot)
    async def vehicles():
        return await state.snapshot()

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
