from uuid import UUID
from typing import Literal
from pydantic import AwareDatetime, Field, model_validator
from .common import Model, Position
from .demand import ZoneState
from .crds import CrdsCandidateGain, CrdsMetadata, CrdsPlanningMode


class RecommendationRequest(Model):
    target_zone_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")


class CandidateAssessment(Model):
    vehicle_id: str
    state_version: int = Field(ge=1)
    eligible: bool
    exclusion_reasons: list[str]
    distance_km: float | None = Field(default=None, ge=0)
    energy_required_kwh: float | None = Field(default=None, ge=0)
    current_soc_pct: float | None = Field(default=None, ge=0, le=100)
    # Negative estimates remain visible for excluded EVs.
    predicted_arrival_soc_pct: float | None = Field(default=None, le=100)
    utilities: dict[str, float] | None = None
    weighted_components: dict[str, float] | None = None
    score: float | None = Field(default=None, ge=0, le=1)
    charging_assessment: str | None = None
    traffic_assessment: dict | None = None
    crds_gain: CrdsCandidateGain | None = None


class SnapshotReference(Model):
    instance_id: UUID
    snapshot_version: int = Field(ge=0)
    snapshot_at: AwareDatetime


class Recommendation(Model):
    recommendation_id: UUID
    created_at: AwareDatetime
    expires_at: AwareDatetime
    policy_version: str
    policy_settings: dict[str, float]
    target: ZoneState
    fleet_snapshot: SnapshotReference
    selected_vehicle_id: str | None
    parking_facility_id: str | None = None
    target_position: Position | None = None
    requires_charging: bool = False
    purpose: Literal["REBALANCE", "CHARGE"] = "REBALANCE"
    ranked_candidates: list[CandidateAssessment]
    excluded_candidates: list[CandidateAssessment]
    traffic_assessment: dict | None = None
    decision_latency_ms: float = Field(ge=0)
    explanation: str
    assumptions: list[str]
    crds: CrdsMetadata | None = None

class BatchRecommendationRequest(RecommendationRequest):
    planning_mode: CrdsPlanningMode = "OPTIMIZE_COVERAGE"
    quantity: int = Field(ge=1, le=50)
    purpose: Literal["REBALANCE", "CHARGE"] = "REBALANCE"
    vehicle_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    facility_id: str | None = Field(default=None, min_length=1, max_length=128)

    @model_validator(mode="after")
    def selected_vehicle_quantity(self):
        if self.purpose == "CHARGE" and self.planning_mode != "OPTIMIZE_COVERAGE":
            raise ValueError("requested-quantity planning is for REBALANCE only")
        if self.vehicle_id and self.quantity != 1:
            raise ValueError("a selected vehicle requires quantity one")
        if self.facility_id and (self.purpose != "CHARGE" or not self.vehicle_id or self.quantity != 1):
            raise ValueError("a selected charging facility requires one selected EV and CHARGE purpose")
        return self


class ParkingAssignment(Model):
    recommendation_id: UUID
    vehicle_id: str
    facility_id: str
    facility_name: str
    target_position: Position
    requires_charging: bool
    distance_km: float = Field(ge=0)
    predicted_arrival_soc_pct: float
    score: float | None = Field(default=None, ge=0, le=1)
    traffic_assessment: dict | None = None
    crds_gain: CrdsCandidateGain | None = None


class BatchRecommendation(Model):
    batch_recommendation_id: UUID
    target_zone_id: str
    purpose: Literal["REBALANCE", "CHARGE"] = "REBALANCE"
    requested_count: int = Field(ge=1, le=50)
    recommended_count: int = Field(ge=0, le=50)
    unfulfilled_count: int = Field(ge=0, le=50)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    assignments: list[ParkingAssignment]
    traffic_status: str | None = None
    traffic_generated_at: AwareDatetime | None = None
    explanation: str
    assumptions: list[str]
    crds: CrdsMetadata | None = None
    unfulfilled_reason: str | None = None
