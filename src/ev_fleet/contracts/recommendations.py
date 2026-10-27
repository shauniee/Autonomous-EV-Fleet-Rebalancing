from uuid import UUID
from pydantic import AwareDatetime, Field
from .common import Model, Position
from .demand import ZoneState


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
    ranked_candidates: list[CandidateAssessment]
    excluded_candidates: list[CandidateAssessment]
    decision_latency_ms: float = Field(ge=0)
    explanation: str
    assumptions: list[str]

class BatchRecommendationRequest(RecommendationRequest):
    quantity: int = Field(ge=1, le=20)


class ParkingAssignment(Model):
    recommendation_id: UUID
    vehicle_id: str
    facility_id: str
    facility_name: str
    target_position: Position
    requires_charging: bool
    distance_km: float = Field(ge=0)
    predicted_arrival_soc_pct: float
    score: float = Field(ge=0, le=1)


class BatchRecommendation(Model):
    batch_recommendation_id: UUID
    target_zone_id: str
    requested_count: int = Field(ge=1, le=20)
    recommended_count: int = Field(ge=0, le=20)
    unfulfilled_count: int = Field(ge=0, le=20)
    created_at: AwareDatetime
    expires_at: AwareDatetime
    assignments: list[ParkingAssignment]
    explanation: str
    assumptions: list[str]
