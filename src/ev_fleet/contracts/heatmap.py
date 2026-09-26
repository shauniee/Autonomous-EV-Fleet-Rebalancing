from typing import Literal
from uuid import UUID
from pydantic import AwareDatetime, Field
from .common import Model, Position


class DemandProvenance(Model):
    source: Literal["MANUAL", "SIMULATED", "UNKNOWN"]
    manual_hold_until: AwareDatetime | None = None


class DemandSimulationStatus(Model):
    enabled: bool
    running: bool
    tick_count: int = Field(ge=0)
    last_tick_at: AwareDatetime | None
    last_error: str | None
    configuration: dict
    zones: dict[str, DemandProvenance]


class HeatMapZone(Model):
    zone_id: str
    name: str
    position: Position
    rental_demand: int | None = Field(ge=0)
    demand_observed_at: AwareDatetime | None
    demand_version: int = Field(ge=0)
    demand_source: Literal["MANUAL", "SIMULATED", "UNKNOWN"]
    manual_hold_until: AwareDatetime | None
    demand_status: Literal["FRESH", "STALE", "MISSING"]
    demand_level: Literal["LOW", "MEDIUM", "HIGH", "UNKNOWN"]
    available_evs: int | None = Field(ge=0)
    inbound_relocating_evs: int | None = Field(ge=0)
    inbound_pending_evs: int | None = Field(ge=0)
    inbound_unconfirmed_evs: int | None = Field(ge=0)
    arrived_pending_evs: int | None = Field(ge=0)
    excluded_stale_evs: int | None = Field(ge=0)
    excluded_low_soc_evs: int | None = Field(ge=0)
    current_balance: int | None
    current_shortage: int | None = Field(ge=0)
    current_surplus: int | None = Field(ge=0)
    projected_supply: int | None = Field(ge=0)
    projected_balance: int | None
    projected_shortage: int | None = Field(ge=0)
    projected_surplus: int | None = Field(ge=0)
    shortage_ratio: float | None = Field(ge=0, le=1)
    hotspot_severity: Literal["NORMAL", "WARNING", "CRITICAL", "UNKNOWN"]
    data_quality: Literal["COMPLETE", "DEGRADED", "UNKNOWN"]


class HeatMapResponse(Model):
    generated_at: AwareDatetime
    status: Literal["COMPLETE", "PARTIAL"]
    issues: list[str]
    calculation_version: str
    policy: dict
    demand_instance_id: UUID
    demand_snapshot_at: AwareDatetime
    fleet_instance_id: UUID | None
    fleet_snapshot_version: int | None
    fleet_snapshot_at: AwareDatetime | None
    fleet_status: Literal["FRESH", "STALE", "UNAVAILABLE", "INVALID"]
    unassigned_evs: int | None = Field(ge=0)
    excluded_stale_evs: int | None = Field(ge=0)
    excluded_low_soc_evs: int | None = Field(ge=0)
    zones: list[HeatMapZone]
