"""Serialized checkpoint publisher with guarded idle and coordinated trip restore."""
import asyncio
from copy import deepcopy
import logging
import time
from uuid import uuid4

import httpx

from ev_fleet.contracts.common import Position, utcnow
from ev_fleet.contracts.dispatch import CommandList, Dispatch, DispatchEvent, DispatchList
from ev_fleet.contracts.recovery import (
    RecoveryAssessment, SimulatorCheckpoint, SimulatorCheckpointReceipt,
    SimulatorCheckpointRequest, SimulatorConfiguration, SimulatorSession, SimulatorSessionRequest,
    SimulatorCheckpointSnapshot, RecoveryHandoffRequest, SimulatorIdleRestoreRequest,
)
from ev_fleet.contracts.vehicles import FleetSnapshot, VehicleState
from .movement import Movement
from .traffic_motion import segment_speed


class RecoveryRequired(Exception):
    """Keep the node alive without publishing invented physical state."""


def prepare_restore(checkpoint, configuration, fleet, dispatch=None, assessment=None):
    """Validate recorded movement; the caller must also verify session authority."""
    saved = SimulatorCheckpoint.model_validate(checkpoint)
    fleet = FleetSnapshot.model_validate(fleet)
    vehicle = next((v for v in fleet.vehicles if v.vehicle_id == saved.vehicle_id), None)
    if vehicle is None or saved.fleet_instance_id != fleet.instance_id:
        raise RecoveryRequired("RESTORE_FLEET_IDENTITY_MISMATCH")
    movement = Movement.from_checkpoint(saved, configuration)
    if (movement.capacity != vehicle.battery_capacity_kwh
            or movement.consumption != vehicle.consumption_kwh_per_km
            or vehicle.active_rental or vehicle.active_charging_task):
        raise RecoveryRequired("RESTORE_VEHICLE_CONFIGURATION_OR_TASK_CONFLICT")
    if vehicle.telemetry and vehicle.telemetry.odometer_km is not None:
        if saved.physical.odometer_km < vehicle.telemetry.odometer_km:
            raise RecoveryRequired("RESTORE_ODOMETER_REGRESSION")
    if saved.command is None:
        if vehicle.active_claim:
            raise RecoveryRequired("RESTORE_CLAIM_MISMATCH")
        return movement
    if dispatch is None or assessment is None:
        raise RecoveryRequired("RESTORE_DISPATCH_EVIDENCE_REQUIRED")
    dispatch = Dispatch.model_validate(dispatch)
    assessment = RecoveryAssessment.model_validate(assessment)
    if (dispatch.dispatch_id != saved.command.dispatch_id or dispatch.vehicle_id != saved.vehicle_id
            or dispatch.command != saved.command or assessment.dispatch_id != dispatch.dispatch_id
            or saved.command.command_id == saved.completed_command_id):
        raise RecoveryRequired("RESTORE_COMMAND_IDENTITY_MISMATCH")
    claim = vehicle.active_claim
    if claim is None:
        if dispatch.claim_held is not False or dispatch.state not in ("SUCCEEDED", "REJECTED"):
            raise RecoveryRequired("RESTORE_CLAIM_OWNERSHIP_UNCONFIRMED")
    elif (claim.dispatch_id != dispatch.dispatch_id or claim.command_id != saved.command.command_id
            or claim.fleet_instance_id != fleet.instance_id or claim.target_zone_id != saved.command.target_zone_id
            or claim.target_position != saved.command.target_position):
        raise RecoveryRequired("RESTORE_CLAIM_MISMATCH")
    if (saved.physical.distance_travelled_km < dispatch.distance_travelled_km
            or saved.next_event_sequence <= assessment.evidence.last_event_sequence
            or assessment.evidence.acknowledgement_accepted is not None
            and assessment.evidence.acknowledgement_accepted != saved.accepted):
        raise RecoveryRequired("RESTORE_EXECUTION_EVIDENCE_CONFLICT")
    telemetry = vehicle.telemetry
    if (telemetry and telemetry.active_dispatch_id == dispatch.dispatch_id
            and telemetry.distance_travelled_km is not None
            and telemetry.distance_travelled_km > saved.physical.distance_travelled_km):
        raise RecoveryRequired("RESTORE_PROGRESS_REGRESSION")
    return movement


def error_code(exc):
    try:
        return exc.response.json().get("error", {}).get("code", "HTTP_ERROR")
    except (ValueError, AttributeError):
        return "HTTP_ERROR"


class CheckpointRunner:
    def __init__(self, node):
        self.node = node
        self.configuration = SimulatorConfiguration(capacity_kwh=node.movement.capacity,
            consumption_kwh_per_km=node.movement.consumption, speed_kph=node.speed_kph, time_scale=node.time_scale)
        self.lease = None
        self.fleet_instance_id = None
        self.revision = 0
        self.sequence = 0
        self.next_event_sequence = 0
        self.acknowledgement = None
        self.acknowledged = False
        self.pending_event = None
        self.pending_checkpoint = None
        self.pending_session = None
        self.pending_idle_restore = None
        self.last_checkpoint = None
        self.last_motion = time.monotonic()
        self.next_poll = self.next_telemetry = self.next_traffic = 0.0
        self.renew_margin = 5.0
        self.blocked = None
        self.restore_pending = False
        self.consumer_id = str(uuid4())

    def stage_coordinated_restore(self, session, fleet, dispatch=None, assessment=None):
        """Install an authorized session and recorded state without advancing downtime."""
        if self.lease is not None or self.pending_checkpoint is not None or self.pending_session is not None:
            raise RecoveryRequired("RESTORE_REQUIRES_A_FRESH_RUNNER")
        receipt = SimulatorSession.model_validate(session)
        fleet = FleetSnapshot.model_validate(fleet)
        saved = receipt.checkpoint
        vehicle = next((v for v in fleet.vehicles if v.vehicle_id == self.node.vehicle_id), None)
        owner = vehicle.simulator_recovery if vehicle else None
        if (saved is None or saved.vehicle_id != self.node.vehicle_id or owner is None
                or owner.status != "ACTIVE" or owner.generation != receipt.generation
                or owner.owner_revision != receipt.owner_revision or owner.expires_at != receipt.expires_at
                or owner.checkpoint_revision != receipt.checkpoint_revision
                or saved.physical.session_id != receipt.session_id or receipt.expires_at <= utcnow()):
            raise RecoveryRequired("COORDINATED_RESTORE_OWNERSHIP_UNCONFIRMED")
        if saved.physical.parked_facility_id and not self.node.facilities_url:
            raise RecoveryRequired("PARKING_VERIFICATION_UNAVAILABLE")
        movement = prepare_restore(saved, self.configuration, fleet, dispatch, assessment)
        self.node.movement = movement
        self.node.session = str(receipt.session_id)
        self.node.initialized = True
        self.node.parking_verified = not bool(self.node.facilities_url)
        self.node.ready.clear()
        self.lease, self.fleet_instance_id = receipt, fleet.instance_id
        self.revision, self.sequence = receipt.checkpoint_revision, saved.physical.sequence + 1
        self.last_checkpoint = saved
        self.acknowledgement, self.acknowledged = saved.acknowledgement, saved.acknowledged
        self.pending_event, self.next_event_sequence = saved.pending_event, saved.next_event_sequence
        self.pending_session = self.pending_checkpoint = None
        self.blocked = None
        self.restore_pending = True
        self.next_poll = self.next_telemetry = self.next_traffic = 0.0
        self.last_motion = time.monotonic()

    async def request(self, method, url, body=None):
        try:
            if hasattr(body, "model_dump"):
                body = body.model_dump(mode="json")
            headers = None
            if (self.lease and method == "POST" and url.startswith(self.node.dispatch_url + "/api/v1/dispatches/")
                    and (url.endswith("/acknowledgements") or url.endswith("/events"))):
                headers = {"X-Simulator-Generation": str(self.lease.generation),
                           "X-Simulator-Session": str(self.lease.session_id)}
            result = await self.node.request(method, url, body, headers=headers)
            if not isinstance(result, dict):
                raise RecoveryRequired("DEPENDENCY_RESPONSE_INVALID")
            return result
        finally:
            # Network/commit/retry time is paused simulation time.
            self.last_motion = time.monotonic()

    def require_live(self):
        if self.lease is None or self.lease.expires_at <= utcnow():
            raise RecoveryRequired("SIMULATOR_LEASE_EXPIRED_COORDINATION_REQUIRED")

    async def session_request(self):
        body = self.pending_session
        receipt = SimulatorSession.model_validate(await self.request("POST",
            f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}/simulator-sessions", body))
        if receipt.checkpoint_revision != self.revision or receipt.checkpoint != self.last_checkpoint:
            raise RecoveryRequired("SIMULATOR_SESSION_CHECKPOINT_CHANGED")
        if self.lease and (receipt.generation != self.lease.generation or receipt.session_id != self.lease.session_id):
            raise RecoveryRequired("SIMULATOR_SESSION_OWNER_CHANGED")
        self.lease = receipt
        if self.node.session is None and not receipt.durable:
            logging.warning("vehicle=%s checkpoint_storage=ephemeral restart_durability=unavailable", self.node.vehicle_id)
        self.node.session = str(receipt.session_id)
        self.node.initialized = True
        self.pending_session = None
        remaining = (receipt.expires_at - utcnow()).total_seconds()
        self.renew_margin = min(5.0, max(.1, remaining / 3))
        self.require_live()

    async def bootstrap(self):
        node = self.node
        await self.request("POST", node.fleet_url + "/api/v1/vehicles", {
            "vehicle_id": node.vehicle_id, "battery_capacity_kwh": node.movement.capacity,
            "consumption_kwh_per_km": node.movement.consumption})
        fleet = FleetSnapshot.model_validate(await self.request("GET", node.fleet_url + "/api/v1/vehicles"))
        state = next((v for v in fleet.vehicles if v.vehicle_id == node.vehicle_id), None)
        if state is None:
            raise RecoveryRequired("SIMULATOR_VEHICLE_MISSING")
        previous = state.telemetry
        if (state.simulator_recovery or state.active_claim or state.active_rental or state.active_charging_task
                or previous and (previous.active_dispatch_id or previous.relocation_state != "IDLE"
                                 or previous.charging_state == "CHARGING")):
            raise RecoveryRequired("EXISTING_JOURNEY_OR_OWNER_REQUIRES_PHASE4_COORDINATION")
        if previous:
            if previous.parked_facility_id and not node.facilities_url:
                raise RecoveryRequired("PARKING_VERIFICATION_UNAVAILABLE")
            if previous.odometer_km is None or previous.heading_deg is None:
                raise RecoveryRequired("IDLE_CHECKPOINT_METADATA_MISSING")
            node.movement.position = previous.position.model_dump()
            node.movement.soc_pct = previous.soc_pct
            node.movement.odometer_km, node.movement.heading_deg = previous.odometer_km, previous.heading_deg
        elif node.road_match_initial:
            match = await self.request("POST", node.dispatch_url + "/api/v1/road-matches", node.movement.position)
            if match.get("source") not in ("ONEMAP", "SIMULATED") or not isinstance(match.get("position"), dict):
                raise RecoveryRequired("INVALID_INITIAL_ROAD_MATCH")
            node.movement.position = Position.model_validate(match["position"]).model_dump()
        node.movement.origin = deepcopy(node.movement.position)
        self.fleet_instance_id = fleet.instance_id
        self.pending_session = SimulatorSessionRequest(request_id=node.session_request, action="ACQUIRE",
            expected_fleet_instance_id=fleet.instance_id, expected_control_version=state.control_version,
            expected_generation=0, expected_owner_revision=0, expected_checkpoint_revision=0).model_dump(mode="json")
        await self.session_request()

    async def renew(self):
        self.require_live()
        state = VehicleState.model_validate(await self.request("GET",
            f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}"))
        if state.vehicle_id != self.node.vehicle_id:
            raise RecoveryRequired("SIMULATOR_VEHICLE_IDENTITY_CHANGED")
        claim = state.active_claim
        self.pending_session = SimulatorSessionRequest(request_id=uuid4(), action="RENEW",
            expected_fleet_instance_id=self.fleet_instance_id, expected_control_version=state.control_version,
            expected_generation=self.lease.generation, expected_owner_revision=self.lease.owner_revision,
            expected_checkpoint_revision=self.revision, session_id=self.lease.session_id,
            dispatch_id=claim.dispatch_id if claim else None,
            command_id=claim.command_id if claim else None).model_dump(mode="json")
        await self.session_request()

    def checkpoint_request(self):
        saved = self.node.movement.snapshot()
        physical = {**saved.pop("physical"), "session_id": self.node.session,
                    "sequence": self.sequence, "observed_at": utcnow()}
        self.sequence += 1
        if self.node.facilities_url and not self.node.parking_verified:
            physical["availability"] = "UNAVAILABLE"
        checkpoint = SimulatorCheckpoint(vehicle_id=self.node.vehicle_id, fleet_instance_id=self.fleet_instance_id,
            physical=physical, **saved, acknowledgement=self.acknowledgement, acknowledged=self.acknowledged,
            pending_event=self.pending_event, next_event_sequence=self.next_event_sequence,
            simulation=self.configuration)
        return SimulatorCheckpointRequest(request_id=uuid4(), session_id=self.lease.session_id,
            generation=self.lease.generation, expected_checkpoint_revision=self.revision,
            checkpoint=checkpoint, publish_telemetry=True).model_dump(mode="json")

    async def checkpoint(self):
        if self.pending_checkpoint is None:
            self.require_live()
            self.pending_checkpoint = self.checkpoint_request()
        for attempt in range(2):
            body = self.pending_checkpoint
            try:
                receipt = SimulatorCheckpointReceipt.model_validate(await self.request("PUT",
                    f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}/simulator-checkpoint", body))
            except httpx.HTTPStatusError as exc:
                code = error_code(exc)
                if attempt or code not in ("OBSERVATION_TIME_INVALID", "SIMULATOR_CHARGING_BASELINE_REQUIRED", "SIMULATOR_IDLE_CLAIM_CHANGED"):
                    raise
                self.require_live()
                if code in ("SIMULATOR_CHARGING_BASELINE_REQUIRED", "SIMULATOR_IDLE_CLAIM_CHANGED"):
                    if self.last_checkpoint is None:
                        raise RecoveryRequired(code) from exc
                    self.node.movement = Movement.from_checkpoint(self.last_checkpoint, self.configuration)
                    self.acknowledgement = self.last_checkpoint.acknowledgement
                    self.acknowledged = self.last_checkpoint.acknowledged
                    self.pending_event = self.last_checkpoint.pending_event
                    self.next_event_sequence = max(self.next_event_sequence, self.last_checkpoint.next_event_sequence)
                    await self.parking()
                # Definitively rejected: reserve a new sequence, never edit the old payload.
                self.pending_checkpoint = self.checkpoint_request()
                continue
            if (receipt.request_id != SimulatorCheckpointRequest.model_validate(body).request_id
                    or receipt.generation != self.lease.generation
                    or receipt.checkpoint_revision != self.revision + 1):
                raise RecoveryRequired("CHECKPOINT_RECEIPT_CONFLICT")
            self.last_checkpoint = SimulatorCheckpoint.model_validate(body["checkpoint"])
            self.revision = receipt.checkpoint_revision
            self.pending_checkpoint = None
            self.next_telemetry = time.monotonic() + self.node.telemetry_delay()
            self.node.ready.set()
            return

    async def parking(self):
        if not self.node.facilities_url:
            self.node.parking_verified = True
            return False
        before = (deepcopy(self.node.movement.parking), self.node.parking_verified)
        try:
            await self.node.refresh_parking()
        except RuntimeError as exc:
            raise RecoveryRequired("PARKING_EVIDENCE_INVALID") from exc
        finally:
            self.last_motion = time.monotonic()
        return before != (self.node.movement.parking, self.node.parking_verified)

    async def acknowledge(self):
        self.require_live()
        command = self.node.movement.command
        await self.request("POST", f"{self.node.dispatch_url}/api/v1/dispatches/{command['dispatch_id']}/acknowledgements",
                           self.acknowledgement)
        self.acknowledged = True
        self.node.movement.start()
        await self.checkpoint()

    async def flush_event(self):
        self.require_live()
        command = self.node.movement.command
        payload = self.pending_event.model_dump(mode="json") if hasattr(self.pending_event, "model_dump") else self.pending_event
        try:
            await self.request("POST", f"{self.node.dispatch_url}/api/v1/dispatches/{command['dispatch_id']}/events", payload)
        except httpx.HTTPStatusError as exc:
            if error_code(exc) != "EVENT_TIME_INVALID":
                raise
            evidence = RecoveryAssessment.model_validate(await self.request("GET",
                f"{self.node.dispatch_url}/api/v1/dispatches/{command['dispatch_id']}/recovery"))
            event = DispatchEvent.model_validate(payload)
            if evidence.evidence.last_event_sequence >= event.sequence or event.observed_at >= utcnow():
                raise RecoveryRequired("EVENT_REPLAY_EVIDENCE_CONFLICT") from exc
            # Unaccepted and expired: persist removal, then generate a fresh higher sequence later.
        self.pending_event = None
        await self.checkpoint()

    async def poll(self):
        node = self.node
        if node.movement.command is None:
            response = CommandList.model_validate(await self.request("GET",
                f"{node.dispatch_url}/api/v1/vehicles/{node.vehicle_id}/commands"))
            if not response.commands:
                if await self.parking():
                    await self.checkpoint()  # verified baseline before charging
                return
            command = response.commands[0].model_dump(mode="json")
            self.require_live()
            if command["vehicle_id"] != node.vehicle_id:
                raise RecoveryRequired("COMMAND_VEHICLE_MISMATCH")
            if command["command_id"] == node.movement.completed_command_id:
                return
            node.movement.accept(command)
            self.acknowledged = False
            self.acknowledgement = {"command_id": command["command_id"], "vehicle_id": node.vehicle_id,
                                    "accepted": node.movement.accepted, "reason": node.movement.reason}
            self.next_event_sequence = 0
            await self.checkpoint()  # durable original ACK intent before the ACK request
        if not self.acknowledged:
            await self.acknowledge()
        command = node.movement.command
        state = Dispatch.model_validate(await self.request("GET",
            f"{node.dispatch_url}/api/v1/dispatches/{command['dispatch_id']}"))
        if state.command is None or state.command.model_dump(mode="json") != command:
            raise RecoveryRequired("DISPATCH_COMMAND_CHANGED")
        if state.claim_held is False and state.state in ("SUCCEEDED", "REJECTED"):
            node.movement.complete()
            self.acknowledgement = None
            self.acknowledged = False
            await self.checkpoint()  # idle/completion marker before refreshing parking/charging
            node.parking_verified = not bool(node.facilities_url)
            return
        if state.state == "FAILED" or not node.movement.accepted:
            if state.state == "FAILED" and node.movement.state != "FAILED":
                raise RecoveryRequired("DISPATCH_EXECUTION_FAILED")
            return
        kind = {"RELOCATING": "PROGRESS", "ARRIVED": "ARRIVED", "FAILED": "FAILED"}.get(node.movement.state)
        if kind:
            self.pending_event = {"command_id": command["command_id"], "vehicle_id": node.vehicle_id,
                "sequence": self.next_event_sequence, "observed_at": utcnow().isoformat(), "kind": kind,
                "distance_travelled_km": node.movement.distance, "reason": node.movement.reason}
            self.next_event_sequence += 1
            await self.checkpoint()
            await self.flush_event()

    def advance(self):
        self.require_live()
        now = time.monotonic()
        elapsed, self.last_motion = max(0, now - self.last_motion), now
        previous = (self.node.movement.state, self.node.movement.charging_state)
        speed = segment_speed(self.node.movement, self.node.speed_kph, self.node.traffic_snapshot)
        if speed > 0:
            self.node.movement.advance(elapsed * self.node.time_scale, self.node.speed_kph,
                traffic_speed_cap_kph=speed if speed < self.node.speed_kph else None)
        else:
            self.node.movement.speed_kph = 0.0
        if self.node.parking_verified:
            self.node.movement.charge(elapsed * self.node.time_scale)
        return previous != (self.node.movement.state, self.node.movement.charging_state)

    async def tick(self):
        if self.pending_session:
            await self.session_request()
        if self.pending_checkpoint:
            await self.checkpoint()
        if self.lease is None:
            await self.bootstrap()
        self.require_live()
        if (self.lease.expires_at - utcnow()).total_seconds() <= self.renew_margin:
            await self.renew()
        if self.last_checkpoint is None or self.restore_pending:
            await self.checkpoint()
            self.restore_pending = False
        if self.pending_event:
            await self.flush_event()
        if self.node.movement.command and not self.acknowledged:
            await self.acknowledge()
        changed = self.advance()
        if changed or time.monotonic() >= self.next_telemetry:
            await self.checkpoint()
        if time.monotonic() >= self.next_poll:
            await self.poll()
            self.next_poll = time.monotonic() + self.node.poll_interval
        if self.node.traffic_enabled and time.monotonic() >= self.next_traffic:
            latest = await self.request("GET", self.node.rebalancing_url + "/api/v1/traffic/simulation-events")
            self.node.traffic_snapshot = latest if latest.get("source") == "SIMULATION" and latest.get("status") == "FRESH" else None
            self.next_traffic = time.monotonic() + 2

    async def try_handoff(self):
        """Resume safe idle stock or consume an operator-approved trip handoff."""
        fleet = FleetSnapshot.model_validate(await self.request("GET", self.node.fleet_url + "/api/v1/vehicles"))
        vehicle = next((v for v in fleet.vehicles if v.vehicle_id == self.node.vehicle_id), None)
        if vehicle is None:
            return
        if self.pending_idle_restore is not None:
            await self.restore_idle(fleet, vehicle, None)
            return
        owner = vehicle.simulator_recovery
        if (owner and owner.expires_at <= utcnow() and vehicle.stale
                and not vehicle.active_claim and not vehicle.active_rental and not vehicle.active_charging_task):
            saved = SimulatorCheckpointSnapshot.model_validate(await self.request("GET",
                f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}/simulator-checkpoint"))
            if (saved.checkpoint and saved.checkpoint.command is None
                    and saved.checkpoint.physical.charging_state == "NOT_CHARGING"):
                await self.restore_idle(fleet, vehicle, saved)
                return
        dispatch_id = vehicle.active_claim.dispatch_id if vehicle.active_claim else (
            vehicle.telemetry.active_dispatch_id if vehicle.telemetry else None)
        if dispatch_id is None:
            saved = SimulatorCheckpointSnapshot.model_validate(await self.request("GET",
                f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}/simulator-checkpoint"))
            if saved.checkpoint is None:
                return
            if saved.checkpoint.command:
                dispatch_id = saved.checkpoint.command.dispatch_id
            elif saved.checkpoint.completed_command_id:
                records = DispatchList.model_validate(await self.request("GET",
                    self.node.dispatch_url + f"/api/v1/dispatches?vehicle_id={self.node.vehicle_id}"))
                dispatch_id = next((d.dispatch_id for d in records.dispatches if d.command
                    and d.command.command_id == saved.checkpoint.completed_command_id), None)
        if dispatch_id is None:
            return
        url = f"{self.node.dispatch_url}/api/v1/dispatches/{dispatch_id}"
        assessment = RecoveryAssessment.model_validate(await self.request("GET", url + "/recovery"))
        if assessment.operation is None or assessment.operation.status != "RESTORING":
            return
        session = SimulatorSession.model_validate(await self.request("POST", url + "/recovery/handoff",
            RecoveryHandoffRequest(consumer_id=self.consumer_id, simulation=self.configuration)))
        # Read authoritative evidence after claiming the single-consumer handoff.
        fleet = FleetSnapshot.model_validate(await self.request("GET", self.node.fleet_url + "/api/v1/vehicles"))
        dispatch = Dispatch.model_validate(await self.request("GET", url))
        assessment = RecoveryAssessment.model_validate(await self.request("GET", url + "/recovery"))
        prepared = CheckpointRunner(self.node)
        prepared.consumer_id = self.consumer_id
        prepared.stage_coordinated_restore(session, fleet, dispatch, assessment)
        self.__dict__.update(prepared.__dict__)

    async def restore_idle(self, fleet, vehicle, saved):
        if self.pending_idle_restore is None:
            self.pending_idle_restore = SimulatorIdleRestoreRequest(request_id=uuid4(),
                expected_fleet_instance_id=fleet.instance_id,
                expected_control_version=saved.control_version, expected_generation=saved.generation,
                expected_owner_revision=vehicle.simulator_recovery.owner_revision,
                expected_checkpoint_revision=saved.checkpoint_revision, simulation=self.configuration)
        try:
            session = SimulatorSession.model_validate(await self.request("POST",
                f"{self.node.fleet_url}/api/v1/vehicles/{self.node.vehicle_id}/simulator-sessions",
                self.pending_idle_restore))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code < 500:
                self.pending_idle_restore = None
            raise
        if session.expires_at <= utcnow():
            self.pending_idle_restore = None
            raise RecoveryRequired("IDLE_RECOVERY_SESSION_EXPIRED")
        # Refresh after the write, including on an exact retry of a lost response.
        current = FleetSnapshot.model_validate(await self.request("GET", self.node.fleet_url + "/api/v1/vehicles"))
        prepared = CheckpointRunner(self.node)
        prepared.stage_coordinated_restore(session, current)
        self.__dict__.update(prepared.__dict__)
        logging.info("vehicle=%s idle_simulation_restored generation=%s", self.node.vehicle_id, session.generation)

    async def run(self):
        async with httpx.AsyncClient(timeout=2) as owned:
            self.node.client = self.node.client or owned
            while True:
                delay = self.node.tick_seconds
                if self.blocked:
                    status = "REQUIRED" if "COORDINATION" in self.blocked or self.blocked == "SIMULATOR_LEASE_EXPIRED" else "BLOCKED"
                    logging.warning("vehicle=%s recovery=%s reason=%s", self.node.vehicle_id, status, self.blocked)
                    try:
                        await self.try_handoff()
                    except (httpx.HTTPError, RecoveryRequired, ValueError) as exc:
                        reason = error_code(exc) if isinstance(exc, httpx.HTTPStatusError) else str(exc)
                        logging.warning("vehicle=%s handoff_wait=%s", self.node.vehicle_id, reason)
                    if self.blocked:
                        await asyncio.sleep(5)
                    self.last_motion = time.monotonic()
                    continue
                try:
                    await self.tick()
                except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                    if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                        self.blocked = str(error_code(exc))
                    logging.warning("vehicle=%s checkpoint_paused=%s", self.node.vehicle_id, type(exc).__name__)
                    delay = 5
                except (RecoveryRequired, ValueError) as exc:
                    self.blocked = str(exc) if isinstance(exc, RecoveryRequired) else "CHECKPOINT_MODEL_OR_CONFIGURATION_INVALID"
                    delay = 5
                # All communication failures and waits suspend motion and charging.
                await asyncio.sleep(delay)
                if delay == 5:
                    self.last_motion = time.monotonic()
