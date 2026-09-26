from pydantic import Field
from uuid import UUID
from .common import Model, Observation, Position


class Zone(Model):
    zone_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    name: str = Field(min_length=1, max_length=100)
    position: Position


class DemandUpdate(Observation):
    expected_version: int = Field(ge=0)
    desired_available_vehicles: int = Field(ge=0, le=10000)


class ChargingUpdate(Observation):
    expected_version: int = Field(ge=0)
    available_slots: int = Field(ge=0, le=10000)


class DemandObservation(Observation):
    desired_available_vehicles: int = Field(ge=0, le=10000)


class ChargingObservation(Observation):
    available_slots: int = Field(ge=0, le=10000)


class ZoneState(Zone):
    instance_id: UUID
    version: int = Field(ge=1)
    demand_version: int = Field(ge=0)
    charging_version: int = Field(ge=0)
    demand: DemandObservation | None
    charging: ChargingObservation | None
