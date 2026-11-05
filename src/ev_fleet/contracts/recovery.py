"""Recovery diagnosis and guarded simulator checkpoint contracts."""
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from .common import Model, Position
from .dispatch import CommandAcknowledgement, DispatchEvent, DispatchState, RelocationCommand, RecoveryOperation
from .vehicles import TelemetryUpdate


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
    checkpoint_source: Literal["LEGACY_TELEMETRY", "SIMULATOR_CHECKPOINT", "NONE"] = "NONE"
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
    can_resume: bool = False
    supported_action: Literal["RESUME_SIMULATION"] | None = None
    operation: RecoveryOperation | None = None
    blockers: list[RecoveryBlocker] = Field(default_factory=list)
    evidence: RecoveryEvidence


class SimulatorConfiguration(Model):
    model_version: Literal["movement-v1"] = "movement-v1"
    capacity_kwh: float = Field(gt=0, le=300)
    consumption_kwh_per_km: float = Field(gt=0, le=2)
    speed_kph: float = Field(gt=0)
    time_scale: float = Field(gt=0)


class SimulatorCheckpoint(Model):
    schema_version: Literal[1] = 1
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    fleet_instance_id: UUID
    physical: TelemetryUpdate
    command: RelocationCommand | None = None
    accepted: bool | None = None
    reason: str | None = Field(default=None, max_length=200)
    acknowledgement: CommandAcknowledgement | None = None
    acknowledged: bool = False
    pending_event: DispatchEvent | None = None
    next_event_sequence: int = Field(default=0, ge=0)
    completed_command_id: UUID | None = None
    simulation: SimulatorConfiguration | None = None

    @model_validator(mode="after")
    def coherent_execution(self):
        command, physical = self.command, self.physical
        if physical.odometer_km is None or physical.heading_deg is None:
            raise ValueError("checkpoint requires recorded odometer and heading")
        if command is None:
            if (physical.active_dispatch_id is not None or physical.relocation_state != "IDLE"
                    or physical.distance_travelled_km is not None or self.accepted is not None
                    or self.acknowledgement is not None or self.acknowledged or self.pending_event is not None):
                raise ValueError("idle checkpoint cannot carry active execution state")
            return self
        if (command.vehicle_id != self.vehicle_id or command.fleet_instance_id != self.fleet_instance_id
                or physical.active_dispatch_id != command.dispatch_id
                or physical.availability != "UNAVAILABLE" or physical.distance_travelled_km is None
                or self.accepted is None):
            raise ValueError("checkpoint execution identity or physical state is inconsistent")
        if self.accepted:
            if (command.route is None or command.route.source != "ONEMAP"
                    or physical.relocation_state not in ("DISPATCHED", "RELOCATING", "ARRIVED", "FAILED")
                    or physical.charging_state != "NOT_CHARGING" or physical.parked_facility_id is not None
                    or physical.distance_travelled_km > command.route.distance_km + 1e-9):
                raise ValueError("accepted command requires valid road progress")
        elif physical.relocation_state != "IDLE" or physical.distance_travelled_km != 0:
            raise ValueError("rejected command cannot move")
        ack = self.acknowledgement
        if ack and (ack.command_id != command.command_id or ack.vehicle_id != self.vehicle_id
                    or ack.accepted != self.accepted or ack.reason != self.reason):
            raise ValueError("acknowledgement must match the recorded command decision")
        if self.acknowledged and ack is None:
            raise ValueError("acknowledged checkpoint requires its original acknowledgement")
        event = self.pending_event
        if event and (not self.accepted or event.command_id != command.command_id or event.vehicle_id != self.vehicle_id
                      or event.sequence >= self.next_event_sequence
                      or event.distance_travelled_km > physical.distance_travelled_km):
            raise ValueError("pending event must match checkpoint identity and progress")
        if event and event.kind in ("ARRIVED", "FAILED") and (
                physical.relocation_state != event.kind
                or event.distance_travelled_km != physical.distance_travelled_km):
            raise ValueError("terminal event must match the saved physical lifecycle and distance")
        return self


class SimulatorSessionRequest(Model):
    request_id: UUID
    action: Literal["ACQUIRE", "RENEW"]
    expected_fleet_instance_id: UUID
    expected_control_version: int = Field(ge=1)
    expected_generation: int = Field(ge=0)
    expected_owner_revision: int = Field(ge=0)
    expected_checkpoint_revision: int = Field(ge=0)
    session_id: UUID | None = None
    dispatch_id: UUID | None = None
    command_id: UUID | None = None

    @model_validator(mode="after")
    def session_identity(self):
        if (self.dispatch_id is None) != (self.command_id is None):
            raise ValueError("dispatch and command identity must be supplied together")
        if self.action == "ACQUIRE":
            if self.session_id is not None or self.expected_generation != 0 or self.expected_owner_revision != 0:
                raise ValueError("initial acquire requires generation/revision zero and no session")
        elif self.session_id is None or self.expected_generation == 0 or self.expected_owner_revision == 0:
            raise ValueError("renewal requires an existing generation, revision and session")
        return self


class SimulatorSession(Model):
    session_id: UUID
    generation: int = Field(ge=1)
    owner_revision: int = Field(ge=1)
    expires_at: AwareDatetime
    checkpoint_revision: int = Field(ge=0)
    checkpoint: SimulatorCheckpoint | None = None
    durable: bool


class SimulatorCheckpointRequest(Model):
    request_id: UUID
    session_id: UUID
    generation: int = Field(ge=1)
    expected_checkpoint_revision: int = Field(ge=0)
    checkpoint: SimulatorCheckpoint
    publish_telemetry: bool = False


class SimulatorCheckpointReceipt(Model):
    request_id: UUID
    generation: int = Field(ge=1)
    checkpoint_revision: int = Field(ge=1)
    command_fingerprint: str | None
    durable: bool


class SimulatorCheckpointSnapshot(Model):
    fleet_instance_id: UUID
    control_version: int
    configured: bool
    generation: int = Field(ge=0)
    checkpoint_revision: int = Field(ge=0)
    checkpoint: SimulatorCheckpoint | None
    configuration: SimulatorConfiguration


class RecoveryRequest(Model):
    request_id: UUID
    action: Literal["RESUME_SIMULATION"] = "RESUME_SIMULATION"
    expected_assessment_revision: str


class SimulatorRestoreRequest(Model):
    action: Literal["RESTORE"] = "RESTORE"
    request_id: UUID
    dispatch_id: UUID
    expected_fleet_instance_id: UUID
    expected_control_version: int
    expected_generation: int = Field(ge=0)
    expected_checkpoint_revision: int = Field(ge=0)
    checkpoint: SimulatorCheckpoint


class RecoveryHandoffRequest(Model):
    consumer_id: UUID
    simulation: SimulatorConfiguration
