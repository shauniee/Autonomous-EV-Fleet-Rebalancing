from enum import Enum
from typing import Literal
from uuid import UUID, uuid5
from pydantic import AwareDatetime, Field, field_validator
from .common import Model, Position
from .routing import RoadRoute


class DispatchState(str, Enum):
    QUEUED = "QUEUED"
    PENDING = "PENDING"
    SENT = "SENT"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    IN_PROGRESS = "IN_PROGRESS"
    ARRIVAL_PENDING = "ARRIVAL_PENDING"
    SUCCEEDED = "SUCCEEDED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class DispatchApproval(Model):
    request_id: UUID
    recommendation_id: UUID


class ClaimRequest(Model):
    command_id: UUID
    expected_instance_id: UUID
    expected_version: int = Field(ge=1)
    expected_control_version: int | None = Field(default=None, ge=1)
    route_origin: Position | None = None
    target_zone_id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    target_position: Position
    approve_before: AwareDatetime
    min_reserve_pct: float = Field(ge=0, lt=100)
    route_factor: float = Field(ge=1)
    energy_margin_factor: float = Field(ge=1)
    route_distance_km: float | None = Field(default=None, ge=0)


class VehicleClaim(Model):
    dispatch_id: UUID
    command_id: UUID
    fleet_instance_id: UUID
    vehicle_id: str
    target_zone_id: str
    target_position: Position
    origin: Position
    acquired_at: AwareDatetime
    status: Literal["ACTIVE", "RELEASED"] = "ACTIVE"
    outcome: Literal["ARRIVED", "REJECTED"] | None = None
    released_at: AwareDatetime | None = None


class ClaimRelease(Model):
    command_id: UUID
    outcome: Literal["ARRIVED", "REJECTED"]


class RelocationCommand(Model):
    dispatch_id: UUID
    command_id: UUID
    vehicle_id: str
    fleet_instance_id: UUID
    target_zone_id: str
    target_position: Position
    issued_at: AwareDatetime
    min_reserve_pct: float = Field(ge=0, lt=100)
    route_factor: float = Field(ge=1)
    energy_margin_factor: float = Field(ge=1)
    route: RoadRoute | None = None


class CommandAcknowledgement(Model):
    command_id: UUID
    vehicle_id: str
    accepted: bool
    reason: str | None = Field(default=None, max_length=200)


class DispatchEvent(Model):
    command_id: UUID
    vehicle_id: str
    sequence: int = Field(ge=0)
    observed_at: AwareDatetime
    kind: Literal["PROGRESS", "ARRIVED", "FAILED"]
    distance_travelled_km: float = Field(ge=0)
    reason: str | None = Field(default=None, max_length=200)


class RecoveryOperation(Model):
    request_id: UUID
    status: Literal["REQUESTED", "RESTORING", "RECOVERED", "BLOCKED"]
    reason_code: str | None = None
    generation: int | None = None
    confirmed_paused: bool = False
    intent_fingerprint: str | None = None


class Dispatch(Model):
    dispatch_id: UUID
    request_id: UUID
    recommendation_id: UUID
    vehicle_id: str | None
    command: RelocationCommand | None
    state: DispatchState
    created_at: AwareDatetime
    updated_at: AwareDatetime
    acknowledged_at: AwareDatetime | None
    completed_at: AwareDatetime | None
    last_contact_at: AwareDatetime | None
    delivery_attempts: int = Field(ge=0)
    distance_travelled_km: float = Field(ge=0)
    arrival_soc_pct: float | None = Field(ge=0, le=100)
    failure_reason: str | None
    claim_held: bool | None
    revalidated_metrics: dict | None = None
    parking_facility_id: str | None = None
    parking_status: Literal["RESERVED", "OCCUPIED", "RELEASED"] | None = None
    recovery: RecoveryOperation | None = None


ApprovalStatus = Literal["APPROVED", "REJECTED", "UNKNOWN"]


class ApprovalDecision(Model):
    status: ApprovalStatus
    reason_code: str
    vehicle_id: str | None = None
    facility_id: str | None = None
    related_reasons: list[str] = Field(default_factory=list)
    arrival_soc_pct: float | None = None
    reserve_pct: float | None = None
    gain_vehicle_minutes: float | None = None
    minimum_gain_vehicle_minutes: float | None = None


class DispatchApprovalResult(Dispatch):
    approval_status: ApprovalStatus


class DispatchList(Model):
    dispatches: list[Dispatch]


class CommandList(Model):
    commands: list[RelocationCommand]

# Queued-plan approvals use this identity for both individual and future group approvals.
QUEUED_BATCH_APPROVAL_NAMESPACE = UUID("80691ee1-26ef-47bb-b272-f5ebc6eba4e3")


def queued_batch_approval_id(batch_id: UUID) -> UUID:
    return uuid5(QUEUED_BATCH_APPROVAL_NAMESPACE, str(batch_id))


class BatchApproval(Model):
    request_id: UUID
    batch_recommendation_id: UUID


class BatchDispatchItem(Model):
    recommendation_id: UUID
    vehicle_id: str
    facility_id: str
    dispatch_id: UUID | None = None
    state: str
    error: str | None = None
    approval_status: ApprovalStatus | None = None
    approval_decision: ApprovalDecision | None = None
    queue_status: Literal["QUEUED", "WAITING", "ADMITTED", "REJECTED", "CANCELLED"] | None = None
    attempts: int = Field(default=0, ge=0)
    retry_at: AwareDatetime | None = None


class BatchDispatch(Model):
    request_id: UUID
    batch_recommendation_id: UUID
    requested_count: int = Field(ge=1)
    recommended_count: int = Field(ge=0)
    items: list[BatchDispatchItem]
    status: Literal["APPROVED", "PARTIAL", "REJECTED", "UNKNOWN", "QUEUED", "PROCESSING"]
    job_status: Literal["QUEUED", "RUNNING", "COMPLETED", "EXPIRED"] | None = None
    accepted_at: AwareDatetime | None = None
    expires_at: AwareDatetime | None = None
    admitted_count: int = Field(default=0, ge=0)
    completed_count: int = Field(default=0, ge=0)
    queued_count: int = Field(default=0, ge=0)
    in_flight_limit: int = Field(default=4, ge=1)


class BatchApprovalRefusal(Model):
    request_id: UUID
    batch_recommendation_id: UUID
    reason_code: str
    status_code: Literal[404, 409, 422]
    recorded_at: AwareDatetime


class BatchApprovalLookup(Model):
    batches: list[BatchDispatch]
    refusals: list[BatchApprovalRefusal]


class BatchApprovalGroupRequest(Model):
    group_id: UUID
    batch_recommendation_ids: list[UUID] = Field(min_length=1, max_length=10)

    @field_validator("batch_recommendation_ids")
    @classmethod
    def unique_plans(cls, ids: list[UUID]) -> list[UUID]:
        if len(set(ids)) != len(ids):
            raise ValueError("batch recommendation IDs must be unique")
        return ids


class BatchApprovalGroupPlan(Model):
    batch_recommendation_id: UUID
    request_id: UUID
    status: Literal["QUEUED", "RUNNING", "APPROVED", "PARTIAL", "REJECTED", "UNKNOWN", "SKIPPED"]
    reason_code: str | None = None
    approval: BatchDispatch | None = None


class BatchApprovalGroupConflict(Model):
    vehicle_id: str
    batch_recommendation_ids: list[UUID]


class BatchApprovalGroup(Model):
    group_id: UUID
    status: Literal["QUEUED", "RUNNING", "COMPLETED", "CONFLICT", "UNKNOWN"]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    reason_code: str | None = None
    plans: list[BatchApprovalGroupPlan]
    conflicts: list[BatchApprovalGroupConflict] = Field(default_factory=list)


class BatchApprovalGroupPage(Model):
    enabled: bool
    groups: list[BatchApprovalGroup]
    next_offset: int | None = None
