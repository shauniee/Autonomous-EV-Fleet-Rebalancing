from enum import Enum
from uuid import UUID
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .common import Model, Observation, Position
from .dispatch import VehicleClaim
from .rentals import RentalRecord


class Availability(str, Enum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"


class RelocationState(str, Enum):
    IDLE = "IDLE"
    DISPATCHED = "DISPATCHED"
    RELOCATING = "RELOCATING"
    ARRIVED = "ARRIVED"
    FAILED = "FAILED"


class VehicleRegistration(Model):
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    battery_capacity_kwh: float = Field(gt=0, le=300)
    consumption_kwh_per_km: float = Field(gt=0, le=2)


class TelemetryUpdate(Observation):
    session_id: UUID
    sequence: int = Field(ge=0)
    position: Position
    soc_pct: float = Field(ge=0, le=100)
    availability: Availability = Availability.AVAILABLE
    relocation_state: RelocationState = RelocationState.IDLE
    active_dispatch_id: UUID | None = None
    distance_travelled_km: float | None = Field(default=None, ge=0)
    parked_facility_id: str | None = None
    charging_state: Literal["NOT_CHARGING", "CHARGING", "COMPLETE"] = "NOT_CHARGING"
    charging_power_kw: float | None = Field(default=None, gt=0, le=1000)
    # Optional simulator-derived digital-twin measurements, not real sensors.
    speed_kph: float | None = Field(default=None, ge=0, le=250)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    odometer_km: float | None = Field(default=None, ge=0)
    battery_temp_c: float | None = Field(default=None, ge=-40, le=120)
    motor_temp_c: float | None = Field(default=None, ge=-40, le=180)
    power_kw: float | None = Field(default=None, ge=-1000, le=1000)
    battery_soh_pct: float | None = Field(default=None, ge=0, le=100)
    gps_quality: Literal["SIMULATED", "GOOD", "DEGRADED", "INVALID"] | None = None
    fault_codes: list[str] = Field(default_factory=list, max_length=20)


class SessionRequest(Model):
    request_id: UUID


class SimulatorRecoveryState(Model):
    generation: int = Field(ge=1)
    owner_revision: int = Field(ge=1)
    expires_at: AwareDatetime
    status: Literal["ACTIVE", "EXPIRED", "BLOCKED"]
    checkpoint_revision: int = Field(ge=0)
    checkpoint_at: AwareDatetime | None = None
    durable: bool


class VehicleState(VehicleRegistration):
    state_version: int = Field(ge=1)
    control_version: int = Field(default=1, ge=1)
    telemetry: TelemetryUpdate | None
    received_at: AwareDatetime | None
    telemetry_age_seconds: float | None = Field(ge=0)
    stale: bool
    connectivity: Literal["ONLINE", "OFFLINE"]
    active_claim: VehicleClaim | None = None
    active_rental: RentalRecord | None = None
    active_charging_task: dict | None = None
    effective_availability: Availability = Availability.UNAVAILABLE
    current_zone_id: str | None = None
    effective_relocation_state: RelocationState = RelocationState.IDLE
    simulator_recovery: SimulatorRecoveryState | None = None

    @model_validator(mode="after")
    def telemetry_metadata(self):
        if self.telemetry is not None and (self.received_at is None or self.telemetry_age_seconds is None):
            raise ValueError("telemetry requires receive time and age")
        return self


class FleetSnapshot(Model):
    instance_id: UUID
    snapshot_version: int = Field(ge=0)
    snapshot_at: AwareDatetime
    vehicles: list[VehicleState]

    @model_validator(mode="after")
    def unique_vehicles(self):
        if len({vehicle.vehicle_id for vehicle in self.vehicles}) != len(self.vehicles):
            raise ValueError("duplicate vehicle IDs in snapshot")
        return self
