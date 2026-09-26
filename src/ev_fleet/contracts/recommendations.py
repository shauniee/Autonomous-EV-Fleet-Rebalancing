from uuid import UUID
from pydantic import AwareDatetime, Field
from .common import Model
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
    ranked_candidates: list[CandidateAssessment]
    excluded_candidates: list[CandidateAssessment]
    decision_latency_ms: float = Field(ge=0)
    explanation: str
    assumptions: list[str]
