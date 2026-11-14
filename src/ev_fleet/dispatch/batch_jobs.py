"""Human-authorized, durable and bounded batch admission.

The recommendation inside approval_intent is immutable. Retry metadata lives on
that same durable dispatch row; batch metadata contains small references only.
"""
from copy import deepcopy
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from fastapi import HTTPException

from ev_fleet.contracts.dispatch import DispatchApproval, VehicleClaim, queued_batch_approval_id
from ev_fleet.contracts.facilities import ParkingHold
from ev_fleet.contracts.recommendations import Recommendation
from ev_fleet.contracts.vehicles import DashboardFleetSnapshot, FleetSnapshot
from .approval_evidence import record_approval
from .clients import request_json


TERMINAL = {"SUCCEEDED", "REJECTED", "FAILED"}
RETRY_REASONS = {
    "DEPENDENCY_TIMEOUT", "DEPENDENCY_UNAVAILABLE", "CRDS_PLANNING_CHANGED",
    "ADMISSION_BUSY_RETRY_SAME_REQUEST", "APPROVAL_RESOURCE_UNRESOLVED",
    "SIMULATOR_RUNTIME_TEMPORARILY_UNAVAILABLE", "SIMULATOR_CHECKPOINT_BACKLOG",
    "ONEMAP_TIMEOUT", "ONEMAP_UNAVAILABLE", "ONEMAP_RATE_LIMITED", "CRDS_INPUT_TIMEOUT",
}


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def authorization_expired(service, record):
    return bool(record.approval_intent and
                timestamp(record.approval_intent["expires_at"]) <= service.clock.now())


def reserves_slot(record):
    data = record.data
    if data["claim_held"] is not False or data.get("parking_status") == "RESERVED":
        return True
    if record.claim_request and data["state"] not in TERMINAL:
        return True
    return bool(record.claimed and data["state"] not in TERMINAL)


def active_count(service):
    return sum(reserves_slot(record) for record in service.store.records.values())


def approved_recommendation(service, record):
    if authorization_expired(service, record):
        raise HTTPException(409, "BATCH_AUTHORIZATION_EXPIRED")
    intent = record.approval_intent
    return Recommendation.model_validate({**intent["recommendation"], "expires_at": intent["expires_at"]})


def check_runtime(vehicle, now):
    owner = vehicle.simulator_recovery
    if owner is not None and (owner.status != "ACTIVE" or owner.expires_at <= now):
        raise HTTPException(409, "SIMULATOR_LEASE_EXPIRED")
    runtime = getattr(vehicle, "simulator_runtime", None)
    if runtime is not None and runtime.mode == "RECOVERY_WAIT":
        raise HTTPException(409, "SIMULATOR_RECOVERY_REQUIRED")
    if runtime is not None and (not runtime.live or not runtime.ready or runtime.mode != "RUNNING"):
        raise HTTPException(503, "SIMULATOR_RUNTIME_TEMPORARILY_UNAVAILABLE")


async def enqueue_batch(service, body):
    key = str(body.request_id)
    async with service.batch_lock:
        existing = service.batches.get(key)
        if existing is not None:
            if existing["batch_recommendation_id"] != str(body.batch_recommendation_id):
                raise HTTPException(409, "BATCH_REQUEST_ID_CONFLICT")
            if not getattr(service.repository, "ready", True):
                raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN")
            # A prior POST may have been cancelled before obtaining its commit
            # barrier. Never acknowledge only the surviving in-memory intent.
            await service.persist_if_changed()
            return await service.get_batch(key)
        refused = service.batch_approval_refusals.get(key)
        if refused is not None:
            if refused["batch_recommendation_id"] != str(body.batch_recommendation_id):
                raise HTTPException(409, "BATCH_REQUEST_ID_CONFLICT")
            await service.persist_if_changed()
            raise HTTPException(refused["status_code"], refused["reason_code"])
        if str(body.batch_recommendation_id) in service.batch_recommendations:
            raise HTTPException(409, "BATCH_ALREADY_APPROVED")
        batch = await service.load_batch_preview(body.batch_recommendation_id,
            queued=body.request_id == queued_batch_approval_id(body.batch_recommendation_id))
        # Read all frozen recommendations before publishing any queue records.
        recommendations = []
        vehicles, recommendation_ids = set(), set()
        for assignment in batch.assignments:
            recommendation = await request_json(service.client, "GET",
                f"{service.settings.rebalancing_url}/api/v1/recommendations/{assignment.recommendation_id}",
                model=Recommendation)
            if (recommendation.recommendation_id != assignment.recommendation_id
                    or recommendation.selected_vehicle_id != assignment.vehicle_id
                    or recommendation.parking_facility_id != assignment.facility_id
                    or recommendation.target.zone_id != batch.target_zone_id
                    or recommendation.target_position != assignment.target_position
                    or recommendation.purpose != batch.purpose
                    or recommendation.requires_charging != assignment.requires_charging
                    or assignment.vehicle_id in vehicles
                    or str(assignment.recommendation_id) in recommendation_ids):
                raise HTTPException(503, "BATCH_ASSIGNMENT_IDENTITY_MISMATCH")
            if recommendation.expires_at <= service.clock.now():
                raise HTTPException(409, "RECOMMENDATION_EXPIRED")
            if str(assignment.recommendation_id) in service.store.recommendations:
                raise HTTPException(409, "RECOMMENDATION_ALREADY_APPROVED")
            vehicles.add(assignment.vehicle_id)
            recommendation_ids.add(str(assignment.recommendation_id))
            recommendations.append(recommendation)
        now = service.clock.now()
        if batch.expires_at <= now or any(rec.expires_at <= now for rec in recommendations):
            raise HTTPException(409, "BATCH_EXPIRED")
        expires_at = (now + timedelta(seconds=service.settings.batch_authorization_seconds)).isoformat()
        async with service.admission_lock:
            # Serialise against direct approvals of the same recommendations.
            if any(str(rec.recommendation_id) in service.store.recommendations for rec in recommendations):
                raise HTTPException(409, "RECOMMENDATION_ALREADY_APPROVED")
            now = service.clock.now()
            if batch.expires_at <= now or any(rec.expires_at <= now for rec in recommendations):
                raise HTTPException(409, "BATCH_EXPIRED")
            expires_at = (now + timedelta(seconds=service.settings.batch_authorization_seconds)).isoformat()
            if len(service.store.records) + len(recommendations) > 10000:
                raise HTTPException(503, "DISPATCH_CAPACITY_REACHED")
            items = []
            for assignment, recommendation in zip(batch.assignments, recommendations):
                record = await service.store.begin(DispatchApproval(
                    request_id=uuid5(body.request_id, str(assignment.recommendation_id)),
                    recommendation_id=assignment.recommendation_id))
                record.approval_intent = {
                    "batch_request_id": key, "expires_at": expires_at,
                    "recommendation": recommendation.model_dump(mode="json"),
                    "queue_status": "QUEUED", "attempts": 0, "retry_at": None,
                    "last_attempt_at": None,
                }
                record.data.update(vehicle_id=assignment.vehicle_id,
                                   parking_facility_id=assignment.facility_id)
                service.transition(record, "QUEUED")
                items.append({"recommendation_id": str(assignment.recommendation_id),
                    "vehicle_id": assignment.vehicle_id, "facility_id": assignment.facility_id,
                    "dispatch_id": record.data["dispatch_id"], "state": "QUEUED", "error": None})
            data = {"request_id": key, "batch_recommendation_id": str(body.batch_recommendation_id),
                "requested_count": batch.requested_count, "recommended_count": batch.recommended_count,
                "items": items, "accepted_at": now.isoformat(), "expires_at": expires_at}
            service.batches[key] = data
            service.batch_recommendations[str(body.batch_recommendation_id)] = key
            await service.persist()
        return await service.get_batch(key)


def batch_intent(service, request_id, recommendation_id):
    # Revalidation calls back while prepare holds the live record/admission lock.
    # Only the committed snapshot authorizes that call; never publish partial work.
    if not getattr(service.repository, "ready", True):
        raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN")
    snapshot = service.persisted_snapshot or {}
    batch = snapshot.get("batches", {}).get(str(request_id))
    item = next((item for item in batch["items"]
                 if item["recommendation_id"] == str(recommendation_id)), None) if batch else None
    if not item or not batch.get("accepted_at"):
        raise HTTPException(404, "BATCH_ITEM_NOT_FOUND")
    record = snapshot.get("records", {}).get(item.get("dispatch_id"))
    intent = record.get("approval_intent") if record else None
    child_request = str(uuid5(UUID(str(request_id)), str(recommendation_id)))
    if (not intent or intent["batch_request_id"] != str(request_id)
            or record["data"]["request_id"] != child_request
            or record["data"]["dispatch_id"] != item["dispatch_id"]
            or record["data"]["recommendation_id"] != str(recommendation_id)
            or intent["recommendation"]["recommendation_id"] != str(recommendation_id)
            or intent["queue_status"] not in {"QUEUED", "WAITING"}
            or record["data"]["state"] in TERMINAL
            or intent["expires_at"] != batch["expires_at"]):
        raise HTTPException(409, "BATCH_INTENT_UNAVAILABLE")
    if timestamp(intent["expires_at"]) <= service.clock.now():
        raise HTTPException(409, "BATCH_AUTHORIZATION_EXPIRED")
    return {"batch_request_id": str(request_id), "dispatch_id": item["dispatch_id"],
            "recommendation_id": str(recommendation_id), "expires_at": intent["expires_at"],
            "recommendation": {**deepcopy(intent["recommendation"]), "expires_at": intent["expires_at"]}}


def job_view(service, result, records=None):
    records = service.store.records if records is None else records
    admitted = completed = queued = 0
    unfinished = False
    for item in result["items"]:
        child = records.get(item.get("dispatch_id"))
        if child is None or child.approval_intent is None:
            continue
        intent = child.approval_intent
        status = intent["queue_status"]
        if child.claimed:
            status = "ADMITTED"
        elif child.data["state"] in TERMINAL and status != "CANCELLED":
            status = "REJECTED"
        item.update(queue_status=status, attempts=intent["attempts"], retry_at=intent["retry_at"])
        admitted += bool(child.claimed)
        completed += child.data["state"] == "SUCCEEDED"
        queued += status in {"QUEUED", "WAITING"}
        unfinished = unfinished or status in {"QUEUED", "WAITING"} or reserves_slot(child)
    expired = timestamp(result["expires_at"]) <= service.clock.now()
    attempted = any(item.get("attempts", 0) for item in result["items"])
    expired_items = any(item.get("error") == "BATCH_AUTHORIZATION_EXPIRED" for item in result["items"])
    job_status = ("COMPLETED" if not unfinished and not expired_items
                  else "EXPIRED" if expired else "RUNNING" if attempted else "QUEUED")
    status = ("QUEUED" if job_status == "QUEUED" else "PROCESSING" if unfinished
              else result["status"])
    return {**result, "status": status, "job_status": job_status,
            "admitted_count": admitted, "completed_count": completed,
            "queued_count": queued, "in_flight_limit": service.settings.dispatch_in_flight_limit}


async def reconcile_expired(service, record):
    """Resolve existing IDs after expiry, with no new claim/hold acquisition."""
    fleet = await request_json(service.client, "GET",
        f"{service.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot)
    if str(fleet.instance_id) != record.claim_request["expected_instance_id"]:
        raise HTTPException(503, "FLEET_INSTANCE_CHANGED")
    if record.parking_request:
        try:
            hold = await request_json(service.client, "GET",
                f"{service.settings.facilities_url}/api/v1/holds/{record.data['dispatch_id']}", model=ParkingHold)
            if (str(hold.dispatch_id) != record.data["dispatch_id"]
                    or hold.vehicle_id != record.data["vehicle_id"]
                    or hold.facility_id != record.data["parking_facility_id"]):
                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
            record.data["parking_status"] = hold.status
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            record.data["parking_status"] = None
    try:
        receipt = await request_json(service.client, "GET", service.claim_url(record), model=VehicleClaim)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
        record.data["claim_held"] = False
        record.approval_intent.update(queue_status="REJECTED", retry_at=None)
        record_approval(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
        service.transition(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
        await service.sync_parking(record)
        return
    if (receipt.status == "ACTIVE" and record.parking_request
            and record.data["parking_status"] != "RESERVED"):
        raise HTTPException(503, "PARKING_HOLD_UNCONFIRMED")
    await service.accept_claim(record, receipt)
    record.approval_intent.update(queue_status="ADMITTED", retry_at=None)


async def runtime_rejection_is_temporary(service, record):
    """Keep known hold/request IDs when a runtime gate races a healthy plan.

    Never infer absence from diagnostics: the exact claim read and Fleet epoch
    must establish it. Physical state or control changes remain terminal.
    """
    fleet = await request_json(service.client, "GET",
        f"{service.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot)
    if str(fleet.instance_id) != record.claim_request["expected_instance_id"]:
        raise HTTPException(503, "FLEET_INSTANCE_CHANGED")
    vehicle = next((item for item in fleet.vehicles if item.vehicle_id == record.data["vehicle_id"]), None)
    if vehicle is None:
        return False
    check_runtime(vehicle, service.clock.now())
    telemetry = vehicle.telemetry
    if (vehicle.control_version != record.claim_request["expected_control_version"]
            or vehicle.active_claim or vehicle.active_rental or vehicle.active_charging_task
            or vehicle.stale or vehicle.connectivity != "ONLINE" or telemetry is None
            or telemetry.availability != "AVAILABLE" or telemetry.relocation_state != "IDLE"
            or telemetry.active_dispatch_id or telemetry.charging_state != "NOT_CHARGING"):
        return False
    try:
        await request_json(service.client, "GET", service.claim_url(record), model=VehicleClaim)
    except HTTPException as exc:
        if exc.status_code != 404:
            raise
    else:
        return False
    diagnostics = await request_json(service.client, "GET",
        f"{service.settings.fleet_url}/api/v1/dashboard/vehicles", model=DashboardFleetSnapshot)
    observed = next((item for item in diagnostics.vehicles if item.vehicle_id == vehicle.vehicle_id), None)
    if diagnostics.instance_id != fleet.instance_id or observed is None:
        raise HTTPException(503, "DEPENDENCY_IDENTITY_MISMATCH")
    try:
        check_runtime(observed, service.clock.now())
    except HTTPException as exc:
        if exc.detail == "SIMULATOR_RUNTIME_TEMPORARILY_UNAVAILABLE":
            return True
        raise
    # A healthy cached diagnostic is advisory only. Fleet's authoritative
    # unavailability with unchanged idle physical state still vetoes the claim;
    # retaining the exact request safely waits for the next observation.
    return vehicle.effective_availability != "AVAILABLE"


async def queue_once(service):
    # A single worker controls all queue admissions, including exact-ID retries.
    if service.queue_lock.locked():
        return
    async with service.queue_lock:
        if not getattr(service.repository, "ready", True):
            raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN")
        changed = False
        for record in list(service.store.records.values()):
            intent = record.approval_intent
            if (intent and not record.claimed and not record.claim_request
                    and intent["queue_status"] in {"QUEUED", "WAITING"}
                    and authorization_expired(service, record)):
                async with record.lock:
                    if (record.claimed or record.claim_request
                            or intent["queue_status"] not in {"QUEUED", "WAITING"}
                            or record.data["state"] in TERMINAL):
                        continue
                    intent.update(queue_status="REJECTED", retry_at=None)
                    record_approval(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
                    service.transition(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
                    changed = True
        if changed:
            await service.persist()
        # Persisted wall times prevent restart from collapsing the pacing gap.
        previous = [timestamp(record.approval_intent["last_attempt_at"])
                    for record in service.store.records.values()
                    if record.approval_intent and record.approval_intent.get("last_attempt_at")]
        if previous and (service.clock.now() - max(previous)).total_seconds() < service.settings.dispatch_admission_interval_seconds:
            return
        for record in list(service.store.records.values()):
            intent = record.approval_intent
            if (not intent or record.claimed or record.data["state"] in TERMINAL
                    or intent["queue_status"] not in {"QUEUED", "WAITING"}
                    or record.lock.locked()
                    or intent["retry_at"] and timestamp(intent["retry_at"]) > service.clock.now()):
                continue
            async with service.admission_lock:
                async with record.lock:
                    # Eligibility can change while waiting for either lock.
                    # In particular an operator may have cancelled this row.
                    intent = record.approval_intent
                    if (not intent or record.claimed or record.data["state"] in TERMINAL
                            or intent["queue_status"] not in {"QUEUED", "WAITING"}
                            or intent["retry_at"] and timestamp(intent["retry_at"]) > service.clock.now()):
                        continue
                    if authorization_expired(service, record) and not record.claim_request:
                        intent.update(queue_status="REJECTED", retry_at=None)
                        record_approval(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
                        service.transition(record, "REJECTED", "BATCH_AUTHORIZATION_EXPIRED")
                        await service.persist()
                        continue
                    if not reserves_slot(record) and active_count(service) >= service.settings.dispatch_in_flight_limit:
                        continue
                    intent["attempts"] += 1
                    intent.update(last_attempt_at=service.clock.now().isoformat(), retry_at=None,
                                  queue_status="WAITING")
                    await service.persist()  # Attempt metadata precedes any remote effect.
                    try:
                        if not record.claim_request:
                            # Partial preparation contains no remote mutation and may be replanned.
                            record.parking_request = None
                            await service.prepare(record)
                            service.planning_revision += 1
                        await service.acquire(record)
                        if record.claimed:
                            intent.update(queue_status="ADMITTED", retry_at=None)
                    except HTTPException as exc:
                        reason = str(exc.detail)
                        runtime_wait = False
                        if (record.claim_request and exc.status_code == 409
                                and reason == "VEHICLE_NOT_ELIGIBLE"):
                            try:
                                runtime_wait = await runtime_rejection_is_temporary(service, record)
                            except HTTPException as classified:
                                exc = classified
                                reason = str(classified.detail)
                            if runtime_wait:
                                reason = "SIMULATOR_RUNTIME_TEMPORARILY_UNAVAILABLE"
                        # Never replace a prepared exact request, even after a lost response.
                        uncertain = bool(record.claim_request and (exc.status_code >= 500 or reason in {
                            "FLEET_INSTANCE_CHANGED", "CLAIM_ID_CONFLICT", "PARKING_HOLD_ID_CONFLICT"}))
                        transient = runtime_wait or reason in RETRY_REASONS or (not record.claim_request and
                            exc.status_code >= 500 and reason.startswith("DEPENDENCY_"))
                        if uncertain or transient:
                            delay = min(30, 2 ** min(intent["attempts"], 5))
                            intent.update(queue_status="WAITING", retry_at=(service.clock.now() + timedelta(seconds=delay)).isoformat())
                            record.data["claim_held"] = None if record.claim_request else False
                            record_approval(record, "UNKNOWN", reason)
                            service.transition(record, "UNKNOWN" if record.claim_request else "QUEUED", reason)
                        else:
                            await service.handle_approval_error(record, exc)
                            intent.update(queue_status="REJECTED", retry_at=None)
                    finally:
                        await service.persist()
                    return
