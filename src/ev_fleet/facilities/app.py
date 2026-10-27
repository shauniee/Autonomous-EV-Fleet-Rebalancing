from fastapi import HTTPException

from ev_fleet.common.logging import base_app
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.facilities import FacilitiesSnapshot, Facility
from ev_fleet.demand.store import SINGAPORE_ZONES
from .catalog import CATALOGUE, BY_ID


def create_app():
    app = base_app("Mock Parking and Charging Service")
    zone_ids = {item[0] for item in SINGAPORE_ZONES}

    @app.get("/api/v1/facilities", response_model=FacilitiesSnapshot)
    async def facilities(zone_id: str | None = None):
        if zone_id is not None and zone_id not in zone_ids:
            raise HTTPException(404, "ZONE_NOT_FOUND")
        return FacilitiesSnapshot(generated_at=utcnow(), facilities=[
            item for item in CATALOGUE if zone_id is None or item.zone_id == zone_id
        ])

    @app.get("/api/v1/facilities/{facility_id}", response_model=Facility)
    async def facility(facility_id: str):
        result = BY_ID.get(facility_id)
        if result is None:
            raise HTTPException(404, "FACILITY_NOT_FOUND")
        return result

    return app


app = create_app()
