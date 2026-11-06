"""Operator-authorized relocation closure over Fleet and Facilities receipts."""
from copy import deepcopy
import hashlib
from uuid import UUID

from fastapi import HTTPException

from ev_fleet.contracts.dispatch import Dispatch, VehicleClaim
from ev_fleet.contracts.facilities import ParkingHold
from ev_fleet.contracts.recovery import (RelocationResolutionAssessment, RelocationResolutionOperation,
    SimulatorStopRequest, stop_checkpoint_evidence)
from .clients import request_json
from .recovery import assess_recovery, local_snapshot, same_local, unstarted_checkpoint


def stop_pending(record):
    return bool(record.resolution and record.resolution["status"] == "REQUESTED")


def operation_view(operation):
    return RelocationResolutionOperation.model_validate({
        key: operation.get(key) for key in ("request_id", "reason", "status", "detail")})


def stop_evidence(service, local, observations):
    dispatch = Dispatch.model_validate(local["dispatch"])
    command = dispatch.command
    fleet, claim, saved = (observations.get(key) for key in ("fleet", "claim", "saved"))
    vehicle = next((v for v in fleet.vehicles if v.vehicle_id == dispatch.vehicle_id), None) if fleet else None
    owner = vehicle.simulator_recovery if vehicle else None
    checkpoint = saved.checkpoint if saved else None
    if local["recovery"] and local["recovery"]["status"] in ("REQUESTED", "RESTORING"):
        return None, "A simulator resume is already in progress."
    if not service.repository.ready or not service.repository.enabled:
        return None, "Durable Dispatch storage is required to end a relocation."
    if not (command and fleet and vehicle and claim and saved and saved.configured and owner
            and owner.durable and checkpoint and checkpoint.simulation):
        return None, "A matching Fleet claim and durable simulator checkpoint are required."
    if (claim.status != "ACTIVE" or vehicle.active_claim != claim or claim.dispatch_id != dispatch.dispatch_id
            or claim.command_id != command.command_id or claim.fleet_instance_id != fleet.instance_id
            or claim.target_zone_id != command.target_zone_id or claim.target_position != command.target_position
            or saved.fleet_instance_id != fleet.instance_id or saved.control_version != vehicle.control_version
            or saved.generation != owner.generation or saved.checkpoint_revision != owner.checkpoint_revision
            or vehicle.active_rental or vehicle.active_charging_task):
        return None, "Fleet ownership changed or could not be confirmed. Reconcile the relocation."
    if dispatch.state == "SUCCEEDED" or local["release_outcome"] == "ARRIVED":
        return None, "Arrival is already recorded. Reconcile to confirm release."
    physical = checkpoint.physical
    if not (checkpoint.command == command or unstarted_checkpoint(local, vehicle, checkpoint)):
        return None, "The saved checkpoint does not match this relocation."
    if (physical.relocation_state == "ARRIVED" or physical.charging_state == "CHARGING"
            or (physical.distance_travelled_km or 0) < dispatch.distance_travelled_km
            or vehicle.telemetry and vehicle.telemetry.active_dispatch_id == dispatch.dispatch_id
            and ((vehicle.telemetry.distance_travelled_km or 0) > (physical.distance_travelled_km or 0)
                 or vehicle.telemetry.relocation_state == "ARRIVED")):
        return None, "The checkpoint cannot account for the recorded progress or arrival."
    if not (physical.relocation_state == "FAILED" or checkpoint.accepted is False
            or owner.expires_at <= service.clock.now() and vehicle.stale):
        return None, "The simulator still owns a live lease and may be moving."
    return {
        "dispatch_id": dispatch.dispatch_id, "command_id": command.command_id,
        "expected_fleet_instance_id": saved.fleet_instance_id,
        "expected_control_version": saved.control_version, "expected_generation": saved.generation,
        "checkpoint_evidence": stop_checkpoint_evidence(checkpoint),
    }, None


async def assess_resolution(service, dispatch_id, *, details=False):
    _, local, observations, _ = await assess_recovery(service, dispatch_id, details=True)
    operation = local["resolution"]
    active = operation and operation["status"] in ("REQUESTED", "CLEANUP_PENDING", "ENDED")
    evidence, blocker = (None, None) if active else stop_evidence(service, local, observations)
    revision = (hashlib.sha256(SimulatorStopRequest(request_id=UUID(int=0), **evidence)
        .model_dump_json().encode()).hexdigest() if evidence else None)
    physical = observations["saved"].checkpoint.physical if evidence else None
    result = RelocationResolutionAssessment(assessment_revision=revision, can_end=bool(evidence),
        stop_position=physical.position if physical else None, stop_soc_pct=physical.soc_pct if physical else None,
        blocker=blocker, operation=operation_view(operation) if operation else None)
    return (result, local, evidence) if details else result


def replay_end(operation, body):
    if not operation:
        return None
    request_id = str(body.request_id)
    saved = operation if operation["request_id"] == request_id else operation.get("past_requests", {}).get(request_id)
    if not saved:
        return None
    if saved["request"] != body.model_dump(mode="json"):
        raise HTTPException(409, "RESOLUTION_REQUEST_ID_CONFLICT")
    return operation_view(saved)


async def request_end(service, dispatch_id, body):
    service.recovery.ensure_ready()
    record = service.store.get(dispatch_id)
    async with record.lock:
        replay = replay_end(record.resolution, body)
        if replay:
            return replay
    assessment, local, evidence = await assess_resolution(service, dispatch_id, details=True)
    async with record.lock:
        replay = replay_end(record.resolution, body)
        if replay:
            return replay
        if not assessment.can_end or not evidence:
            raise HTTPException(409, "RELOCATION_STOP_UNAVAILABLE")
        if (assessment.assessment_revision != body.expected_assessment_revision
                or not same_local(local_snapshot(record), local)):
            raise HTTPException(409, "STOP_ASSESSMENT_CHANGED")
        intent = SimulatorStopRequest(request_id=body.request_id, **evidence)
        previous = record.resolution
        history = dict(previous.get("past_requests", {})) if previous else {}
        if previous:
            history[previous["request_id"]] = {**operation_view(previous).model_dump(mode="json"),
                                                "request": previous["request"]}
        record.resolution = {"request_id": str(body.request_id), "request": body.model_dump(mode="json"),
            "intent": intent.model_dump(mode="json"), "reason": body.reason, "status": "REQUESTED", "detail": None,
            "past_requests": history}
        await service.persist()
        return operation_view(record.resolution)


async def stop_intent(service, dispatch_id):
    record = service.store.get(dispatch_id)
    async with record.lock:
        service.recovery.ensure_ready()
        if not stop_pending(record):
            raise HTTPException(409, "STOP_NOT_AUTHORIZED")
        return record.resolution["intent"]


async def reconcile_hold(service, record):
    facility_id = record.data["parking_facility_id"]
    if not facility_id or record.data["claim_held"] is not False:
        return
    if record.data["parking_status"] is None:
        hold = await request_json(service.client, "GET",
            f"{service.settings.facilities_url}/api/v1/holds/{record.data['dispatch_id']}", model=ParkingHold)
        if (str(hold.dispatch_id) != record.data["dispatch_id"] or hold.facility_id != facility_id
                or hold.vehicle_id != record.data["vehicle_id"]
                or hold.status == "OCCUPIED" and record.data["state"] != "SUCCEEDED"):
            raise HTTPException(409, "PARKING_HOLD_IDENTITY_MISMATCH")
        record.data["parking_status"] = hold.status
        service.planning_revision += 1
    await service.sync_parking(record)


async def reconcile_relocation(service, record):
    if stop_pending(record):
        return
    if (record.data["state"] in ("FAILED", "REJECTED") and record.data["command"]
            and record.data["claim_held"] is not False):
        receipt = await request_json(service.client, "GET", service.claim_url(record), model=VehicleClaim)
        service.check_receipt(record, receipt)
        if receipt.status == "RELEASED":
            service.finish(record, receipt.outcome)
    else:
        await service.refresh(record)
    if record.data["state"] in ("FAILED", "REJECTED", "SUCCEEDED"):
        await reconcile_hold(service, record)


async def advance_resolution(service, record):
    service.recovery.ensure_ready()
    async with record.lock:
        operation = deepcopy(record.resolution)
    if operation["status"] == "REQUESTED":
        try:
            receipt = await request_json(service.client, "POST",
                f"{service.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}/simulator-stop",
                operation["intent"], VehicleClaim, timeout_seconds=8)
            service.check_receipt(record, receipt)
            if receipt.status != "RELEASED" or receipt.outcome != "REJECTED":
                raise HTTPException(503, "STOP_RELEASE_UNCONFIRMED")
        except HTTPException as exc:
            async with record.lock:
                if stop_pending(record) and record.resolution["request_id"] == operation["request_id"]:
                    record.resolution["detail"] = str(exc.detail)
                    if exc.status_code in (409, 422):
                        record.resolution["status"] = "BLOCKED"
                    await service.persist()
            return
        async with record.lock:
            if not stop_pending(record) or record.resolution["request_id"] != operation["request_id"]:
                return
            service.finish(record, receipt.outcome)
            service.transition(record, "REJECTED", "OPERATOR_ENDED_RELOCATION")
            record.resolution.update(status="CLEANUP_PENDING", detail=None)
            await service.persist()
    async with record.lock:
        if record.resolution["status"] != "CLEANUP_PENDING":
            return
        try:
            await reconcile_hold(service, record)
            cleaned = not record.data["parking_facility_id"] or record.data["parking_status"] == "RELEASED"
            record.resolution.update(status="ENDED" if cleaned else "CLEANUP_PENDING",
                                     detail=None if cleaned else "PARKING_RELEASE_UNCONFIRMED")
        except HTTPException as exc:
            record.resolution["detail"] = str(exc.detail)
        await service.persist()
