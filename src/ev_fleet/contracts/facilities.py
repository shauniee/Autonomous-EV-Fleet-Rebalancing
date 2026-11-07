from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from ev_fleet.contracts.common import Model, Position


class Facility(Model):
    """Destination observations plus local simulation capacity.

    For CHARGING sites, allocation slots represent chargers, not public parking
    bays. Unknown totals remain None rather than being inferred from availability.
    """
    facility_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    zone_id: str = Field(min_length=1)
    position: Position
    parking_capacity: int | None = Field(default=None, ge=0)
    parking_available: int = Field(ge=0)
    charging_bays: int | None = Field(default=None, ge=0)
    charging_available: int = Field(ge=0)
    charging_power_kw: float | None = Field(default=None, gt=0, le=1000)
    connector_types: list[str] = Field(default_factory=list)
    status: Literal["OPEN", "CLOSED", "UNKNOWN"]
    source: Literal["MOCK", "LTA_DATAMALL"] = "MOCK"
    facility_type: Literal["PARKING", "CHARGING"] = "PARKING"
    availability_basis: Literal["PARKING_BAYS", "CHARGERS"] = "PARKING_BAYS"
    agency: str | None = None
    address: str | None = None
    availability_observed_at: AwareDatetime | None = None
    availability_expires_at: AwareDatetime | None = None
    # Original external observations, before local simulated allocations.
    reported_parking_available: int | None = Field(default=None, ge=0)
    reported_charging_available: int | None = Field(default=None, ge=0)

    def is_fresh(self, now):
        return self.source == "MOCK" or (
            self.availability_observed_at is not None
            and self.availability_observed_at <= now
            and self.availability_expires_at is not None
            and now < self.availability_expires_at)

    @model_validator(mode="after")
    def validate_capacity(self):
        if self.parking_capacity is not None and self.parking_available > self.parking_capacity:
            raise ValueError("parking availability exceeds total capacity")
        if (self.charging_bays is not None and self.parking_capacity is not None
                and self.charging_bays > self.parking_capacity):
            raise ValueError("charging bays must be a subset of parking capacity")
        if (self.charging_available > self.parking_available or
                (self.charging_bays is not None and self.charging_available > self.charging_bays)):
            raise ValueError("available charging bays must be available parking bays")
        if self.status == "CLOSED" and (self.parking_available or self.charging_available):
            raise ValueError("closed facilities cannot report available spaces")
        if self.source == "MOCK" and self.charging_bays and not self.connector_types:
            raise ValueError("charging bays must specify connector types")
        return self


class FacilityFeed(Model):
    name: Literal["parking", "charging"]
    status: Literal["PENDING", "MISSING_KEY", "FRESH", "STALE", "ERROR"]
    last_success_at: AwareDatetime | None = None
    error_code: str | None = None


class FacilitiesSnapshot(Model):
    generated_at: AwareDatetime
    data_source: Literal["MOCK", "LTA_DATAMALL"] = "MOCK"
    facilities: list[Facility]
    feeds: list[FacilityFeed] = Field(default_factory=list)

from uuid import UUID

class ParkingHoldRequest(Model):
    dispatch_id: UUID
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    expected_zone_id: str
    expected_position: Position
    # Road-only arrival endpoint; centre remains the immutable facility identity.
    access_position: Position | None = None
    requires_charging: bool = False

class ParkingHold(Model):
    dispatch_id: UUID
    vehicle_id: str
    facility_id: str
    requires_charging: bool
    charging_allocated: bool
    status: Literal["RESERVED", "OCCUPIED", "RELEASED"]


class ParkedFacility(Model):
    facility_id: str
    name: str
    position: Position
    zone_id: str
    source: Literal["MOCK", "LTA_DATAMALL"]
    charging_allocated: bool
    charging_power_kw: float = Field(gt=0, le=1000)
    charging_target_soc_pct: float = Field(gt=0, le=100)


class VehicleParking(Model):
    vehicle_id: str
    parking: ParkedFacility | None = None
    pending: bool = False
    access_position_missing: bool = False
