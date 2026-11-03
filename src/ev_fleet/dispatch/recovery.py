"""Inspect persisted dispatch evidence without invoking mutating reconciliation."""
import asyncio
from copy import deepcopy
import hashlib
import json

from fastapi import HTTPException

from ev_fleet.contracts.dispatch import Dispatch, VehicleClaim
from ev_fleet.contracts.facilities import Facility, ParkingHold
from ev_fleet.contracts.recovery import RecoveryAssessment, RecoveryBlocker, RecoveryEvidence
from ev_fleet.contracts.vehicles import FleetSnapshot
from .clients import request_json


def telemetry_sync_pending(dispatch, vehicle):
    """Fresh telemetry may predate an ACK or a separately published progress event."""
    if (vehicle is None or vehicle.stale or vehicle.connectivity != "ONLINE"
            or vehicle.telemetry is None or dispatch.state not in ("ACKNOWLEDGED", "IN_PROGRESS")):
        return False
    telemetry = vehicle.telemetry
    if telemetry.active_dispatch_id is None and telemetry.relocation_state == "IDLE":
        return bool(dispatch.acknowledged_at and telemetry.observed_at <= dispatch.acknowledged_at)
    if telemetry.active_dispatch_id == dispatch.dispatch_id:
        return bool(dispatch.last_contact_at and telemetry.observed_at <= dispatch.last_contact_at
                    and (telemetry.distance_travelled_km is None
                         or telemetry.distance_travelled_km < dispatch.distance_travelled_km))
    return False


def evidence_blockers(dispatch, fleet, vehicle, claim, hold, facility, now, stale_seconds):
    """Pure consistency checks; missing observations never establish availability."""
    blockers = []

    def block(code, detail):
        blockers.append(RecoveryBlocker(code=code, detail=detail))

    command = dispatch.command
    if fleet is not None:
        if not -5 <= (now - fleet.snapshot_at).total_seconds() < stale_seconds:
            block("FLEET_SNAPSHOT_STALE", "The Fleet snapshot is not current.")
        if command.fleet_instance_id != fleet.instance_id:
            block("FLEET_INSTANCE_MISMATCH", "The command belongs to a different Fleet instance.")
        if vehicle is None:
            block("VEHICLE_NOT_FOUND", "The dispatch vehicle is missing from Fleet.")
    if command.dispatch_id != dispatch.dispatch_id or command.vehicle_id != dispatch.vehicle_id:
        block("COMMAND_IDENTITY_MISMATCH", "The command does not identify this dispatch and vehicle.")
    if claim is not None:
        if (claim.dispatch_id != dispatch.dispatch_id or claim.command_id != command.command_id
                or claim.vehicle_id != dispatch.vehicle_id
                or claim.fleet_instance_id != command.fleet_instance_id
                or claim.target_zone_id != command.target_zone_id
                or claim.target_position != command.target_position):
            block("CLAIM_COMMAND_MISMATCH", "The claim receipt and command disagree.")
        if vehicle is not None and (
                claim.status == "ACTIVE" and vehicle.active_claim != claim
                or claim.status == "RELEASED" and vehicle.active_claim is not None
                and vehicle.active_claim.dispatch_id == dispatch.dispatch_id):
            block("CLAIM_OWNERSHIP_MISMATCH", "The receipt and vehicle's active ownership disagree.")
    if hold is not None:
        if (hold.dispatch_id != dispatch.dispatch_id or hold.vehicle_id != dispatch.vehicle_id
                or hold.facility_id != dispatch.parking_facility_id):
            block("PARKING_HOLD_MISMATCH", "The facility hold does not match this journey.")
        if claim is not None and claim.status == "ACTIVE" and hold.status == "RELEASED":
            block("PARKING_HOLD_RELEASED", "The retained journey no longer has its facility hold.")
    if facility is not None:
        if (facility.facility_id != dispatch.parking_facility_id
                or facility.zone_id != command.target_zone_id):
            block("FACILITY_IDENTITY_MISMATCH", "The destination facility and command disagree.")
        if claim is not None and claim.status == "ACTIVE" and (facility.status != "OPEN" or not facility.is_fresh(now)):
            block("FACILITY_UNAVAILABLE", "The destination is closed or its observations are stale.")
    if vehicle is not None:
        if claim is not None and claim.status == "ACTIVE" and (vehicle.active_rental or vehicle.active_charging_task):
            block("VEHICLE_TASK_CONFLICT", "Rental or charging-task recovery is outside relocation recovery.")
        telemetry = vehicle.telemetry
        awaiting_telemetry = telemetry_sync_pending(dispatch, vehicle)
        if claim is not None and claim.status == "ACTIVE":
            if telemetry is None:
                block("TELEMETRY_MISSING", "No recorded physical state is available.")
            elif telemetry.active_dispatch_id != dispatch.dispatch_id:
                # Idle origin telemetry before command delivery is normal.
                if (telemetry.active_dispatch_id is not None or telemetry.relocation_state != "IDLE"
                        or dispatch.acknowledged_at and not awaiting_telemetry):
                    block("TELEMETRY_DISPATCH_MISMATCH", "Recorded telemetry does not identify this journey.")
            elif not awaiting_telemetry and (telemetry.distance_travelled_km is None
                    or telemetry.distance_travelled_km < dispatch.distance_travelled_km):
                block("CHECKPOINT_PROGRESS_INCOMPLETE", "Telemetry cannot account for all accepted dispatch progress.")
            if telemetry is not None and telemetry.active_dispatch_id == dispatch.dispatch_id:
                if command.route is None:
                    block("ROUTE_MISSING", "No recorded road route is available for this journey.")
                elif telemetry.distance_travelled_km is not None and telemetry.distance_travelled_km > command.route.distance_km:
                    block("CHECKPOINT_PROGRESS_INVALID", "Recorded progress exceeds the command's route length.")
    return blockers


async def assess_recovery(service, dispatch_id):
    record = service.store.get(dispatch_id)  # Only unknown dispatches produce endpoint 404.

    def snapshot():
        return deepcopy({"dispatch": record.view(), "ack": record.ack,
                         "event_sequence": record.event_sequence, "release_outcome": record.release_outcome,
                         "parking_request": record.parking_request, "claim_request": record.claim_request,
                         "claimed": record.claimed})

    async with record.lock:
        local = snapshot()
    dispatch = Dispatch.model_validate(local["dispatch"])
    command = dispatch.command
    evidence = RecoveryEvidence(
        dispatch_state=dispatch.state, failure_reason=dispatch.failure_reason,
        command_id=command.command_id if command else None,
        last_event_sequence=local["event_sequence"],
        acknowledgement_accepted=local["ack"]["accepted"] if local["ack"] else None,
        release_outcome=local["release_outcome"], claim_held=dispatch.claim_held)
    blockers = []
    observations = {}
    # acquire() is reached only after durable claim intent. A bare rejection
    # before either resource intent exists cannot have started a journey.
    rejected_before_acquisition = (
        dispatch.state == "REJECTED" and command is None and dispatch.vehicle_id is None
        and dispatch.claim_held is False and not local["claimed"]
        and local["claim_request"] is None and local["parking_request"] is None
        and dispatch.parking_facility_id is None and dispatch.parking_status is None
        and dispatch.delivery_attempts == 0 and local["ack"] is None
        and local["event_sequence"] == -1 and local["release_outcome"] is None)
    if command is None or dispatch.vehicle_id is None:
        if not rejected_before_acquisition:
            blockers.append(RecoveryBlocker(code="DISPATCH_COMMAND_MISSING", detail="No complete execution identity is recorded."))
    else:
        reads = {
            "fleet": (service.settings.fleet_url + "/api/v1/vehicles", FleetSnapshot),
            "claim": (f"{service.settings.fleet_url}/api/v1/vehicles/{dispatch.vehicle_id}/claims/{dispatch.dispatch_id}", VehicleClaim),
        }
        if dispatch.parking_facility_id:
            reads.update({
                "hold": (f"{service.settings.facilities_url}/api/v1/holds/{dispatch.dispatch_id}", ParkingHold),
                "facility": (f"{service.settings.facilities_url}/api/v1/facilities/{dispatch.parking_facility_id}", Facility),
            })
        elif local["parking_request"]:
            blockers.append(RecoveryBlocker(code="PARKING_RESERVATION_MISSING", detail="No destination reservation is recorded."))

        async def read(name, url, model):
            try:
                return name, await request_json(service.client, "GET", url, model=model, timeout_seconds=2)
            except HTTPException as exc:
                code = f"{name.upper()}_NOT_FOUND" if exc.status_code == 404 else f"{name.upper()}_UNAVAILABLE"
                return name, RecoveryBlocker(code=code, detail=f"{name} evidence could not be read ({exc.detail}).")

        for name, result in await asyncio.gather(*(read(name, *spec) for name, spec in reads.items())):
            if isinstance(result, RecoveryBlocker):
                blockers.append(result)
            else:
                observations[name] = result

    now = service.clock.now()
    fleet, claim = observations.get("fleet"), observations.get("claim")
    hold, facility = observations.get("hold"), observations.get("facility")
    vehicle = next((v for v in fleet.vehicles if v.vehicle_id == dispatch.vehicle_id), None) if fleet else None
    if command is not None and dispatch.vehicle_id is not None:
        blockers.extend(evidence_blockers(dispatch, fleet, vehicle, claim, hold, facility, now, service.settings.stale_seconds))
    async with record.lock:
        if snapshot() != local:
            blockers.append(RecoveryBlocker(code="DISPATCH_EVIDENCE_CHANGED", detail="Dispatch changed during inspection; refresh the assessment."))
    if fleet:
        evidence.fleet_instance_id = fleet.instance_id
        evidence.fleet_snapshot_version = fleet.snapshot_version
    if claim:
        evidence.claim_status = claim.status
    if hold:
        evidence.parking_status = hold.status
    if vehicle:
        evidence.fleet_control_version = vehicle.control_version
        evidence.telemetry_age_seconds = vehicle.telemetry_age_seconds
        if vehicle.telemetry:
            evidence.checkpoint_source = "LEGACY_TELEMETRY"
            evidence.checkpoint_at = vehicle.telemetry.observed_at
            evidence.recorded_position = vehicle.telemetry.position
            evidence.recorded_soc_pct = vehicle.telemetry.soc_pct
            evidence.recorded_distance_km = vehicle.telemetry.distance_travelled_km

    if dispatch.state == "FAILED" or (vehicle and vehicle.telemetry
            and vehicle.telemetry.active_dispatch_id == dispatch.dispatch_id
            and vehicle.telemetry.relocation_state == "FAILED"):
        blockers.insert(0, RecoveryBlocker(code="EXECUTION_FAILED", detail="Execution failure does not establish availability or permit automatic resume."))
    if blockers:
        status, reason = "BLOCKED", blockers[0].code
    elif rejected_before_acquisition:
        status, reason = "NOT_REQUIRED", "REJECTED_BEFORE_ACQUISITION"
    elif claim.status == "RELEASED":
        status, reason = "NOT_REQUIRED", "CLAIM_RELEASED_RECONCILIATION_PENDING" if dispatch.claim_held is not False else "JOURNEY_FINISHED"
    elif vehicle.stale or vehicle.connectivity != "ONLINE":
        status, reason = "REQUIRED", "LEGACY_JOURNEY_REQUIRES_RECOVERY"
        blockers = [RecoveryBlocker(code="RECOVERY_NOT_IMPLEMENTED", detail="Only diagnosis is available; simulator identity, checkpoint validation and guarded recovery are not implemented.")]
    elif telemetry_sync_pending(dispatch, vehicle):
        status, reason = "NOT_REQUIRED", "TELEMETRY_SYNCHRONIZATION_PENDING"
    elif dispatch.failure_reason == "ACK_TIMEOUT" and dispatch.acknowledged_at is None:
        status, reason = "NOT_REQUIRED", "ACK_TIMEOUT_RECONCILIATION_PENDING"
    elif dispatch.state in ("UNKNOWN", "SUCCEEDED", "REJECTED") or local["release_outcome"]:
        status, reason = "BLOCKED", "LIFECYCLE_RECONCILIATION_REQUIRED"
        blockers = [RecoveryBlocker(code=reason, detail="Fresh execution and claim evidence require lifecycle reconciliation, not simulator takeover.")]
    else:
        status, reason = "NOT_REQUIRED", "EXECUTION_HEALTHY"

    revision_data = {**local, "observations": {k: v.model_dump(mode="json") for k, v in observations.items()},
                     "blockers": [b.model_dump() for b in blockers]}
    revision = hashlib.sha256(json.dumps(revision_data, sort_keys=True, default=str).encode()).hexdigest()
    return RecoveryAssessment(dispatch_id=dispatch.dispatch_id, vehicle_id=dispatch.vehicle_id,
        assessed_at=now, assessment_revision=revision, status=status, reason_code=reason,
        blockers=blockers, evidence=evidence)
