import asyncio
import logging
from uuid import uuid4, uuid5
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.settings import DispatchSettings, Settings
from ev_fleet.contracts.dispatch import ClaimRequest, RelocationCommand, VehicleClaim
from ev_fleet.contracts.vehicles import FleetSnapshot, VehicleState
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.recommendations import Recommendation, BatchRecommendation
from ev_fleet.contracts.facilities import Facility, ParkingHold, ParkingHoldRequest
from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.rebalancing.policy import assess
from .clients import request_json
from .store import DispatchRecord, DispatchStore


TERMINAL = {"SUCCEEDED", "REJECTED", "FAILED"}


class DispatchService:
    def __init__(self, client, settings=None, clock=None, repository=None):
        self.client = client
        self.settings = settings or DispatchSettings.from_env()
        self.clock = clock or Clock()
        self.store = DispatchStore(self.clock)
        self.batch_lock = asyncio.Lock()
        self.persist_lock = asyncio.Lock()
        self.batches = {}
        self.batch_recommendations = {}
        self.repository = repository or SnapshotRepository.from_env("dispatch")

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if not payload:
            await self.persist()
            return
        self.store.requests = payload["requests"]
        self.store.recommendations = payload["recommendations"]
        self.batches = payload["batches"]
        self.batch_recommendations = payload["batch_recommendations"]
        tick = self.clock.monotonic()
        for key, item in payload["records"].items():
            record = DispatchRecord(
                data=item["data"], claim_request=item["claim_request"],
                parking_request=item["parking_request"], claimed=item["claimed"],
                ack=item["ack"], events=item["events"],
                event_sequence=item["event_sequence"],
                release_outcome=item["release_outcome"])
            if record.data["delivery_attempts"]:
                record.sent_tick = tick
            if record.claimed and record.data["state"] not in TERMINAL:
                record.claim_tick = tick
            if record.data["acknowledged_at"] and record.data["state"] not in TERMINAL:
                record.progress_tick = tick
            self.store.records[key] = record

    async def close(self):
        await self.persist()
        await self.repository.close()

    async def persist(self):
        async with self.persist_lock:
            records = {key: {
                "data": record.data,
                "claim_request": record.claim_request,
                "parking_request": record.parking_request,
                "claimed": record.claimed,
                "ack": record.ack,
                "events": record.events,
                "event_sequence": record.event_sequence,
                "release_outcome": record.release_outcome,
            } for key, record in self.store.records.items()}
            await self.repository.save({
                "records": records,
                "requests": self.store.requests,
                "recommendations": self.store.recommendations,
                "batches": self.batches,
                "batch_recommendations": self.batch_recommendations,
            })

    def transition(self, record, state, reason=None):
        record.data.update(state=state, failure_reason=reason, updated_at=self.clock.now().isoformat())

    def claim_url(self, record):
        return f"{self.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}/claims/{record.data['dispatch_id']}"

    async def prepare(self, record):
        recommendation = await request_json(self.client, "GET",
            f"{self.settings.rebalancing_url}/api/v1/recommendations/{record.data['recommendation_id']}", model=Recommendation)
        if str(recommendation.recommendation_id) != record.data["recommendation_id"]:
            raise HTTPException(503, "DEPENDENCY_IDENTITY_MISMATCH")
        if recommendation.expires_at <= self.clock.now():
            raise HTTPException(409, "RECOMMENDATION_EXPIRED")
        if not recommendation.selected_vehicle_id:
            raise HTTPException(409, "NO_RECOMMENDED_VEHICLE")
        target, fleet = await asyncio.gather(
            request_json(self.client, "GET", f"{self.settings.demand_url}/api/v1/zones/{recommendation.target.zone_id}", model=ZoneState),
            request_json(self.client, "GET", f"{self.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot))
        if target != recommendation.target:
            raise HTTPException(409, "TARGET_CHANGED")
        if fleet.instance_id != recommendation.fleet_snapshot.instance_id:
            raise HTTPException(409, "FLEET_INSTANCE_CHANGED")
        vehicle = next((v for v in fleet.vehicles if v.vehicle_id == recommendation.selected_vehicle_id), None)
        if vehicle is None:
            raise HTTPException(409, "VEHICLE_NOT_FOUND")
        policy = Settings(**recommendation.policy_settings)
        parking = None
        destination = target.position
        assessed_target = target.model_dump(mode="json")
        if recommendation.parking_facility_id:
            parking = await request_json(self.client, "GET",
                f"{self.settings.facilities_url}/api/v1/facilities/{recommendation.parking_facility_id}",
                model=Facility)
            if (parking.zone_id != target.zone_id or parking.status != "OPEN"
                    or parking.parking_available <= 0 or parking.source != "MOCK"
                    or parking.position != recommendation.target_position):
                raise HTTPException(409, "PARKING_DESTINATION_CHANGED")
            destination = parking.position
            assessed_target["position"] = parking.position.model_dump(mode="json")
            assessed_target["charging"] = {
                "observed_at": self.clock.now().isoformat(),
                "available_slots": parking.charging_available}
        assessment = assess(vehicle.model_dump(mode="json"), assessed_target, policy, self.clock.now())
        if not assessment["eligible"]:
            raise HTTPException(409, "VEHICLE_NOT_ELIGIBLE:" + ",".join(assessment["exclusion_reasons"]))
        if parking:
            requires_charge = (assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
                               or recommendation.requires_charging)
            if requires_charge and parking.charging_available <= 0:
                raise HTTPException(409, "CHARGING_FULL")
            record.data["parking_facility_id"] = parking.facility_id
            record.parking_request = ParkingHoldRequest(
                dispatch_id=record.data["dispatch_id"], vehicle_id=vehicle.vehicle_id,
                expected_zone_id=target.zone_id, expected_position=parking.position,
                requires_charging=requires_charge).model_dump(mode="json")
        command_id = uuid4()
        claim = ClaimRequest(command_id=command_id, expected_instance_id=fleet.instance_id,
            expected_version=vehicle.state_version, target_zone_id=target.zone_id,
            target_position=destination, approve_before=recommendation.expires_at,
            min_reserve_pct=max(policy.min_reserve_pct, self.settings.min_reserve_pct),
            route_factor=max(policy.route_factor, self.settings.route_factor),
            energy_margin_factor=max(policy.energy_margin_factor, self.settings.energy_margin_factor))
        command = RelocationCommand(dispatch_id=record.data["dispatch_id"], command_id=command_id,
            vehicle_id=vehicle.vehicle_id, fleet_instance_id=fleet.instance_id,
            target_zone_id=target.zone_id, target_position=destination, issued_at=self.clock.now(),
            min_reserve_pct=claim.min_reserve_pct, route_factor=claim.route_factor,
            energy_margin_factor=claim.energy_margin_factor)
        record.data.update(vehicle_id=vehicle.vehicle_id, command=command.model_dump(mode="json"),
                           revalidated_metrics=assessment)
        # Store the exact request BEFORE the remote mutation. Never silently refresh its version.
        record.claim_request = claim.model_dump(mode="json")

    async def acquire(self, record):
        if record.parking_request:
            hold = await request_json(self.client, "POST",
                f"{self.settings.facilities_url}/api/v1/facilities/{record.data['parking_facility_id']}/holds",
                record.parking_request, model=ParkingHold)
            if (str(hold.dispatch_id) != record.data["dispatch_id"] or
                    hold.vehicle_id != record.data["vehicle_id"] or
                    hold.facility_id != record.data["parking_facility_id"] or hold.status != "RESERVED"):
                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
            record.data["parking_status"] = hold.status
        receipt = await request_json(self.client, "PUT", self.claim_url(record), record.claim_request, VehicleClaim)
        self.check_receipt(record, receipt)
        record.claimed = True
        if record.claim_tick is None:
            elapsed = max(0, (self.clock.now() - receipt.acquired_at).total_seconds())
            record.claim_tick = self.clock.monotonic() - elapsed
        record.data["claim_held"] = receipt.status == "ACTIVE"
        if receipt.status == "RELEASED":
            self.finish(record, receipt.outcome)
            await self.sync_parking(record)
        else:
            self.transition(record, "PENDING")

    def check_receipt(self, record, receipt):
        command = record.data["command"]
        if (str(receipt.dispatch_id) != record.data["dispatch_id"] or
            str(receipt.command_id) != command["command_id"] or
            str(receipt.fleet_instance_id) != command["fleet_instance_id"] or
            receipt.vehicle_id != record.data["vehicle_id"]):
            raise HTTPException(503, "CLAIM_IDENTITY_MISMATCH")

    def finish(self, record, outcome):
        record.data["claim_held"] = False
        record.data["completed_at"] = self.clock.now().isoformat()
        self.transition(record, "SUCCEEDED" if outcome == "ARRIVED" else "REJECTED",
                        None if outcome == "ARRIVED" else "EV_REJECTED_COMMAND")

    async def sync_parking(self, record):
        """Complete on verified arrival; release only after a known released claim."""
        if record.data["parking_status"] != "RESERVED" or record.data["claim_held"] is not False:
            return
        state = record.data["state"]
        if state not in TERMINAL:
            return
        action = "complete" if state == "SUCCEEDED" else "release"
        try:
            receipt = await request_json(self.client, "POST",
                f"{self.settings.facilities_url}/api/v1/holds/{record.data['dispatch_id']}/{action}",
                model=ParkingHold)
            wanted = "OCCUPIED" if action == "complete" else "RELEASED"
            if (str(receipt.dispatch_id) != record.data["dispatch_id"] or
                    receipt.facility_id != record.data["parking_facility_id"] or
                    receipt.status != wanted):
                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
            record.data["parking_status"] = receipt.status
        except HTTPException as exc:
            logging.warning("parking finalisation pending dispatch=%s error=%s",
                            record.data["dispatch_id"], exc.detail)

    async def approve(self, body):
        record = await self.store.begin(body)
        try:
            async with record.lock:
                if record.claimed or record.data["state"] in TERMINAL:
                    return record.view()
                try:
                    if not record.claim_request:
                        await self.prepare(record)
                    await self.acquire(record)
                except HTTPException as exc:
                    # Only definite claim failures may free a parking reservation.
                    # Lost responses keep the reservation until reconciliation.
                    if (exc.status_code in (404, 409, 422) and not record.claimed
                            and record.data["parking_status"] == "RESERVED"):
                        try:
                            released = await request_json(self.client, "POST",
                                f"{self.settings.facilities_url}/api/v1/holds/{record.data['dispatch_id']}/release",
                                model=ParkingHold)
                            if released.status != "RELEASED" or str(released.dispatch_id) != record.data["dispatch_id"]:
                                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
                            record.data["parking_status"] = "RELEASED"
                        except HTTPException:
                            self.transition(record, "UNKNOWN", "PARKING_RELEASE_UNCONFIRMED")
                            return record.view()
                    self.handle_approval_error(record, exc)
                    if exc.status_code in (404, 409, 422):
                        raise HTTPException(409, exc.detail) from exc
                return record.view()
        finally:
            await self.persist()

    def handle_approval_error(self, record, exc):
        # A Fleet epoch change after an attempted claim cannot prove the old claim did not execute.
        uncertain = exc.status_code >= 500 or (record.claim_request and exc.detail == "FLEET_INSTANCE_CHANGED")
        self.transition(record, "UNKNOWN" if uncertain else "REJECTED", str(exc.detail))
        if uncertain and record.claim_request:
            record.data["claim_held"] = None  # Unknown, not proof that the EV is free.

    async def refresh(self, record):
        """Caller serializes this dispatch; Fleet owns the vehicle's atomic state."""
        if record.data["state"] in TERMINAL:
            await self.sync_parking(record)
            return
        if not record.claimed:
            try:
                if not record.claim_request:
                    await self.prepare(record)
                await self.acquire(record)
            except HTTPException as exc:
                self.handle_approval_error(record, exc)
            return
        try:
            receipt = await request_json(self.client, "GET", self.claim_url(record), model=VehicleClaim)
            self.check_receipt(record, receipt)
            if receipt.status == "RELEASED":
                self.finish(record, receipt.outcome)
                await self.sync_parking(record)
                return
            record.data["claim_held"] = True
            vehicle = await request_json(self.client, "GET",
                f"{self.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}", model=VehicleState)
            if not vehicle.active_claim or vehicle.active_claim != receipt:
                raise HTTPException(409, "CLAIM_OWNERSHIP_UNCONFIRMED")
            telemetry = vehicle.telemetry
            if vehicle.stale or vehicle.connectivity != "ONLINE":
                self.transition(record, "UNKNOWN", "VEHICLE_OFFLINE_OR_STALE")
                return
            if record.data["state"] == "UNKNOWN" and not record.data["acknowledged_at"] and not record.release_outcome:
                self.transition(record, "SENT" if record.sent_tick is not None else "PENDING")
            if telemetry and str(telemetry.active_dispatch_id) == record.data["dispatch_id"]:
                record.data["last_contact_at"] = telemetry.observed_at.isoformat()
                if telemetry.distance_travelled_km is not None:
                    record.data["distance_travelled_km"] = max(record.data["distance_travelled_km"], telemetry.distance_travelled_km)
                state = telemetry.relocation_state
                if state == "ARRIVED":
                    record.release_outcome = "ARRIVED"
                    record.data["arrival_soc_pct"] = telemetry.soc_pct
                    self.transition(record, "ARRIVAL_PENDING")
                elif state == "FAILED":
                    self.transition(record, "FAILED", "EV_EXECUTION_FAILED")
                    return  # Retain claim; physical failure does not establish availability.
                elif state == "RELOCATING":
                    if record.release_outcome == "REJECTED":
                        self.transition(record, "UNKNOWN", "REJECTION_CONTRADICTS_MOVEMENT")
                        return
                    self.transition(record, "IN_PROGRESS")
                    record.data["acknowledged_at"] = record.data["acknowledged_at"] or self.clock.now().isoformat()
                    # Accepted fresh telemetry is execution evidence even when ACK was lost.
                    record.progress_tick = self.clock.monotonic()
                elif state == "DISPATCHED" and record.ack and record.ack["accepted"]:
                    self.transition(record, "ACKNOWLEDGED")
            if record.release_outcome:
                outcome = record.release_outcome
                receipt = await request_json(self.client, "POST", self.claim_url(record) + "/release",
                    {"command_id": record.data["command"]["command_id"], "outcome": outcome}, VehicleClaim)
                self.check_receipt(record, receipt)
                if receipt.status != "RELEASED" or receipt.outcome != outcome:
                    raise HTTPException(503, "RELEASE_NOT_CONFIRMED")
                self.finish(record, outcome)
                await self.sync_parking(record)
                return
            if record.claim_tick is not None and record.data["acknowledged_at"] is None:
                if self.clock.monotonic() - record.claim_tick >= self.settings.ack_timeout_seconds:
                    self.transition(record, "UNKNOWN", "ACK_TIMEOUT")
            elif record.progress_tick is not None and self.clock.monotonic() - record.progress_tick >= self.settings.progress_timeout_seconds:
                self.transition(record, "UNKNOWN", "PROGRESS_TIMEOUT")
        except HTTPException as exc:
            if record.release_outcome and exc.status_code == 409 and exc.detail in ("COMPLETION_TELEMETRY_PENDING", "COMPLETION_NOT_CONFIRMED"):
                self.transition(record, "ARRIVAL_PENDING" if record.release_outcome == "ARRIVED" else "UNKNOWN", str(exc.detail))
            else:
                self.transition(record, "UNKNOWN", str(exc.detail))

    async def get(self, dispatch_id):
        record = self.store.get(dispatch_id)
        async with record.lock:
            await self.refresh(record)
            result = record.view()
        await self.persist()
        return result

    async def commands(self, vehicle_id):
        try:
            for record in list(self.store.records.values()):
                if record.data["vehicle_id"] != vehicle_id:
                    continue
                async with record.lock:
                    await self.refresh(record)
                    if (not record.claimed or record.data["state"] in TERMINAL or record.release_outcome or
                        record.data["state"] == "UNKNOWN" and record.data["failure_reason"] != "ACK_TIMEOUT"):
                        continue
                    # Only a positively reconciled claim can be delivered. ACK timeout permits same-ID redelivery.
                    if record.sent_tick is None:
                        record.sent_tick = self.clock.monotonic()
                        self.transition(record, "SENT")
                    record.data["delivery_attempts"] += 1
                    return {"commands": [record.view()["command"]]}
            return {"commands": []}
        finally:
            await self.persist()

    def check_command(self, record, body):
        if (not record.claimed or not record.data["delivery_attempts"] or
            record.data["command"]["command_id"] != str(body.command_id) or
            record.data["vehicle_id"] != body.vehicle_id):
            raise HTTPException(409, "COMMAND_ID_CONFLICT")

    async def acknowledge(self, dispatch_id, body):
        record = self.store.get(dispatch_id)
        async with record.lock:
            self.check_command(record, body)
            payload = body.model_dump(mode="json")
            if record.ack:
                if record.ack != payload:
                    raise HTTPException(409, "ACK_CONFLICT")
                return record.view()
            if record.data["state"] in TERMINAL:
                raise HTTPException(409, "DISPATCH_TERMINAL")
            if not body.accepted and record.data["state"] in ("IN_PROGRESS", "ARRIVAL_PENDING"):
                raise HTTPException(409, "COMMAND_ALREADY_STARTED")
            record.ack = payload
            if body.accepted:
                record.data["acknowledged_at"] = self.clock.now().isoformat()
                record.progress_tick = self.clock.monotonic()
                if record.data["state"] not in ("IN_PROGRESS", "ARRIVAL_PENDING"):
                    self.transition(record, "ACKNOWLEDGED")
            else:
                record.release_outcome = "REJECTED"
                await self.refresh(record)
            result = record.view()
        await self.persist()
        return result

    async def event(self, dispatch_id, body):
        record = self.store.get(dispatch_id)
        async with record.lock:
            self.check_command(record, body)
            payload = body.model_dump(mode="json")
            previous = record.events.get(body.sequence)
            if previous == payload:
                return record.view()
            if body.sequence <= record.event_sequence:
                raise HTTPException(409, "EVENT_SEQUENCE_CONFLICT")
            if record.data["state"] in TERMINAL:
                raise HTTPException(409, "DISPATCH_TERMINAL")
            age = (self.clock.now() - body.observed_at).total_seconds()
            if age < -5 or age >= self.settings.stale_seconds:
                raise HTTPException(422, "EVENT_TIME_INVALID")
            if body.distance_travelled_km < record.data["distance_travelled_km"]:
                raise HTTPException(409, "DISTANCE_REGRESSION")
            if record.release_outcome and body.kind == "PROGRESS":
                raise HTTPException(409, "LIFECYCLE_REGRESSION")
            if record.ack and not record.ack["accepted"]:
                raise HTTPException(409, "COMMAND_REJECTED")
            record.events[body.sequence] = payload
            record.event_sequence = body.sequence
            record.data["distance_travelled_km"] = body.distance_travelled_km
            record.data["last_contact_at"] = body.observed_at.isoformat()
            record.progress_tick = self.clock.monotonic()
            if body.kind == "FAILED":
                self.transition(record, "FAILED", body.reason or "EV_EXECUTION_FAILED")
            elif body.kind == "ARRIVED":
                record.release_outcome = "ARRIVED"
                self.transition(record, "ARRIVAL_PENDING")
                await self.refresh(record)
            else:
                record.data["acknowledged_at"] = record.data["acknowledged_at"] or self.clock.now().isoformat()
                self.transition(record, "IN_PROGRESS")
            result = record.view()
        await self.persist()
        return result

    def batch_view(self, data):
        items = list(data["items"])
        approved = sum(item["state"] not in ("REJECTED", "UNKNOWN") for item in items)
        unknown = sum(item["state"] == "UNKNOWN" for item in items)
        status = ("UNKNOWN" if unknown else "APPROVED" if approved == len(items)
                  else "PARTIAL" if approved else "REJECTED")
        return {**data, "status": status}

    async def approve_batch(self, body):
        key = str(body.request_id)
        async with self.batch_lock:
            data = self.batches.get(key)
            if data is not None and data["batch_recommendation_id"] != str(body.batch_recommendation_id):
                raise HTTPException(409, "BATCH_REQUEST_ID_CONFLICT")
            if data is None:
                rec = await request_json(self.client, "GET",
                    f"{self.settings.rebalancing_url}/api/v1/batch-recommendations/{body.batch_recommendation_id}",
                    model=BatchRecommendation)
                if rec.batch_recommendation_id != body.batch_recommendation_id:
                    raise HTTPException(503, "BATCH_IDENTITY_MISMATCH")
                if rec.expires_at <= self.clock.now():
                    raise HTTPException(409, "BATCH_EXPIRED")
                if not rec.assignments:
                    raise HTTPException(409, "NO_BATCH_ASSIGNMENTS")
                existing = self.batch_recommendations.get(str(body.batch_recommendation_id))
                if existing is not None and existing != key:
                    raise HTTPException(409, "BATCH_ALREADY_APPROVED")
                data = {"request_id": key, "batch_recommendation_id": str(body.batch_recommendation_id),
                        "requested_count": rec.requested_count,
                        "recommended_count": rec.recommended_count,
                        "items": [{"recommendation_id": str(a.recommendation_id),
                                   "vehicle_id": a.vehicle_id, "facility_id": a.facility_id,
                                   "dispatch_id": None, "state": "PENDING", "error": None}
                                  for a in rec.assignments]}
                self.batches[key] = data
                self.batch_recommendations[str(body.batch_recommendation_id)] = key
            for item in data["items"]:
                if item["state"] not in ("PENDING", "UNKNOWN"):
                    continue
                # Stable child idempotency key across HTTP retries.
                child_id = uuid5(body.request_id, item["recommendation_id"])
                try:
                    result = await self.approve(DispatchApproval(
                        request_id=child_id, recommendation_id=item["recommendation_id"]))
                    item["dispatch_id"] = result["dispatch_id"]
                    item["state"] = result["state"]
                    item["error"] = result.get("failure_reason")
                except HTTPException as exc:
                    item["state"] = "REJECTED" if exc.status_code in (404, 409, 422) else "UNKNOWN"
                    item["error"] = str(exc.detail)
            result = self.batch_view(data)
        await self.persist()
        return result

    async def get_batch(self, request_id):
        data = self.batches.get(str(request_id))
        if data is None:
            raise HTTPException(404, "BATCH_NOT_FOUND")
        return self.batch_view(data)

    async def reconcile_loop(self):
        while True:
            for record in list(self.store.records.values()):
                if record.lock.locked() or (record.data["state"] in TERMINAL
                    and record.data["parking_status"] != "RESERVED"):
                    continue
                try:
                    async with record.lock:
                        await self.refresh(record)
                    await self.persist()
                except Exception:
                    logging.exception("dispatch reconciliation failed: %s", record.data["dispatch_id"])
            await asyncio.sleep(self.settings.reconcile_interval_seconds)
