from fastapi import HTTPException

from ev_fleet.common.logging import base_app
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.facilities import FacilitiesSnapshot, Facility, ParkingHoldRequest, ParkingHold
from ev_fleet.demand.store import SINGAPORE_ZONES
from .store import FacilitiesStore


def create_app(store=None):
    app = base_app("Mock Parking and Charging Service")
    state = store or FacilitiesStore()
    app.state.store = state
    zone_ids = {item[0] for item in SINGAPORE_ZONES}

    @app.get("/api/v1/facilities", response_model=FacilitiesSnapshot)
    async def facilities(zone_id: str | None = None):
        if zone_id is not None and zone_id not in zone_ids:
            raise HTTPException(404, "ZONE_NOT_FOUND")
        return FacilitiesSnapshot(generated_at=utcnow(), facilities=await state.snapshot(zone_id))

    @app.get("/api/v1/facilities/{facility_id}", response_model=Facility)
    async def facility(facility_id: str):
        return await state.get(facility_id)

    @app.post("/api/v1/facilities/{facility_id}/holds", status_code=201, response_model=ParkingHold)
    async def reserve(facility_id: str, body: ParkingHoldRequest):
        return await state.reserve(facility_id, body)

    @app.get("/api/v1/holds/{dispatch_id}", response_model=ParkingHold)
    async def hold(dispatch_id: str):
        return await state.get_hold(dispatch_id)

    @app.post("/api/v1/holds/{dispatch_id}/complete", response_model=ParkingHold)
    async def complete(dispatch_id: str):
        return await state.change(dispatch_id, "OCCUPIED")

    @app.post("/api/v1/holds/{dispatch_id}/release", response_model=ParkingHold)
    async def release(dispatch_id: str):
        return await state.change(dispatch_id, "RELEASED")

    return app


app = create_app()
