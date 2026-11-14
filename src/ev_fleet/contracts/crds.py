"""Internal inputs and results for the pure CRDS calculation."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, NonNegativeInt, model_validator

from .common import Model, Position
from .routing import RoadRoute
from .demand import ZoneState
from .dispatch import Dispatch
from .facilities import FacilitiesSnapshot, Facility
from .vehicles import FleetSnapshot


CrdsPlanningMode = Literal["OPTIMIZE_COVERAGE", "FULFIL_REQUEST"]


class CrdsParameters(Model):
    horizon_minutes: int = Field(default=30, gt=0)
    slot_minutes: int = Field(default=1, gt=0)
    minimum_gain_vehicle_minutes: float = Field(default=1, ge=0)
    priority_coverage_pct: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def whole_slots(self):
        if self.horizon_minutes % self.slot_minutes:
            raise ValueError("horizon_minutes must contain a whole number of slots")
        return self

    @property
    def slot_count(self):
        return self.horizon_minutes // self.slot_minutes


class CrdsZoneTrajectory(Model):
    """Baseline stock at each slot's start, including existing commitments."""

    desired_stock: list[NonNegativeInt] = Field(min_length=1)
    usable_stock: list[NonNegativeInt] = Field(min_length=1)

    @model_validator(mode="after")
    def matching_slots(self):
        if len(self.desired_stock) != len(self.usable_stock):
            raise ValueError("desired_stock and usable_stock must have matching slots")
        return self


class CrdsFuture(Model):
    future_id: str = Field(min_length=1)
    zones: dict[str, CrdsZoneTrajectory] = Field(min_length=1)


class CrdsMoveOutcome(Model):
    arrival_minutes: float = Field(ge=0)
    availability_delay_minutes: float = Field(default=0, ge=0)
    energy_required_kwh: float = Field(ge=0)


class CrdsMove(Model):
    """A pre-eligible EV move with supplied outcomes for each required future."""

    vehicle_id: str = Field(min_length=1)
    facility_id: str = Field(min_length=1)
    # None means a known physical position outside every modeled demand zone.
    origin_zone_id: str | None = Field(min_length=1)
    target_zone_id: str = Field(min_length=1)
    outcomes: dict[str, CrdsMoveOutcome] = Field(default_factory=dict)


class CrdsCandidateGain(Model):
    vehicle_id: str
    facility_id: str
    # Historical wire name. The score may weight minutes below priority coverage.
    worst_case_gain_vehicle_minutes: float
    worst_case_future_id: str
    # Positive change is added shortage; negative change is shortage relief.
    shortage_changes_by_zone: dict[str, float]
    worst_case_energy_required_kwh: float = Field(ge=0)


class CrdsEvaluation(Model):
    decision: Literal["MOVE", "HOLD", "DEFER"]
    reason_code: str
    selected_vehicle_id: str | None = None
    selected_facility_id: str | None = None
    ranked_candidates: list[CrdsCandidateGain] = Field(default_factory=list)


class CrdsPlanningSnapshot(Model):
    """Live journey observations with a commitment/health revision.

    Delivery, ACK and routine progress update journey observations without
    changing revision once that journey holds its claim and destination.
    Claims, journey health, arrival, parking and recovery changes invalidate approvals.
    """
    instance_id: UUID
    revision: int = Field(ge=0)
    observed_at: AwareDatetime
    dispatches: list[Dispatch]


class CrdsDemandSnapshot(Model):
    instance_id: UUID
    snapshot_at: AwareDatetime
    zones: list[ZoneState]
    fixed_demand_zone_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_zones(self):
        if len({zone.zone_id for zone in self.zones}) != len(self.zones):
            raise ValueError("duplicate demand zone IDs")
        return self


class CrdsRelocationExclusion(Model):
    vehicle_id: str
    reason: Literal["TELEMETRY_UNAVAILABLE", "RELOCATION_FAILED", "RELOCATION_UNCONFIRMED",
                    "RELOCATION_OWNERSHIP_UNCONFIRMED", "VEHICLE_MISSING"]
    dispatch_ids: list[UUID]


class CrdsInputs(Model):
    evaluated_at: AwareDatetime
    fleet: FleetSnapshot
    demand: CrdsDemandSnapshot
    facilities: FacilitiesSnapshot
    planning: CrdsPlanningSnapshot
    traffic: dict
    available_by_zone: dict[str, int]
    vehicle_zones: dict[str, str]
    excluded_vehicle_ids: list[str] = Field(default_factory=list)
    relocation_exclusions: list[CrdsRelocationExclusion] = Field(default_factory=list)
    excluded_facility_ids: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    # Diagnostic only. The calculation fingerprint uses the filtered inputs.
    telemetry_exclusions: dict[str, str] = Field(default_factory=dict, exclude=True)
    withheld_facilities: list[Facility] = Field(default_factory=list, exclude=True)


class CrdsRouteRequest(Model):
    vehicle_id: str = Field(min_length=1)
    facility_id: str = Field(min_length=1)
    expected_fleet_instance_id: UUID
    expected_control_version: int = Field(ge=1)


class CrdsRouteEstimate(Model):
    vehicle_id: str
    facility_id: str
    fleet_instance_id: UUID
    control_version: int
    origin: Position
    route: RoadRoute


class CrdsMetadata(Model):
    planning_mode: CrdsPlanningMode = "OPTIMIZE_COVERAGE"
    coverage_tradeoff: bool = False
    decision: Literal["MOVE", "HOLD", "DEFER"]
    reason_code: str
    approval_allowed: bool = False
    parameters: CrdsParameters
    scenario_model_version: str
    scenario_count: int = Field(ge=0)
    epoch_id: UUID
    input_fingerprint: str
    source_references: dict
    worst_case_gain_vehicle_minutes: float | None = None
    worst_case_future_id: str | None = None
    shortage_changes_by_zone: dict[str, float] = Field(default_factory=dict)
    arrival_minutes_min: float | None = Field(default=None, ge=0)
    arrival_minutes_max: float | None = Field(default=None, ge=0)
    execution: dict = Field(default_factory=dict)


class CrdsRevalidationRequest(Model):
    approved_batch_request_id: UUID | None = None
    recommendation_id: UUID
    dispatch_id: UUID
    route: RoadRoute
    expected_planning_instance_id: UUID
    expected_planning_revision: int = Field(ge=0)


class CrdsRevalidationResult(Model):
    allowed: bool
    metadata: CrdsMetadata


class CrdsEvaluationTask(Model):
    epoch_id: UUID
    model_version: Literal["crds-engine-v2"] = "crds-engine-v2"
    input_fingerprint: str
    parameters: CrdsParameters
    futures: list[CrdsFuture] = Field(min_length=1, max_length=64)
    moves: list[CrdsMove] = Field(max_length=1000)


class CrdsTaskResult(Model):
    epoch_id: UUID
    model_version: Literal["crds-engine-v2"]
    input_fingerprint: str
    future_ids: list[str]
    evaluation: CrdsEvaluation
