from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ev_fleet.contracts.common import Model, Position


class Facility(Model):
    """Simulated location; capacity includes charging bays and must not be double counted."""
    facility_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    position: Position
    parking_capacity: int = Field(ge=0)
    parking_available: int = Field(ge=0)
    charging_bays: int = Field(ge=0)
    charging_available: int = Field(ge=0)
    connector_types: list[str] = Field(default_factory=list)
    status: Literal["OPEN", "CLOSED"]
    source: Literal["MOCK"] = "MOCK"

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.parking_available > self.parking_capacity:
            raise ValueError("parking availability exceeds total capacity")
        if self.charging_bays > self.parking_capacity:
            raise ValueError("charging bays must be a subset of parking capacity")
        if self.charging_available > min(self.charging_bays, self.parking_available):
            raise ValueError("available charging bays must be available parking bays")
        if self.status == "CLOSED" and (self.parking_available or self.charging_available):
            raise ValueError("closed facilities cannot report available spaces")
        if self.charging_bays and not self.connector_types:
            raise ValueError("charging bays must specify connector types")
        return self


class FacilitiesSnapshot(Model):
    generated_at: AwareDatetime
    data_source: Literal["MOCK"] = "MOCK"
    facilities: list[Facility]
