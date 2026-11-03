"""Read-only recovery diagnosis; this contract does not authorize takeover."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from .common import Model, Position
from .dispatch import DispatchState


class RecoveryBlocker(Model):
    code: str
    detail: str


class RecoveryEvidence(Model):
    dispatch_state: DispatchState
    failure_reason: str | None
    command_id: UUID | None
    last_event_sequence: int = Field(ge=-1)
    acknowledgement_accepted: bool | None
    release_outcome: Literal["ARRIVED", "REJECTED"] | None
    claim_held: bool | None
    fleet_instance_id: UUID | None = None
    fleet_snapshot_version: int | None = None
    fleet_control_version: int | None = None
    claim_status: Literal["ACTIVE", "RELEASED"] | None = None
    parking_status: Literal["RESERVED", "OCCUPIED", "RELEASED"] | None = None
    telemetry_age_seconds: float | None = None
    checkpoint_source: Literal["LEGACY_TELEMETRY", "NONE"] = "NONE"
    checkpoint_at: AwareDatetime | None = None
    recorded_position: Position | None = None
    recorded_soc_pct: float | None = Field(default=None, ge=0, le=100)
    recorded_distance_km: float | None = Field(default=None, ge=0)


class RecoveryAssessment(Model):
    dispatch_id: UUID
    vehicle_id: str | None
    assessed_at: AwareDatetime
    assessment_revision: str
    status: Literal["NOT_REQUIRED", "REQUIRED", "BLOCKED"]
    reason_code: str
    # Phase 1 diagnoses only. No simulator capability or durable checkpoint yet.
    can_resume: Literal[False] = False
    supported_action: None = None
    blockers: list[RecoveryBlocker] = Field(default_factory=list)
    evidence: RecoveryEvidence
