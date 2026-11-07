"""Inspect persisted dispatch evidence without invoking mutating reconciliation."""
import asyncio
from copy import deepcopy
import hashlib
import json
from uuid import UUID

from fastapi import HTTPException

from ev_fleet.contracts.dispatch import Dispatch, VehicleClaim
from ev_fleet.contracts.facilities import Facility, ParkingHold
from ev_fleet.contracts.recovery import (RecoveryAssessment, RecoveryBlocker, RecoveryEvidence,
    SimulatorCheckpoint, SimulatorCheckpointSnapshot, SimulatorRestoreRequest, SimulatorSession, idle_before_command)
from ev_fleet.contracts.vehicles import FleetSnapshot, VehicleState, TelemetryUpdate
from ev_fleet.simulator.movement import Movement
from ev_fleet.rebalancing.traffic import assess_traffic
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


def evidence_blockers(dispatch, fleet, vehicle, claim, hold, facility, now, stale_seconds, checkpoint=None):
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
        telemetry = checkpoint.physical if checkpoint else vehicle.telemetry
        awaiting_telemetry = not checkpoint and telemetry_sync_pending(dispatch, vehicle)
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


def local_snapshot(record):
    return deepcopy({"dispatch": record.view(), "ack": record.ack, "event_sequence": record.event_sequence,
        "release_outcome": record.release_outcome, "parking_request": record.parking_request,
        "claim_request": record.claim_request, "claimed": record.claimed, "recovery": record.recovery, "resolution": record.resolution})


def same_local(left, right):
    left, right = deepcopy(left), deepcopy(right)
    left["dispatch"].pop("updated_at", None)
    right["dispatch"].pop("updated_at", None)
    return left == right


def unstarted_checkpoint(local, vehicle, checkpoint, *, allow_completed_charge=False):
    """An idle checkpoint can precede a claim that was never delivered."""
    dispatch, claim = Dispatch.model_validate(local["dispatch"]), vehicle.active_claim
    physical = checkpoint.physical
    return bool(claim and claim.status == "ACTIVE" and local["claimed"] and dispatch.claim_held is True
        and claim.dispatch_id == dispatch.dispatch_id
        and claim.command_id == dispatch.command.command_id
        and checkpoint.vehicle_id == vehicle.vehicle_id
        and checkpoint.fleet_instance_id == claim.fleet_instance_id
        and dispatch.state not in ("FAILED", "SUCCEEDED", "REJECTED")
        and dispatch.delivery_attempts == 0 and dispatch.acknowledged_at is None
        and dispatch.distance_travelled_km == 0 and local["ack"] is None
        and local["event_sequence"] == -1 and local["release_outcome"] is None
        and (physical.charging_state == "NOT_CHARGING"
             or allow_completed_charge and physical.charging_state == "COMPLETE")
        and idle_before_command(checkpoint, dispatch.command.command_id, claim.origin, vehicle.telemetry))


def candidate_checkpoint(local, fleet, vehicle, saved):
    dispatch = Dispatch.model_validate(local["dispatch"])
    checkpoint = saved.checkpoint
    unstarted = checkpoint is not None and unstarted_checkpoint(local, vehicle, checkpoint)
    if unstarted:
        # Prepare the never-delivered original command without advancing motion
        # or fabricating an ACK. The consumer sends this durable ACK intent.
        movement = Movement.from_checkpoint(checkpoint, checkpoint.simulation)
        if not movement.accept(dispatch.command.model_dump(mode="json")):
            raise ValueError("UNSTARTED_COMMAND_NOT_ACCEPTABLE")
        state = movement.snapshot()
        physical = TelemetryUpdate.model_validate({
            **checkpoint.physical.model_dump(mode="json"), **state.pop("physical")})
        physical = physical.model_copy(update={"sequence": physical.sequence + 1})
        checkpoint = SimulatorCheckpoint(vehicle_id=vehicle.vehicle_id,
            fleet_instance_id=fleet.instance_id, physical=physical, **state,
            acknowledgement={"command_id": dispatch.command.command_id,
                "vehicle_id": vehicle.vehicle_id, "accepted": True, "reason": None},
            next_event_sequence=0, simulation=checkpoint.simulation)
    if checkpoint is None:
        physical = vehicle.telemetry
        if (physical is None or physical.active_dispatch_id != dispatch.dispatch_id
                or physical.relocation_state not in ("RELOCATING", "ARRIVED")
                or local["ack"] and not local["ack"]["accepted"]):
            raise ValueError("LEGACY_EXECUTION_STATE_UNSUPPORTED")
        checkpoint = SimulatorCheckpoint(vehicle_id=vehicle.vehicle_id, fleet_instance_id=fleet.instance_id,
            physical=physical, command=dispatch.command, accepted=True,
            acknowledgement=local["ack"] or {"command_id": dispatch.command.command_id,
                "vehicle_id": vehicle.vehicle_id, "accepted": True, "reason": None},
            acknowledged=local["ack"] is not None, next_event_sequence=local["event_sequence"] + 1,
            simulation=saved.configuration)
    if checkpoint.command is not None:
        if ((dispatch.delivery_attempts < 1 and not unstarted)
                or checkpoint.command != dispatch.command or checkpoint.vehicle_id != vehicle.vehicle_id
                or checkpoint.fleet_instance_id != fleet.instance_id
                or checkpoint.physical.distance_travelled_km < dispatch.distance_travelled_km
                or checkpoint.next_event_sequence <= local["event_sequence"]
                or local["ack"] and local["ack"]["accepted"] != checkpoint.accepted):
            raise ValueError("CHECKPOINT_EXECUTION_EVIDENCE_CONFLICT")
    elif (vehicle.active_claim is not None or dispatch.claim_held is not False
            or checkpoint.completed_command_id != dispatch.command.command_id):
        raise ValueError("CHECKPOINT_COMMAND_MISMATCH")
    if checkpoint.simulation is None:
        raise ValueError("CHECKPOINT_CONFIGURATION_MISSING")
    if (checkpoint.simulation.capacity_kwh != vehicle.battery_capacity_kwh
            or checkpoint.simulation.consumption_kwh_per_km != vehicle.consumption_kwh_per_km
            or checkpoint.simulation.time_scale != saved.configuration.time_scale):
        raise ValueError("CHECKPOINT_CONFIGURATION_MISMATCH")
    Movement.from_checkpoint(checkpoint, checkpoint.simulation)
    if checkpoint.physical.relocation_state == "FAILED" or dispatch.state == "FAILED":
        raise ValueError("EXECUTION_FAILED")
    if checkpoint.command and checkpoint.accepted and checkpoint.physical.relocation_state != "ARRIVED":
        remaining = max(0, checkpoint.command.route.distance_km - checkpoint.physical.distance_travelled_km)
        arrival_soc = checkpoint.physical.soc_pct - 100 * remaining * vehicle.consumption_kwh_per_km * (
            checkpoint.command.energy_margin_factor / vehicle.battery_capacity_kwh)
        if arrival_soc < checkpoint.command.min_reserve_pct:
            raise ValueError("RECOVERY_BELOW_RESERVE")
    return checkpoint


async def assess_recovery(service, dispatch_id, *, details=False):
    record = service.store.get(dispatch_id)
    async with record.lock:
        local = local_snapshot(record)
    dispatch = Dispatch.model_validate(local["dispatch"])
    command = dispatch.command
    evidence = RecoveryEvidence(dispatch_state=dispatch.state, failure_reason=dispatch.failure_reason,
        command_id=command.command_id if command else None, last_event_sequence=local["event_sequence"],
        acknowledgement_accepted=local["ack"]["accepted"] if local["ack"] else None,
        release_outcome=local["release_outcome"], claim_held=dispatch.claim_held)
    blockers, observations = [], {}
    rejected = (dispatch.state == "REJECTED" and command is None and dispatch.vehicle_id is None
        and dispatch.claim_held is False and not local["claimed"] and local["claim_request"] is None
        and local["parking_request"] is None and dispatch.parking_facility_id is None
        and dispatch.parking_status is None and dispatch.delivery_attempts == 0 and local["ack"] is None
        and local["event_sequence"] == -1 and local["release_outcome"] is None)
    if command is None or dispatch.vehicle_id is None:
        if not rejected:
            blockers.append(RecoveryBlocker(code="DISPATCH_COMMAND_MISSING", detail="No complete execution identity is recorded."))
    else:
        reads = {"fleet": (service.settings.fleet_url + "/api/v1/vehicles", FleetSnapshot),
            "claim": (f"{service.settings.fleet_url}/api/v1/vehicles/{dispatch.vehicle_id}/claims/{dispatch.dispatch_id}", VehicleClaim),
            "saved": (f"{service.settings.fleet_url}/api/v1/vehicles/{dispatch.vehicle_id}/simulator-checkpoint", SimulatorCheckpointSnapshot)}
        if dispatch.parking_facility_id:
            reads.update({"hold": (f"{service.settings.facilities_url}/api/v1/holds/{dispatch.dispatch_id}", ParkingHold),
                "facility": (f"{service.settings.facilities_url}/api/v1/facilities/{dispatch.parking_facility_id}", Facility)})
        async def read(name, spec):
            try:
                return name, await request_json(service.client, "GET", spec[0], model=spec[1], timeout_seconds=2)
            except HTTPException as exc:
                return name, RecoveryBlocker(code=f"{name.upper()}_UNAVAILABLE", detail=str(exc.detail))
        for name, result in await asyncio.gather(*(read(name, spec) for name, spec in reads.items())):
            if isinstance(result, RecoveryBlocker):
                blockers.append(result)
            else:
                observations[name] = result
    now = service.clock.now()
    fleet, claim, saved = observations.get("fleet"), observations.get("claim"), observations.get("saved")
    vehicle = next((v for v in fleet.vehicles if v.vehicle_id == dispatch.vehicle_id), None) if fleet else None
    selected = saved.checkpoint if saved and saved.checkpoint and (saved.checkpoint.command == command
        or saved.checkpoint.command is None and command
        and saved.checkpoint.completed_command_id == command.command_id
        or vehicle and command and unstarted_checkpoint(local, vehicle, saved.checkpoint)) else None
    if command and dispatch.vehicle_id:
        blockers.extend(evidence_blockers(dispatch, fleet, vehicle, claim, observations.get("hold"),
            observations.get("facility"), now, service.settings.stale_seconds, selected))
    async with record.lock:
        if not same_local(local_snapshot(record), local):
            blockers.append(RecoveryBlocker(code="DISPATCH_EVIDENCE_CHANGED", detail="Refresh the changed assessment."))
    physical = selected.physical if selected else vehicle.telemetry if vehicle else None
    if fleet:
        evidence.fleet_instance_id, evidence.fleet_snapshot_version = fleet.instance_id, fleet.snapshot_version
    if claim:
        evidence.claim_status = claim.status
    if observations.get("hold"):
        evidence.parking_status = observations["hold"].status
    if vehicle:
        evidence.fleet_control_version, evidence.telemetry_age_seconds = vehicle.control_version, vehicle.telemetry_age_seconds
    if physical:
        evidence.checkpoint_source = "SIMULATOR_CHECKPOINT" if selected else "LEGACY_TELEMETRY"
        evidence.checkpoint_at, evidence.recorded_position = physical.observed_at, physical.position
        evidence.recorded_soc_pct, evidence.recorded_distance_km = physical.soc_pct, physical.distance_travelled_km
    candidate = None
    stopped = vehicle is not None and vehicle.stale and (not vehicle.simulator_recovery
        or vehicle.simulator_recovery.expires_at <= now)
    if not blockers and stopped and saved and saved.configured:
        try:
            candidate = candidate_checkpoint(local, fleet, vehicle, saved)
        except ValueError as exc:
            blockers.append(RecoveryBlocker(code="RECOVERY_EVIDENCE_INVALID", detail=str(exc)))
    if dispatch.state == "FAILED":
        blockers.insert(0, RecoveryBlocker(code="EXECUTION_FAILED", detail="Physical execution failure cannot be resumed."))
    if blockers:
        status, reason = "BLOCKED", blockers[0].code
    elif rejected:
        status, reason = "NOT_REQUIRED", "REJECTED_BEFORE_ACQUISITION"
    elif candidate:
        status, reason = "REQUIRED", "SIMULATOR_RECOVERY_REQUIRED"
    elif stopped and (not saved or not saved.configured):
        status, reason = "BLOCKED", "SIMULATOR_RECOVERY_NOT_CONFIGURED"
    elif claim.status == "RELEASED":
        status, reason = "NOT_REQUIRED", "JOURNEY_FINISHED"
    elif telemetry_sync_pending(dispatch, vehicle):
        status, reason = "NOT_REQUIRED", "TELEMETRY_SYNCHRONIZATION_PENDING"
    elif dispatch.failure_reason == "ACK_TIMEOUT" and dispatch.acknowledged_at is None:
        status, reason = "NOT_REQUIRED", "ACK_TIMEOUT_RECONCILIATION_PENDING"
    else:
        status, reason = "NOT_REQUIRED", "EXECUTION_HEALTHY"
    operation = dispatch.recovery
    if operation and not service.repository.ready:
        operation = operation.model_copy(update={"intent_fingerprint": None})
    ending = local["resolution"] and local["resolution"]["status"] in ("REQUESTED", "CLEANUP_PENDING", "ENDED")
    active = operation and operation.status in ("REQUESTED", "RESTORING")
    if active:
        status, reason = "REQUIRED", operation.reason_code or operation.status
    # Hash target evidence, not snapshot times/other vehicles/age counters.
    material_dispatch = dispatch.model_dump(mode="json", exclude={"updated_at", "recovery"})
    material_vehicle = vehicle.model_dump(mode="json", exclude={"telemetry_age_seconds"}) if vehicle else None
    revision = hashlib.sha256(json.dumps({"dispatch": material_dispatch, "vehicle": material_vehicle,
        "fleet_instance_id": str(fleet.instance_id) if fleet else None,
        "saved": saved.model_dump(mode="json") if saved else None,
        "claim": claim.model_dump(mode="json") if claim else None,
        "hold": observations["hold"].model_dump(mode="json") if observations.get("hold") else None,
        "facility": observations["facility"].model_dump(mode="json") if observations.get("facility") else None,
        "event_sequence": local["event_sequence"], "ack": local["ack"], "release": local["release_outcome"]},
        sort_keys=True, default=str).encode()).hexdigest()
    result = RecoveryAssessment(dispatch_id=dispatch.dispatch_id, vehicle_id=dispatch.vehicle_id,
        assessed_at=now, assessment_revision=revision, status=status, reason_code=reason,
        can_resume=candidate is not None and not blockers and not active and not ending,
        supported_action="RESUME_SIMULATION" if candidate is not None and not blockers and not active and not ending else None,
        blockers=blockers, evidence=evidence, operation=operation)
    return (result, local, observations, candidate) if details else result


class RecoveryCoordinator:
    """One persisted request per dispatch, driven by the existing reconcile loop."""
    def __init__(self, service):
        self.service = service

    def ensure_ready(self):
        if not self.service.repository.ready:
            raise HTTPException(503, "RECOVERY_PERSISTENCE_UNAVAILABLE")

    async def request(self, dispatch_id, body):
        self.ensure_ready()
        service, record = self.service, self.service.store.get(dispatch_id)
        async with record.lock:
            if record.recovery and record.recovery["request_id"] == str(body.request_id):
                if record.recovery["request"] != body.model_dump(mode="json"):
                    raise HTTPException(409, "RECOVERY_REQUEST_ID_CONFLICT")
                return record.view()["recovery"]
        assessment, local, observations, checkpoint = await assess_recovery(service, dispatch_id, details=True)
        if body.expected_assessment_revision != assessment.assessment_revision:
            raise HTTPException(409, "RECOVERY_ASSESSMENT_CHANGED")
        if not assessment.can_resume or checkpoint is None:
            raise HTTPException(409, assessment.reason_code)
        if service.traffic_enabled and checkpoint.command and checkpoint.accepted and checkpoint.physical.relocation_state != "ARRIVED":
            traffic = await request_json(service.client, "GET", service.settings.rebalancing_url + "/api/v1/traffic/conditions")
            route = checkpoint.command.route
            if assess_traffic([p.model_dump() for p in route.geometry], route.distance_km,
                              route.duration_seconds, traffic, service.clock.now())["blocked"]:
                raise HTTPException(409, "RECOVERY_ROUTE_BLOCKED")
        saved = observations["saved"]
        intent = SimulatorRestoreRequest(request_id=body.request_id, dispatch_id=dispatch_id,
            expected_fleet_instance_id=saved.fleet_instance_id, expected_control_version=saved.control_version,
            expected_generation=saved.generation, expected_checkpoint_revision=saved.checkpoint_revision,
            checkpoint=checkpoint)
        digest = hashlib.sha256(json.dumps(intent.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        async with record.lock:
            if not same_local(local_snapshot(record), local):
                raise HTTPException(409, "RECOVERY_ASSESSMENT_CHANGED")
            record.recovery = {"request_id": str(body.request_id), "request": body.model_dump(mode="json"),
                "restore": intent.model_dump(mode="json"), "intent_fingerprint": digest,
                "status": "REQUESTED", "reason_code": None, "generation": None,
                "confirmed_paused": False, "session": None, "consumer_id": None}
            service.planning_revision += 1
            # Gate ACK/events durably before asking Fleet to rotate ownership.
            await service.persist()
            return record.view()["recovery"]

    async def advance(self, record):
        self.ensure_ready()
        service = self.service
        async with record.lock:
            operation = deepcopy(record.recovery)
        if operation is None:
            return
        if (operation["status"] == "RECOVERED" and record.data["state"] in ("SUCCEEDED", "REJECTED")
                and record.data["claim_held"] is False):
            return  # Later work on this vehicle must not rewrite historical recovery outcomes.
        if operation["status"] == "REQUESTED":
            try:
                receipt = await request_json(service.client, "POST",
                    f"{service.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}/simulator-sessions",
                    operation["restore"], SimulatorSession, timeout_seconds=12)
                changes = {"session": receipt.model_dump(mode="json"), "generation": receipt.generation,
                           "status": "RESTORING", "reason_code": "WAITING_FOR_SIMULATOR", "confirmed_paused": True}
            except HTTPException as exc:
                changes = {"reason_code": str(exc.detail)}
                if exc.status_code < 500:
                    changes["status"] = "BLOCKED"
            async with record.lock:
                if not record.recovery or record.recovery["request_id"] != operation["request_id"] or record.recovery["status"] != "REQUESTED":
                    return
                record.recovery.update(changes)
                service.planning_revision += 1
                await service.persist()
            return
        if operation["session"] is None:
            return
        vehicle = await request_json(service.client, "GET",
            f"{service.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}", model=VehicleState)
        owner, telemetry = vehicle.simulator_recovery, vehicle.telemetry
        changes = {}
        if owner is None or owner.generation != operation["generation"]:
            changes = {"status": "BLOCKED", "reason_code": "RECOVERY_OWNER_CHANGED", "confirmed_paused": False}
        elif (not vehicle.stale and telemetry and str(telemetry.session_id) == operation["session"]["session_id"]
                and (str(telemetry.active_dispatch_id) == record.data["dispatch_id"]
                     or telemetry.active_dispatch_id is None and vehicle.active_claim is None)):
            changes = {"status": "RECOVERED", "reason_code": None, "confirmed_paused": False}
        elif owner.expires_at <= service.clock.now():
            changes = {"status": "BLOCKED", "reason_code": "RECOVERY_LEASE_EXPIRED", "confirmed_paused": True}
        async with record.lock:
            if record.recovery and record.recovery["request_id"] == operation["request_id"] and any(
                    record.recovery.get(k) != v for k, v in changes.items()):
                record.recovery.update(changes)
                service.planning_revision += 1
                await service.persist()

    async def handoff(self, dispatch_id, body):
        service, record = self.service, self.service.store.get(dispatch_id)
        async with record.lock:
            self.ensure_ready()
            operation = record.recovery
            if operation is None or operation["status"] != "RESTORING" or operation["session"] is None:
                raise HTTPException(409, "RECOVERY_HANDOFF_NOT_READY")
            receipt = SimulatorSession.model_validate(operation["session"])
            if receipt.expires_at <= service.clock.now():
                raise HTTPException(409, "RECOVERY_LEASE_EXPIRED")
            if receipt.checkpoint.simulation != body.simulation:
                raise HTTPException(409, "RECOVERY_SIMULATION_CONFIGURATION_MISMATCH")
            consumer = str(body.consumer_id)
            if operation["consumer_id"] not in (None, consumer):
                raise HTTPException(409, "RECOVERY_HANDOFF_ALREADY_CLAIMED")
            if operation["consumer_id"] is None:
                operation.update(consumer_id=consumer, confirmed_paused=False, reason_code="RESTORING_SIMULATOR")
                if record.data["delivery_attempts"] == 0:
                    # Delivering the prepared original command is part of this
                    # handoff, not an invented ACK or a second approval.
                    record.data["delivery_attempts"] = 1
                    record.sent_tick = service.clock.monotonic()
                service.planning_revision += 1
                # Planning must stop assuming paused before the node can resume.
                await service.persist()
            return receipt

    async def guard(self, record, generation, session_id):
        # Called while holding the same record lock as request admission.
        if record.recovery and record.recovery["status"] == "REQUESTED":
            raise HTTPException(409, "RECOVERY_OWNERSHIP_PENDING")
        vehicle = await request_json(self.service.client, "GET",
            f"{self.service.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}", model=VehicleState)
        owner = vehicle.simulator_recovery
        if owner is None and generation is None and session_id is None and record.recovery is None:
            return  # Existing non-opted-in simulator compatibility.
        if (owner is None or owner.status != "ACTIVE" or generation != owner.generation
                or owner.expires_at <= self.service.clock.now() or vehicle.stale or vehicle.telemetry is None
                or vehicle.telemetry.session_id != session_id):
            raise HTTPException(409, "SIMULATOR_OWNER_CONFLICT")
        if record.recovery and record.recovery["generation"] != generation:
            raise HTTPException(409, "SIMULATOR_OWNER_CONFLICT")
