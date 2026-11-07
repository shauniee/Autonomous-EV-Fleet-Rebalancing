import asyncio
import logging
import os
from copy import deepcopy
from uuid import UUID, uuid4, uuid5
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.settings import DispatchSettings, Settings, CrdsSettings
from ev_fleet.contracts.crds import CrdsRevalidationRequest, CrdsRevalidationResult
from ev_fleet.contracts.dispatch import ClaimRequest, RelocationCommand, VehicleClaim
from ev_fleet.contracts.vehicles import FleetSnapshot, VehicleState
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.recommendations import Recommendation, BatchRecommendation
from ev_fleet.contracts.facilities import Facility, ParkingHold, ParkingHoldRequest
from ev_fleet.contracts.dispatch import ApprovalDecision, DispatchApproval
from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT, CHARGING_TARGET_SOC_PCT
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.rebalancing.policy import assess
from .approval_evidence import (ApprovalCheckError, approval_evidence, record_approval,
                                revalidation_rejection)
from .clients import request_json
from .store import DispatchRecord, DispatchStore
from .recovery import RecoveryCoordinator
from .resolution import advance_resolution, stop_pending
from .routing import RoutingSettings, driving_route
from .traffic_routes import choose_traffic_route
from ev_fleet.rebalancing.traffic import assess_traffic
from ev_fleet.common.travel import distance_km


TERMINAL = {"SUCCEEDED", "REJECTED", "FAILED"}


class DispatchService:
    def __init__(self, client, settings=None, clock=None, repository=None):
        self.client = client
        self.settings = settings or DispatchSettings.from_env()
        self.clock = clock or Clock()
        self.store = DispatchStore(self.clock)
        self.batch_lock = asyncio.Lock()
        self.persist_lock = asyncio.Lock()
        self.admission_lock = asyncio.Lock()
        self.crds_settings = CrdsSettings.from_env()
        self.batches = {}
        self.batch_recommendations = {}
        self.repository = repository or SnapshotRepository.from_env("dispatch")
        self.persisted_snapshot = None
        self.routing = RoutingSettings.from_env()
        self.planning_instance_id = uuid4()
        self.planning_revision = 0
        self.route_estimates = {}
        self.recovery = RecoveryCoordinator(self)
        self.traffic_enabled = (os.getenv("TRAFFIC_SOURCE", "DISABLED").upper() == "LTA_DATAMALL"
            or os.getenv("TRAFFIC_SIMULATION_ENABLED", "false").lower() == "true")

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
        self.planning_revision = payload.get("planning_revision", 0)
        tick = self.clock.monotonic()
        for key, item in payload["records"].items():
            record = DispatchRecord(
                data=item["data"], claim_request=item["claim_request"],
                parking_request=item["parking_request"], claimed=item["claimed"],
                ack=item["ack"], events=item["events"],
                event_sequence=item["event_sequence"],
                release_outcome=item["release_outcome"], recovery=item.get("recovery"),
                resolution=item.get("resolution"))
            if record.data["delivery_attempts"]:
                record.sent_tick = tick
            if record.claimed and record.data["state"] not in TERMINAL:
                record.claim_tick = tick
            if record.data["acknowledged_at"] and record.data["state"] not in TERMINAL:
                record.progress_tick = tick
            self.store.records[key] = record
        self.persisted_snapshot = deepcopy(payload)

    async def close(self):
        # Do not retry an ambiguous write during shutdown: reconciliation must
        # load the known durable state on the next process start.
        try:
            if getattr(self.repository, "ready", True):
                await self.persist()
        finally:
            await self.repository.close()

    async def persist(self):
        async with self.persist_lock:
            await self.save_snapshot(self.current_snapshot())

    async def persist_if_changed(self):
        async with self.persist_lock:
            if not getattr(self.repository, "ready", True):
                raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
            current = self.current_snapshot()
            if current == self.persisted_snapshot:
                return False
            await self.save_snapshot(current)
            return True

    async def save_snapshot(self, snapshot):
        if not getattr(self.repository, "ready", True):
            raise RuntimeError("PERSISTENCE_STATE_UNCERTAIN")
        frozen = deepcopy(snapshot)
        await self.repository.save(frozen)
        self.persisted_snapshot = frozen

    def current_snapshot(self):
        return {
            "records": {key: self.durable_record(record)
                        for key, record in self.store.records.items()},
            "requests": self.store.requests,
            "recommendations": self.store.recommendations,
            "batches": self.batches,
            "batch_recommendations": self.batch_recommendations,
            "planning_revision": self.planning_revision,
        }

    @staticmethod
    def durable_record(record):
        return {
            "data": record.data,
            "claim_request": record.claim_request,
            "parking_request": record.parking_request,
            "claimed": record.claimed,
            "ack": record.ack,
            "events": record.events,
            "event_sequence": record.event_sequence,
            "release_outcome": record.release_outcome,
            "recovery": record.recovery,
            "resolution": record.resolution,
        }

    def record_checkpoint(self, record):
        return deepcopy(self.durable_record(record))

    def transition(self, record, state, reason=None):
        if (record.data["state"], record.data["failure_reason"]) != (state, reason):
            self.planning_revision += 1
        record.data.update(state=state, failure_reason=reason, updated_at=self.clock.now().isoformat())

    async def planning_snapshot(self):
        from ev_fleet.contracts.crds import CrdsPlanningSnapshot

        async with self.store.lock:
            return CrdsPlanningSnapshot(
                instance_id=self.planning_instance_id, revision=self.planning_revision,
                observed_at=self.clock.now(), dispatches=[
                    record.view() for record in self.store.records.values()
                    if record.data["command"] is not None
                    and (record.data["state"] not in TERMINAL or record.data["claim_held"] is not False)])

    async def estimate_route(self, body):
        from ev_fleet.contracts.crds import CrdsRouteEstimate

        fleet, facility = await asyncio.gather(
            request_json(self.client, "GET", f"{self.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot),
            request_json(self.client, "GET", f"{self.settings.facilities_url}/api/v1/facilities/{body.facility_id}", model=Facility))
        vehicle = next((v for v in fleet.vehicles if v.vehicle_id == body.vehicle_id), None)
        if (fleet.instance_id != body.expected_fleet_instance_id or vehicle is None
                or vehicle.control_version != body.expected_control_version or vehicle.telemetry is None
                or vehicle.stale or vehicle.connectivity != "ONLINE"):
            raise HTTPException(409, "ROUTE_ESTIMATE_INPUT_CHANGED")
        if facility.status != "OPEN" or not facility.is_fresh(self.clock.now()):
            raise HTTPException(409, "PARKING_DESTINATION_CHANGED")
        origin = vehicle.telemetry.position
        key = (vehicle.vehicle_id, facility.facility_id, str(fleet.instance_id), vehicle.control_version,
               origin.latitude, origin.longitude, facility.position.latitude, facility.position.longitude)
        cached = self.route_estimates.get(key)
        if cached and self.clock.monotonic() - cached[0] < 15:
            return cached[1]
        route = await driving_route(self.client, self.routing, origin.model_dump(),
                                    facility.position.model_dump(), self.settings.arrival_tolerance_km,
                                    facility=True)
        if route is None or route.source != "ONEMAP":
            raise HTTPException(409, "ONEMAP_ROUTE_REQUIRED")
        result = CrdsRouteEstimate(vehicle_id=vehicle.vehicle_id, facility_id=facility.facility_id,
            fleet_instance_id=fleet.instance_id, control_version=vehicle.control_version, origin=origin, route=route)
        if len(self.route_estimates) >= 256:
            self.route_estimates.pop(next(iter(self.route_estimates)))
        self.route_estimates[key] = (self.clock.monotonic(), result)
        return result

    def claim_url(self, record):
        return f"{self.settings.fleet_url}/api/v1/vehicles/{record.data['vehicle_id']}/claims/{record.data['dispatch_id']}"

    async def route_traffic_assessment(self, geometry, distance, duration):
        # Read-only bounded POST. Rebalancing owns the full 144k-link index;
        # Dispatch must never use its 800-link dashboard projection.
        return await request_json(self.client, "POST",
            f"{self.settings.rebalancing_url}/api/v1/traffic/assess-route",
            {"geometry": geometry, "distance_km": distance,
             "duration_seconds": duration})

    async def prepare(self, record):
        recommendation = await request_json(self.client, "GET",
            f"{self.settings.rebalancing_url}/api/v1/recommendations/{record.data['recommendation_id']}", model=Recommendation)
        if str(recommendation.recommendation_id) != record.data["recommendation_id"]:
            raise HTTPException(503, "DEPENDENCY_IDENTITY_MISMATCH")
        record_approval(record, "UNKNOWN", "APPROVAL_IN_PROGRESS", ApprovalDecision(
            status="UNKNOWN", reason_code="APPROVAL_IN_PROGRESS",
            vehicle_id=recommendation.selected_vehicle_id, facility_id=recommendation.parking_facility_id))
        if recommendation.expires_at <= self.clock.now():
            raise HTTPException(409, "RECOMMENDATION_EXPIRED")
        if recommendation.crds is not None and (
                self.crds_settings.mode != "ACTIVE" or not recommendation.crds.approval_allowed
                or recommendation.crds.decision != "MOVE"):
            raise HTTPException(409, "CRDS_ADVISORY_ONLY")
        if not recommendation.selected_vehicle_id:
            raise HTTPException(409, "NO_RECOMMENDED_VEHICLE")
        for other in self.store.records.values():
            if (other is not record and other.claim_request and not other.claimed
                    and other.data["state"] not in TERMINAL
                    and (other.data["vehicle_id"] == recommendation.selected_vehicle_id
                         or recommendation.parking_facility_id is not None
                         and other.data["parking_facility_id"] == recommendation.parking_facility_id)):
                raise HTTPException(409, "APPROVAL_RESOURCE_UNRESOLVED")
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
            destination_issue = None
            if parking.zone_id != target.zone_id or parking.position != recommendation.target_position:
                destination_issue = "PARKING_DESTINATION_CHANGED"
            elif parking.status != "OPEN":
                destination_issue = "PARKING_CLOSED" if parking.status == "CLOSED" else "FACILITY_DATA_STALE"
            elif not parking.is_fresh(self.clock.now()):
                destination_issue = "FACILITY_DATA_STALE"
            elif parking.parking_available <= 0:
                destination_issue = "PARKING_FULL"
            if destination_issue:
                raise ApprovalCheckError(409, "PARKING_DESTINATION_CHANGED", ApprovalDecision(
                    status="REJECTED", reason_code=destination_issue))
            destination = parking.position
            assessed_target["position"] = parking.position.model_dump(mode="json")
            assessed_target["charging"] = {
                "observed_at": self.clock.now().isoformat(),
                "available_slots": parking.charging_available}
        origin = vehicle.telemetry.position.model_dump() if vehicle.telemetry else None
        route = await driving_route(self.client, self.routing, origin, destination.model_dump(),
                                    self.settings.arrival_tolerance_km,
                                    facility=parking is not None) if origin else None
        if route is None or route.source != "ONEMAP":
            raise ApprovalCheckError(409, "ONEMAP_ROUTE_REQUIRED", ApprovalDecision(
                status="REJECTED", reason_code="NO_TELEMETRY" if origin is None else "ONEMAP_ROUTE_REQUIRED"))
        if route and self.traffic_enabled:
            # No full Speed Bands JSON or map sampling on the Dispatch path.
            # A valid configured OSRM alternate is considered before rejection.
            async def score(chosen):
                return await self.route_traffic_assessment(
                    [p.model_dump() for p in chosen.geometry],
                    chosen.distance_km, chosen.duration_seconds)
            route = await choose_traffic_route(self.client, route, origin,
                destination.model_dump(), self.settings.arrival_tolerance_km,
                {"source": "LTA_DATAMALL"}, self.clock.now(),
                facility=parking is not None, assess_route=score)
        if route:
            # Physical destination is the road endpoint, NOT the carpark's
            # off-road map coordinate. Fleet keeps the strict 50 m arrival rule.
            destination = route.geometry[-1]
            assessed_target["position"] = destination.model_dump(mode="json")
            # Routing may take longer than a telemetry interval. Revalidate a
            # fresh Fleet version without reusing a route from a moved origin.
            fresh = await request_json(self.client, "GET", f"{self.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot)
            if fresh.instance_id != fleet.instance_id:
                raise HTTPException(409, "FLEET_INSTANCE_CHANGED")
            vehicle = next((item for item in fresh.vehicles if item.vehicle_id == recommendation.selected_vehicle_id), None)
            if vehicle is None or vehicle.telemetry is None or vehicle.telemetry.position.model_dump() != origin:
                raise HTTPException(409, "ROUTE_ORIGIN_CHANGED")
        assessment = assess(vehicle.model_dump(mode="json"), assessed_target, policy, self.clock.now())
        if route:
            energy = route.distance_km * vehicle.consumption_kwh_per_km * max(policy.energy_margin_factor, self.settings.energy_margin_factor)
            arrival = vehicle.telemetry.soc_pct - 100 * energy / vehicle.battery_capacity_kwh
            assessment.update(distance_km=route.distance_km, energy_required_kwh=energy, predicted_arrival_soc_pct=arrival)
            reserve = max(policy.min_reserve_pct, self.settings.min_reserve_pct)
            if arrival < reserve:
                raise ApprovalCheckError(409, "ROUTE_BELOW_OPERATIONAL_RESERVE", ApprovalDecision(
                    status="REJECTED", reason_code="ROUTE_BELOW_OPERATIONAL_RESERVE",
                    arrival_soc_pct=arrival, reserve_pct=reserve))
        if not assessment["eligible"]:
            raise HTTPException(409, "VEHICLE_NOT_ELIGIBLE:" + ",".join(assessment["exclusion_reasons"]))
        if parking:
            if recommendation.purpose == "CHARGE" and assessment["current_soc_pct"] >= CHARGING_TARGET_SOC_PCT:
                raise HTTPException(409, "CHARGING_TARGET_ALREADY_REACHED")
            requires_charge = (assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
                               or recommendation.requires_charging)
            if parking.facility_type == "CHARGING" and not requires_charge:
                raise HTTPException(409, "CHARGING_NOT_REQUIRED")
            if requires_charge and parking.charging_available <= 0:
                raise HTTPException(409, "CHARGING_FULL")
            record.data["parking_facility_id"] = parking.facility_id
            record.parking_request = ParkingHoldRequest(
                dispatch_id=record.data["dispatch_id"], vehicle_id=vehicle.vehicle_id,
                expected_zone_id=target.zone_id, expected_position=parking.position,
                access_position=destination if route else None,
                requires_charging=requires_charge).model_dump(mode="json")
        if recommendation.crds is not None:
            deadline = self.clock.monotonic() + self.crds_settings.deadline_seconds
            for attempt in range(2):
                revision = self.planning_revision
                remaining = (recommendation.expires_at - self.clock.now()).total_seconds()
                if remaining <= 0:
                    raise HTTPException(409, "RECOMMENDATION_EXPIRED")
                budget = min(remaining, deadline - self.clock.monotonic())
                if budget <= 0:
                    raise HTTPException(409, "CRDS_PLANNING_CHANGED")
                body = CrdsRevalidationRequest(recommendation_id=recommendation.recommendation_id,
                    dispatch_id=record.data["dispatch_id"], route=route,
                    expected_planning_instance_id=self.planning_instance_id,
                    expected_planning_revision=revision)
                try:
                    revalidated = await request_json(self.client, "POST",
                        f"{self.settings.rebalancing_url}/api/v1/crds/revalidate",
                        body.model_dump(mode="json"), model=CrdsRevalidationResult, timeout_seconds=budget)
                except HTTPException as exc:
                    if attempt == 0 and exc.status_code == 409 and exc.detail == "CRDS_PLANNING_CHANGED":
                        continue
                    raise
                if self.planning_revision == revision:
                    break
            else:
                raise HTTPException(409, "CRDS_PLANNING_CHANGED")
            if not revalidated.allowed:
                evidence = revalidation_rejection(
                    revalidated.metadata, vehicle.vehicle_id, recommendation.parking_facility_id)
                raise ApprovalCheckError(409, "CRDS_BENEFIT_CHANGED:" + revalidated.metadata.reason_code,
                                         evidence)
            if recommendation.expires_at <= self.clock.now():
                raise HTTPException(409, "RECOMMENDATION_EXPIRED")
            assessment["crds"] = revalidated.metadata.model_dump(mode="json")
        command_id = uuid4()
        claim = ClaimRequest(command_id=command_id, expected_instance_id=fleet.instance_id,
            expected_version=vehicle.state_version,
            expected_control_version=vehicle.control_version,
            route_origin=route.geometry[0] if route else None,
            target_zone_id=target.zone_id,
            target_position=destination, approve_before=recommendation.expires_at,
            min_reserve_pct=max(policy.min_reserve_pct, self.settings.min_reserve_pct),
            route_factor=max(policy.route_factor, self.settings.route_factor),
            energy_margin_factor=max(policy.energy_margin_factor, self.settings.energy_margin_factor),
            route_distance_km=route.distance_km if route else None)
        command = RelocationCommand(dispatch_id=record.data["dispatch_id"], command_id=command_id,
            vehicle_id=vehicle.vehicle_id, fleet_instance_id=fleet.instance_id,
            target_zone_id=target.zone_id, target_position=destination, issued_at=self.clock.now(),
            min_reserve_pct=claim.min_reserve_pct, route_factor=claim.route_factor,
            energy_margin_factor=claim.energy_margin_factor, route=route)
        assessment["origin"] = {
            "position": origin,
            "zone_id": vehicle.current_zone_id,
            "facility_id": (
                vehicle.telemetry.parked_facility_id if vehicle.telemetry else None
            ),
        }
        assessment["approval_decision"] = approval_evidence(record)
        record.data.update(vehicle_id=vehicle.vehicle_id, command=command.model_dump(mode="json"),
                           revalidated_metrics=assessment)
        # Store the exact request BEFORE the remote mutation. Never silently refresh its version.
        record.claim_request = claim.model_dump(mode="json")

    async def acquire(self, record):
        # Applies to initial admission AND exact-request reconciliation retries.
        # Persistence failure prevents any subsequent remote resource mutation.
        await self.persist()
        if record.parking_request and record.data["parking_status"] != "RESERVED":
            hold = await request_json(self.client, "POST",
                f"{self.settings.facilities_url}/api/v1/facilities/{record.data['parking_facility_id']}/holds",
                record.parking_request, model=ParkingHold)
            if (str(hold.dispatch_id) != record.data["dispatch_id"] or
                    hold.vehicle_id != record.data["vehicle_id"] or
                    hold.facility_id != record.data["parking_facility_id"] or hold.status != "RESERVED"):
                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
            record.data["parking_status"] = hold.status
            self.planning_revision += 1
        receipt = await request_json(self.client, "PUT", self.claim_url(record), record.claim_request, VehicleClaim)
        self.check_receipt(record, receipt)
        record.claimed = True
        metrics = record.data.get("revalidated_metrics") or {}
        crds = metrics.get("crds") or {}
        reason = "CRDS_APPROVAL_CONFIRMED" if crds else "APPROVAL_CONFIRMED"
        record_approval(record, "APPROVED", reason, ApprovalDecision(
            status="APPROVED", reason_code=reason,
            arrival_soc_pct=metrics.get("predicted_arrival_soc_pct"),
            reserve_pct=record.claim_request["min_reserve_pct"],
            gain_vehicle_minutes=crds.get("worst_case_gain_vehicle_minutes"),
            minimum_gain_vehicle_minutes=crds.get("parameters", {}).get("minimum_gain_vehicle_minutes")))
        if record.claim_tick is None:
            elapsed = max(0, (self.clock.now() - receipt.acquired_at).total_seconds())
            record.claim_tick = self.clock.monotonic() - elapsed
        claim_held = receipt.status == "ACTIVE"
        if record.data["claim_held"] is not claim_held:
            record.data["claim_held"] = claim_held
            self.planning_revision += 1
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
                    receipt.vehicle_id != record.data["vehicle_id"] or receipt.status != wanted):
                raise HTTPException(503, "PARKING_HOLD_IDENTITY_MISMATCH")
            record.data["parking_status"] = receipt.status
            self.planning_revision += 1
        except HTTPException as exc:
            logging.warning("parking finalisation pending dispatch=%s error=%s",
                            record.data["dispatch_id"], exc.detail)

    async def traffic_check(self, dispatch_id):
        """Read-only active-journey condition check; never alters command identity."""
        record = await self.get(dispatch_id)
        cmd = record.get("command")
        if not self.traffic_enabled or not cmd or not cmd.get("route"):
            return {"status": "UNKNOWN", "action": "NONE", "reason": "NO_LIVE_ROAD_ROUTE"}
        if record["state"] in TERMINAL:
            return {"status": "FINISHED", "action": "NONE", "reason": "JOURNEY_TERMINAL"}
        fleet = await request_json(self.client, "GET",
            f"{self.settings.fleet_url}/api/v1/vehicles", model=FleetSnapshot)
        vehicle = next((v for v in fleet.vehicles if v.vehicle_id == record["vehicle_id"]), None)
        if vehicle is None or vehicle.stale or vehicle.telemetry is None:
            return {"status": "UNKNOWN", "action": "NONE", "reason": "TELEMETRY_UNAVAILABLE"}
        points = cmd["route"]["geometry"]
        location = vehicle.telemetry.position.model_dump()
        closest = min(range(len(points)), key=lambda i: distance_km(location, points[i]))
        if distance_km(location, points[closest]) > .05:
            return {"status": "UNKNOWN", "action": "NONE", "reason": "VEHICLE_NOT_ON_RECORDED_ROUTE"}
        remaining = points[closest:]
        if len(remaining) < 2:
            return {"status": "ARRIVING", "action": "NONE", "reason": "NEAR_DESTINATION"}
        estimated_length = sum(distance_km(p, q) for p, q in zip(remaining, remaining[1:]))
        total_geometry_length = sum(distance_km(p, q) for p, q in zip(points, points[1:]))
        baseline = cmd["route"]["duration_seconds"] * estimated_length / max(.001, total_geometry_length)
        report = await self.route_traffic_assessment(remaining, estimated_length, baseline)
        if report["blocked"]:
            return {"status": "BLOCKED_AHEAD", "action": "OPERATOR_REVIEW_REQUIRED",
                    "reason": "CLOSURE_ON_REMAINING_ROUTE", "assessment": report}
        if report["status"] != "FRESH":
            return {"status": "UNKNOWN", "action": "NONE",
                    "reason": "TRAFFIC_COVERAGE_INCOMPLETE", "assessment": report}
        if report["delay_factor"] is not None and report["delay_factor"] >= 1.5:
            return {"status": "CONGESTED_AHEAD", "action": "OPERATOR_REVIEW_REQUIRED",
                    "reason": "SIGNIFICANT_DELAY", "assessment": report}
        return {"status": "MONITORED", "action": "NONE", "reason": "NO_RECORDED_DISRUPTION",
                "assessment": report}

    async def approve(self, body):
        # Shared by legacy requests and CRDS batch children. Read-only planning
        # and route endpoints never take this admission lock.
        try:
            await asyncio.wait_for(self.admission_lock.acquire(), self.crds_settings.deadline_seconds)
        except asyncio.TimeoutError:
            raise HTTPException(503, "ADMISSION_BUSY_RETRY_SAME_REQUEST") from None
        try:
            return await self.approve_serial(body)
        finally:
            self.admission_lock.release()

    async def approve_serial(self, body):
        record = await self.store.begin(body)
        try:
            async with record.lock:
                if record.claimed or record.data["state"] in TERMINAL:
                    return record.view()
                try:
                    if not record.claim_request:
                        await self.prepare(record)
                        # Durable intent/IDs must precede the first remote hold
                        # or claim. On restart, reconcile this exact request.
                        self.planning_revision += 1
                    await self.acquire(record)
                except HTTPException as exc:
                    if not record.claim_request and str(exc.detail).startswith("ONEMAP_"):
                        # No remote mutation has occurred. Reject explicitly;
                        # never retry expired tokens/rate limits in reconciliation.
                        reason = str(exc.detail)
                        record_approval(record, "REJECTED", reason,
                                        exc.decision if isinstance(exc, ApprovalCheckError) else None)
                        self.transition(record, "REJECTED", reason)
                        return record.view()
                    decision = await self.handle_approval_error(record, exc)
                    if record.data["state"] == "REJECTED" and exc.status_code in (404, 409, 422):
                        raise ApprovalCheckError(409, exc.detail, decision) from exc
                return record.view()
        finally:
            await self.persist()

    async def handle_approval_error(self, record, exc):
        # A Fleet epoch change after an attempted claim cannot prove the old claim did not execute.
        uncertain = exc.status_code >= 500 or (record.claim_request and exc.detail in (
            "FLEET_INSTANCE_CHANGED", "CLAIM_ID_CONFLICT", "PARKING_HOLD_ID_CONFLICT"))
        state, reason = ("UNKNOWN" if uncertain else "REJECTED"), str(exc.detail)
        claim_held = None if uncertain and record.claim_request else False
        if record.data["claim_held"] is not claim_held:
            record.data["claim_held"] = claim_held
            self.planning_revision += 1
        decision = record_approval(record, state, reason,
                                   exc.decision if isinstance(exc, ApprovalCheckError) else None)
        self.transition(record, state, reason)
        if not uncertain:
            await self.sync_parking(record)
        return decision

    async def refresh(self, record):
        """Caller serializes this dispatch; Fleet owns the vehicle's atomic state."""
        if stop_pending(record):
            return
        if record.data["state"] in TERMINAL:
            await self.sync_parking(record)
            return
        if not record.claimed:
            try:
                if not record.claim_request:
                    # No durable resource intent exists yet. Only explicit
                    # approval retries may prepare it through admission order.
                    reason = "PREPARATION_INCOMPLETE_RETRY_SAME_REQUEST"
                    if record.data["state"] != "UNKNOWN" or record.data["failure_reason"] != reason:
                        self.transition(record, "UNKNOWN", reason)
                    return
                await self.acquire(record)
            except HTTPException as exc:
                await self.handle_approval_error(record, exc)
            return
        try:
            receipt = await request_json(self.client, "GET", self.claim_url(record), model=VehicleClaim)
            self.check_receipt(record, receipt)
            if receipt.status == "RELEASED":
                self.finish(record, receipt.outcome)
                await self.sync_parking(record)
                return
            if record.data["claim_held"] is not True:
                record.data["claim_held"] = True
                self.planning_revision += 1
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
                if (telemetry.distance_travelled_km is not None
                        and telemetry.distance_travelled_km > record.data["distance_travelled_km"]):
                    record.data["distance_travelled_km"] = telemetry.distance_travelled_km
                    self.planning_revision += 1
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
        await self.persist_if_changed()
        return result

    async def commands(self, vehicle_id):
        try:
            for record in list(self.store.records.values()):
                if record.data["vehicle_id"] != vehicle_id:
                    continue
                async with record.lock:
                    await self.refresh(record)
                    if (stop_pending(record) or not record.claimed or record.data["state"] in TERMINAL or record.release_outcome or
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
            await self.persist_if_changed()

    def check_command(self, record, body):
        if stop_pending(record):
            raise HTTPException(409, "RELOCATION_STOP_PENDING")
        if (not record.claimed or not record.data["delivery_attempts"] or
            record.data["command"]["command_id"] != str(body.command_id) or
            record.data["vehicle_id"] != body.vehicle_id):
            raise HTTPException(409, "COMMAND_ID_CONFLICT")

    async def acknowledge(self, dispatch_id, body, *, simulator_generation=None, simulator_session_id=None):
        record = self.store.get(dispatch_id)
        async with record.lock:
            self.check_command(record, body)
            await self.recovery.guard(record, simulator_generation, simulator_session_id)
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

    async def event(self, dispatch_id, body, *, simulator_generation=None, simulator_session_id=None):
        record = self.store.get(dispatch_id)
        async with record.lock:
            self.check_command(record, body)
            await self.recovery.guard(record, simulator_generation, simulator_session_id)
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
            if body.distance_travelled_km != record.data["distance_travelled_km"]:
                record.data["distance_travelled_km"] = body.distance_travelled_km
                self.planning_revision += 1
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

    @staticmethod
    def approval_status(record):
        if record.claimed:
            return "APPROVED"
        return "REJECTED" if record.data["state"] == "REJECTED" else "UNKNOWN"

    def approval_view(self, request_id):
        key = self.store.requests.get(str(request_id))
        if key is None:
            raise HTTPException(404, "APPROVAL_NOT_FOUND")
        record = self.store.get(key)
        return {**record.view(), "approval_status": self.approval_status(record)}

    async def close_unstarted(self, request_id: UUID, recommendation_id: UUID | None = None):
        async with self.admission_lock:
            key = self.store.requests.get(str(request_id))
            if recommendation_id is not None:
                record = await self.store.begin(DispatchApproval(
                    request_id=request_id, recommendation_id=recommendation_id))
            elif key is not None:
                record = self.store.get(key)
            else:
                raise HTTPException(404, "APPROVAL_NOT_FOUND")
            async with record.lock:
                if (record.claimed or record.data["claim_held"] is not False
                        or record.claim_request or record.data["command"]
                        or record.data["parking_status"] or record.data["delivery_attempts"]
                        or record.ack or record.events):
                    raise HTTPException(409, "APPROVAL_MAY_HAVE_ACQUIRED_RESOURCES")
                if record.data["state"] not in TERMINAL:
                    record_approval(record, "REJECTED", "OPERATOR_CLOSED_UNSTARTED_APPROVAL")
                    self.transition(record, "REJECTED", "OPERATOR_CLOSED_UNSTARTED_APPROVAL")
                await self.persist()
                return self.approval_view(request_id)

    def batch_view(self, data):
        items = []
        for stored in data["items"]:
            item = dict(stored)
            key = item.get("dispatch_id") or self.store.requests.get(
                str(uuid5(UUID(data["request_id"]), item["recommendation_id"])))
            child = self.store.records.get(key)
            if child:
                item.update(dispatch_id=key, state=child.data["state"], error=child.data["failure_reason"])
                item["approval_status"] = self.approval_status(child)
                item["approval_decision"] = approval_evidence(child) or None
            else:
                item["approval_status"] = "REJECTED" if item["state"] == "REJECTED" else "UNKNOWN"
            items.append(item)
        unknown = any(i["approval_status"] == "UNKNOWN" for i in items)
        approved = sum(i["approval_status"] == "APPROVED" for i in items)
        status = ("UNKNOWN" if unknown else "APPROVED" if approved == len(items)
                  else "PARTIAL" if approved else "REJECTED")
        return {**data, "items": items, "status": status}

    async def close_batch_item(self, request_id: UUID, recommendation_id: UUID):
        async with self.batch_lock:
            data = self.batches.get(str(request_id))
            item = next((i for i in data["items"]
                         if i["recommendation_id"] == str(recommendation_id)), None) if data else None
            if item is None:
                raise HTTPException(404, "BATCH_ITEM_NOT_FOUND")
            child_id = uuid5(request_id, item["recommendation_id"])
            if item.get("dispatch_id") and self.store.requests.get(str(child_id)) != item["dispatch_id"]:
                raise HTTPException(409, "BATCH_CHILD_UNAVAILABLE")
            if item["state"] != "REJECTED" or str(child_id) in self.store.requests:
                await self.close_unstarted(child_id, recommendation_id)
            return self.batch_view(data)

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
            await self.persist()
            for item in data["items"]:
                if item["state"] not in ("PENDING", "UNKNOWN"):
                    continue
                # Stable child idempotency key across HTTP retries.
                child_id = uuid5(body.request_id, item["recommendation_id"])
                existing_id = item.get("dispatch_id") or self.store.requests.get(str(child_id))
                child = self.store.records.get(existing_id)
                if child and (child.claimed or child.data["state"] in TERMINAL):
                    item["dispatch_id"] = existing_id
                    continue
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

    async def reconcile_once(self):
        changed = False
        for record in list(self.store.records.values()):
            if record.lock.locked():
                continue
            try:
                if record.resolution and record.resolution["status"] in ("REQUESTED", "CLEANUP_PENDING"):
                    await advance_resolution(self, record)
                    continue
                if record.recovery and not (record.resolution and record.resolution["status"] == "ENDED"):
                    await self.recovery.advance(record)
                if record.data["state"] in TERMINAL and record.data["parking_status"] != "RESERVED":
                    continue
                async with record.lock:
                    if record.recovery and record.recovery["status"] == "REQUESTED":
                        continue
                    before = self.record_checkpoint(record)
                    await self.refresh(record)
                    record_changed = self.durable_record(record) != before
                changed = changed or record_changed
            except Exception:
                logging.exception("dispatch reconciliation failed: %s", record.data["dispatch_id"])
        if changed:
            try:
                await self.persist_if_changed()
            except Exception:
                logging.exception("dispatch reconciliation persistence failed")

    async def reconcile_loop(self):
        while True:
            await self.reconcile_once()
            await asyncio.sleep(self.settings.reconcile_interval_seconds)
