"""Single-replica Fleet ownership/checkpoints; takeover needs Dispatch coordination."""
import asyncio
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import json
from uuid import uuid4

import httpx
from fastapi import HTTPException

from ev_fleet.common.settings import SimulatorRecoverySettings
from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.recovery import (
    RecoveryAssessment, SimulatorCheckpoint, SimulatorCheckpointReceipt, SimulatorSession,
    SimulatorCheckpointSnapshot, SimulatorConfiguration, SimulatorStopRequest, idle_before_command, stop_checkpoint_evidence,
)
from ev_fleet.contracts.facilities import ParkingObservation
from ev_fleet.contracts.dispatch import DispatchList, VehicleClaim


def fingerprint(model):
    return hashlib.sha256(json.dumps(model.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()


@dataclass(frozen=True)
class ChargingEvidence:
    observation: ParkingObservation
    requested_tick: float


class SimulatorRecovery:
    evidence_timeout = 2.0
    evidence_max_age = 2.0

    def __init__(self, store, settings=None):
        self.store = store
        self.settings = settings or SimulatorRecoverySettings.from_env()
        self.charging_epoch = str(uuid4())
        self.charging_client: httpx.AsyncClient | None = None

    async def close(self) -> None:
        client, self.charging_client = self.charging_client, None
        if client is not None:
            await client.aclose()

    @staticmethod
    def needs_charging_evidence(checkpoint: SimulatorCheckpoint) -> bool:
        return (checkpoint.command is None
                and checkpoint.physical.charging_state != "NOT_CHARGING")

    async def read_charging_evidence(self, vehicle_id: str) -> ChargingEvidence:
        """One nonce-bound direct read; caller must not hold the Fleet lock."""
        requested_tick = self.store.clock.monotonic()
        request_id = uuid4()
        if self.charging_client is None:
            self.charging_client = httpx.AsyncClient(
                timeout=self.evidence_timeout,
                limits=httpx.Limits(max_connections=32,
                                   max_keepalive_connections=16))
        try:
            response = await asyncio.wait_for(
                self.charging_client.get(
                    f"{self.store.settings.facilities_url}/api/v1/vehicles/"
                    f"{vehicle_id}/parking-observation",
                    params={"request_id": str(request_id)},
                    headers={"Cache-Control": "no-cache, no-store"}),
                timeout=self.evidence_timeout)
            response.raise_for_status()
            observation = ParkingObservation.model_validate(response.json())
        except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(
                503, "SIMULATOR_CHARGING_EVIDENCE_UNAVAILABLE") from exc
        if observation.request_id != request_id:
            raise HTTPException(503, "SIMULATOR_CHARGING_EVIDENCE_UNAVAILABLE")
        return ChargingEvidence(observation, requested_tick)

    def guarded(self, vehicle_id, record):
        # Disabling configuration cannot restore the old unguarded write path.
        return record.get("simulator_owner") is not None or (
            self.settings.enabled and vehicle_id in self.settings.vehicle_ids)

    def gate(self, vehicle_id):
        if not self.settings.enabled:
            raise HTTPException(409, "SIMULATOR_RECOVERY_DISABLED")
        if vehicle_id not in self.settings.vehicle_ids:
            raise HTTPException(409, "SIMULATOR_IDENTITY_NOT_CONFIGURED")
        if not self.store.ready:
            raise HTTPException(503, "SIMULATOR_PERSISTENCE_UNAVAILABLE")

    def owner(self, record, session_id, generation, *, require_live=True):
        owner = record.get("simulator_owner")
        if (owner is None or owner["session_id"] != str(session_id)
                or owner["generation"] != generation or record["session_id"] != session_id):
            raise HTTPException(409, "SIMULATOR_OWNER_CONFLICT")
        if require_live and owner["expires_at"] <= self.store.clock.now():
            raise HTTPException(409, "SIMULATOR_LEASE_EXPIRED")
        return owner

    async def commit(self, vehicle_id, staged, sample=None):
        """Publish in memory only after the snapshot/history transaction succeeds."""
        store = self.store
        store.ensure_consistent()
        payload = {"instance_id": store.instance_id, "version": store.version + 1,
                   "vehicles": {**store.vehicles, vehicle_id: staged}, "charging_tasks": store.charging_tasks}
        try:
            if sample is None:
                await store.repository.save(payload)
            else:
                await store.repository.save(payload, telemetry_sample=sample)
        except asyncio.CancelledError:
            store.mark_persistence_uncertain()
            raise
        except Exception as exc:
            # A lost commit response is ambiguous. Do not overwrite durable
            # ownership from old memory; restart/reload Fleet before any writes.
            store.mark_persistence_uncertain()
            raise HTTPException(503, "SIMULATOR_PERSISTENCE_UNCERTAIN") from exc
        store.vehicles[vehicle_id] = staged
        store.version += 1
        if sample is not None:
            store.cache_telemetry(sample)

    async def session(self, vehicle_id, body):
        if body.action == "RESTORE_IDLE":
            return await self.restore_idle(vehicle_id, body)
        if body.action == "RESTORE":
            return await self.restore(vehicle_id, body)
        store = self.store
        async with store.lock:
            self.gate(vehicle_id)
            record = store.record(vehicle_id)
            if str(body.expected_fleet_instance_id) != store.instance_id:
                raise HTTPException(409, "FLEET_INSTANCE_CHANGED")
            owner = record.get("simulator_owner")
            request_fingerprint = fingerprint(body)
            if owner and owner["request_id"] == str(body.request_id):
                if owner["request_fingerprint"] != request_fingerprint:
                    raise HTTPException(409, "SIMULATOR_REQUEST_ID_CONFLICT")
                # This returns the original expiry; retries never extend a lease.
                return SimulatorSession.model_validate(owner["receipt"])
            checkpoint = record.get("simulator_checkpoint")
            revision = checkpoint["revision"] if checkpoint else 0
            if body.expected_checkpoint_revision != revision or body.expected_control_version != record["control_version"]:
                raise HTTPException(409, "SIMULATOR_EVIDENCE_CHANGED")
            claim = record["active_claim"]
            if claim is not None:
                if (str(body.dispatch_id) != claim["dispatch_id"] or str(body.command_id) != claim["command_id"]
                        or claim["fleet_instance_id"] != store.instance_id or claim["status"] != "ACTIVE"):
                    raise HTTPException(409, "SIMULATOR_CLAIM_MISMATCH")
            elif body.dispatch_id is not None:
                raise HTTPException(409, "SIMULATOR_CLAIM_MISMATCH")
            if record["active_rental"] or record["active_charging_task"]:
                raise HTTPException(409, "SIMULATOR_TASK_CONFLICT")
            staged = deepcopy(record)
            if body.action == "ACQUIRE":
                physical = record["telemetry"]
                if (owner is not None or checkpoint is not None or claim is not None
                        or physical and (physical.active_dispatch_id is not None
                                         or physical.relocation_state != "IDLE"
                                         or physical.charging_state == "CHARGING")):
                    raise HTTPException(409, "SIMULATOR_RECOVERY_COORDINATION_REQUIRED")
                if body.request_id in record["retired_requests"]:
                    raise HTTPException(409, "SESSION_REQUEST_SUPERSEDED")
                if record["session_request_id"]:
                    staged["retired_requests"].add(record["session_request_id"])
                staged["session_request_id"] = body.request_id
                staged["session_id"] = uuid4()
                generation, owner_revision = 1, 1
                staged["control_version"] += 1
            else:
                owner = self.owner(record, body.session_id, body.expected_generation)
                if body.expected_owner_revision != owner["revision"]:
                    raise HTTPException(409, "SIMULATOR_OWNER_REVISION_CONFLICT")
                generation, owner_revision = owner["generation"], owner["revision"] + 1
            expires_at = store.clock.now() + timedelta(seconds=self.settings.lease_seconds)
            receipt = SimulatorSession(session_id=staged["session_id"], generation=generation,
                owner_revision=owner_revision, expires_at=expires_at, checkpoint_revision=revision,
                checkpoint=SimulatorCheckpoint.model_validate(checkpoint["data"]) if checkpoint else None,
                durable=store.enabled)
            staged["simulator_owner"] = {"generation": generation, "revision": owner_revision,
                "session_id": str(staged["session_id"]), "expires_at": expires_at,
                "request_id": str(body.request_id), "request_fingerprint": request_fingerprint,
                "receipt": receipt.model_dump(mode="json")}
            staged["state_version"] += 1
            await self.commit(vehicle_id, staged)
            return receipt

    async def snapshot(self, vehicle_id):
        async with self.store.lock:
            record = self.store.record(vehicle_id)
            owner, saved = record.get("simulator_owner"), record.get("simulator_checkpoint")
            registration = record["registration"]
            return SimulatorCheckpointSnapshot(fleet_instance_id=self.store.instance_id,
                control_version=record["control_version"],
                configured=self.settings.enabled and vehicle_id in self.settings.vehicle_ids,
                generation=owner["generation"] if owner else 0,
                checkpoint_revision=saved["revision"] if saved else 0,
                checkpoint=SimulatorCheckpoint.model_validate(saved["data"]) if saved else None,
                configuration=SimulatorConfiguration(capacity_kwh=registration.battery_capacity_kwh,
                    consumption_kwh_per_km=registration.consumption_kwh_per_km,
                    speed_kph=30, time_scale=self.settings.simulation_time_scale))

    def restore_retry(self, record, body):
        previous = record.get("simulator_restore")
        if previous and previous["request_id"] == str(body.request_id):
            if previous["fingerprint"] != fingerprint(body):
                raise HTTPException(409, "SIMULATOR_REQUEST_ID_CONFLICT")
            return SimulatorSession.model_validate(previous["receipt"])
        return None

    def idle_restore_record(self, vehicle_id, body):
        """Caller holds Fleet lock; exact retries precede mutable checks."""
        store = self.store
        self.gate(vehicle_id)
        record = store.record(vehicle_id)
        retry = self.restore_retry(record, body)
        if retry:
            return record, None, retry
        owner = record.get("simulator_owner")
        previous = record.get("simulator_checkpoint")
        if (str(body.expected_fleet_instance_id) != store.instance_id
                or body.expected_control_version != record["control_version"]
                or owner is None or body.expected_generation != owner["generation"]
                or body.expected_owner_revision != owner["revision"]
                or previous is None
                or body.expected_checkpoint_revision != previous["revision"]):
            raise HTTPException(409, "SIMULATOR_EVIDENCE_CHANGED")
        if owner["expires_at"] > store.clock.now() or not store.view(record)["stale"]:
            raise HTTPException(409, "SIMULATOR_OWNER_STILL_ACTIVE")
        if record["active_claim"] or record["active_rental"] or record["active_charging_task"]:
            raise HTTPException(409, "SIMULATOR_TASK_CONFLICT")
        checkpoint = SimulatorCheckpoint.model_validate(previous["data"])
        physical = checkpoint.physical
        if (checkpoint.command is not None or physical.relocation_state != "IDLE"
                or physical.active_dispatch_id is not None or physical.speed_kph != 0
                or physical.session_id != record["session_id"]
                or str(physical.session_id) != owner["session_id"]):
            raise HTTPException(409, "IDLE_RECOVERY_CHECKPOINT_REQUIRED")
        if checkpoint.simulation != body.simulation:
            raise HTTPException(409, "SIMULATOR_CONFIGURATION_MISMATCH")
        # Rotation may have committed before its response was lost.
        telemetry = record["telemetry"]
        identity_fields = {"session_id", "sequence"}
        stopped = any(entry["receipt"]["command_id"] == str(checkpoint.completed_command_id)
                      and entry.get("operator_stop", {}).get("checkpoint_revision") == previous["revision"]
                      for entry in record["claims"].values())
        if not stopped and (telemetry is None or physical.model_dump(exclude=identity_fields)
                != telemetry.model_dump(exclude=identity_fields)):
            raise HTTPException(409, "IDLE_RECOVERY_TELEMETRY_MISMATCH")
        self.validate_checkpoint(vehicle_id, record, checkpoint)
        return record, checkpoint, None

    async def restore_idle(self, vehicle_id, body):
        """Fence an expired idle owner after unlocked dependency observations."""
        store = self.store
        async with store.lock:
            record, checkpoint, retry = self.idle_restore_record(vehicle_id, body)
            if retry:
                return retry
            if checkpoint is None:
                raise HTTPException(409, "IDLE_RECOVERY_CHECKPOINT_REQUIRED")
            saved = deepcopy(record)
        # A released Fleet claim alone cannot establish that Dispatch finished.
        try:
            async with httpx.AsyncClient(timeout=2) as client:
                response = await client.get(
                    self.settings.dispatch_url + "/api/v1/dispatches",
                    params={"vehicle_id": vehicle_id})
                response.raise_for_status()
                dispatches = DispatchList.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "IDLE_RECOVERY_DISPATCH_UNAVAILABLE") from exc
        if any(d.vehicle_id != vehicle_id or d.state not in ("SUCCEEDED", "REJECTED")
               or d.claim_held is not False
               or d.recovery and d.recovery.status in ("REQUESTED", "RESTORING")
               for d in dispatches.dispatches):
            raise HTTPException(409, "IDLE_RECOVERY_DISPATCH_UNRESOLVED")
        evidence = (await self.read_charging_evidence(vehicle_id)
                    if self.needs_charging_evidence(checkpoint) else None)
        async with store.lock:
            record, checkpoint, retry = self.idle_restore_record(vehicle_id, body)
            if retry:
                return retry
            if checkpoint is None:
                raise HTTPException(409, "IDLE_RECOVERY_CHECKPOINT_REQUIRED")
            if record != saved:
                raise HTTPException(503, "SIMULATOR_CHARGING_EVIDENCE_CHANGED")
            self.charging_context(vehicle_id, record, checkpoint, evidence)
            return await self.rotate_owner(vehicle_id, record, body, checkpoint)

    async def restore(self, vehicle_id, body):
        store = self.store
        async with store.lock:
            self.gate(vehicle_id)
            retry = self.restore_retry(store.record(vehicle_id), body)
            if retry:
                return retry
        # No Fleet/Dispatch lock is held during this callback: assessment reads Fleet.
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.settings.dispatch_url}/api/v1/dispatches/{body.dispatch_id}/recovery")
                response.raise_for_status()
                assessment = RecoveryAssessment.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "RECOVERY_AUTHORIZATION_UNAVAILABLE") from exc
        operation = assessment.operation
        if (assessment.dispatch_id != body.dispatch_id or operation is None
                or operation.request_id != body.request_id or operation.status not in ("REQUESTED", "RESTORING")
                or operation.intent_fingerprint != fingerprint(body)):
            raise HTTPException(409, "RECOVERY_NOT_AUTHORIZED")
        async with store.lock:
            self.gate(vehicle_id)
            record = store.record(vehicle_id)
            retry = self.restore_retry(record, body)
            if retry:
                return retry
            owner, previous = record.get("simulator_owner"), record.get("simulator_checkpoint")
            if (str(body.expected_fleet_instance_id) != store.instance_id
                    or body.expected_control_version != record["control_version"]
                    or body.expected_generation != (owner["generation"] if owner else 0)
                    or body.expected_checkpoint_revision != (previous["revision"] if previous else 0)):
                raise HTTPException(409, "SIMULATOR_EVIDENCE_CHANGED")
            if (owner and owner["expires_at"] > store.clock.now()) or not store.view(record)["stale"]:
                raise HTTPException(409, "SIMULATOR_OWNER_STILL_ACTIVE")
            if body.checkpoint.command and body.checkpoint.command.dispatch_id != body.dispatch_id:
                raise HTTPException(409, "RECOVERY_DISPATCH_MISMATCH")
            self.validate_checkpoint(vehicle_id, record, body.checkpoint)
            return await self.rotate_owner(vehicle_id, record, body, body.checkpoint)

    def stop_retry(self, record, body):
        entry = record["claims"].get(str(body.dispatch_id))
        previous = entry.get("operator_stop") if entry else None
        if previous is None:
            return None
        if previous["fingerprint"] != fingerprint(body):
            raise HTTPException(409, "SIMULATOR_STOP_ALREADY_RECORDED")
        return VehicleClaim.model_validate(entry["receipt"])

    async def stop_relocation(self, vehicle_id: str, body: SimulatorStopRequest):
        store = self.store
        if not store.enabled:
            raise HTTPException(503, "SIMULATOR_DURABLE_STORAGE_REQUIRED")
        async with store.lock:
            self.gate(vehicle_id)
            retry = self.stop_retry(store.record(vehicle_id), body)
            if retry:
                return retry
        try:
            async with httpx.AsyncClient(timeout=3) as client:
                response = await client.get(f"{self.settings.dispatch_url}/api/v1/dispatches/{body.dispatch_id}/resolution/intent")
                response.raise_for_status()
                authorized = SimulatorStopRequest.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(503, "STOP_AUTHORIZATION_UNAVAILABLE") from exc
        if authorized != body:
            raise HTTPException(409, "STOP_NOT_AUTHORIZED")

        async with store.lock:
            self.gate(vehicle_id)
            record = store.record(vehicle_id)
            retry = self.stop_retry(record, body)
            if retry:
                return retry
            owner, saved = record.get("simulator_owner"), record.get("simulator_checkpoint")
            claim = record["active_claim"]
            entry = record["claims"].get(str(body.dispatch_id))
            if (str(body.expected_fleet_instance_id) != store.instance_id
                    or record["control_version"] != body.expected_control_version
                    or not owner or owner["generation"] != body.expected_generation
                    or not saved or not claim or not entry or entry["receipt"] != claim
                    or claim["dispatch_id"] != str(body.dispatch_id)
                    or claim["command_id"] != str(body.command_id)
                    or claim["vehicle_id"] != vehicle_id or claim["fleet_instance_id"] != store.instance_id
                    or claim["status"] != "ACTIVE" or record["active_rental"] or record["active_charging_task"]):
                raise HTTPException(409, "STOP_OWNERSHIP_CHANGED")
            checkpoint = SimulatorCheckpoint.model_validate(saved["data"])
            physical = checkpoint.physical
            if (stop_checkpoint_evidence(checkpoint) != body.checkpoint_evidence
                    or physical.session_id != record["session_id"]
                    or str(physical.session_id) != owner["session_id"]
                    or checkpoint.simulation is None or physical.relocation_state == "ARRIVED"
                    or physical.charging_state == "CHARGING"):
                raise HTTPException(409, "STOP_CHECKPOINT_CHANGED")
            command = checkpoint.command
            if command is None:
                origin = VehicleClaim.model_validate(claim).origin
                if (physical.charging_state not in ("NOT_CHARGING", "COMPLETE")
                        or not idle_before_command(checkpoint, body.command_id, origin, record["telemetry"])):
                    raise HTTPException(409, "STOP_UNSTARTED_CHECKPOINT_REQUIRED")
            else:
                if command.dispatch_id != body.dispatch_id or command.command_id != body.command_id:
                    raise HTTPException(409, "STOP_COMMAND_CHANGED")
                if checkpoint.accepted is False and physical.position.model_dump() != claim["origin"]:
                    raise HTTPException(409, "STOP_ORIGIN_CHANGED")
            stopped = physical.relocation_state == "FAILED" or checkpoint.accepted is False
            if not stopped and owner["expires_at"] > store.clock.now():
                raise HTTPException(409, "SIMULATOR_OWNER_STILL_ACTIVE")
            # A stop can preserve a completed charge already saved before the claim.
            self.validate_checkpoint(vehicle_id, record, checkpoint, closing_unstarted=command is None)

            staged = deepcopy(record)
            now = store.clock.now()
            saved_physical = type(physical).model_validate({
                **physical.model_dump(),
                "active_dispatch_id": None, "distance_travelled_km": None,
                "relocation_state": "IDLE", "availability": "UNAVAILABLE", "speed_kph": 0,
                "power_kw": 0,
            })
            idle = SimulatorCheckpoint(vehicle_id=vehicle_id, fleet_instance_id=store.instance_id,
                physical=saved_physical, completed_command_id=body.command_id,
                simulation=checkpoint.simulation)
            revision = saved["revision"] + 1
            receipt = SimulatorCheckpointReceipt(request_id=body.request_id,
                generation=owner["generation"], checkpoint_revision=revision,
                command_fingerprint=None, durable=store.enabled)
            staged["simulator_checkpoint"] = {"revision": revision, "data": idle.model_dump(mode="json"),
                "request_id": str(body.request_id), "request_fingerprint": fingerprint(body),
                "receipt": receipt.model_dump(mode="json"), "charging": saved.get("charging")}
            staged["simulator_owner"]["expires_at"] = now
            staged["simulator_owner"]["revision"] += 1
            staged["simulator_restore"] = None
            stored_claim = staged["claims"][str(body.dispatch_id)]
            stored_claim["operator_stop"] = {"fingerprint": fingerprint(body), "checkpoint_revision": revision}
            stored_claim["receipt"].update(status="RELEASED", outcome="REJECTED", released_at=now.isoformat())
            staged.update(active_claim=None, state_version=record["state_version"] + 1,
                          control_version=record["control_version"] + 1)
            if physical.position.model_dump() != claim["origin"]:
                staged["current_zone_id"] = None
                staged["stranded_after_stop"] = True
            await self.commit(vehicle_id, staged)
            return VehicleClaim.model_validate(stored_claim["receipt"])

    async def rotate_owner(self, vehicle_id, record, body, checkpoint):
        """Caller holds Fleet lock and has validated the restoration evidence."""
        store = self.store
        staged = deepcopy(record)
        session_id, generation = uuid4(), body.expected_generation + 1
        physical = checkpoint.physical.model_copy(update={"session_id": session_id, "sequence": 0})
        checkpoint = checkpoint.model_copy(update={"physical": physical})
        if staged["session_request_id"]:
            staged["retired_requests"].add(staged["session_request_id"])
        staged.update(session_id=session_id, session_request_id=body.request_id)
        receipt = SimulatorSession(session_id=session_id, generation=generation, owner_revision=1,
            expires_at=store.clock.now() + timedelta(seconds=self.settings.lease_seconds),
            checkpoint_revision=body.expected_checkpoint_revision + 1, checkpoint=checkpoint, durable=store.enabled)
        staged["simulator_owner"] = {"generation": generation, "revision": 1,
            "session_id": str(session_id), "expires_at": receipt.expires_at,
            "request_id": str(body.request_id), "request_fingerprint": fingerprint(body),
            "receipt": receipt.model_dump(mode="json")}
        saved_receipt = SimulatorCheckpointReceipt(request_id=body.request_id, generation=generation,
            checkpoint_revision=receipt.checkpoint_revision,
            command_fingerprint=fingerprint(checkpoint.command) if checkpoint.command else None, durable=store.enabled)
        staged["simulator_checkpoint"] = {"revision": receipt.checkpoint_revision,
            "data": checkpoint.model_dump(mode="json"), "request_id": str(body.request_id),
            "request_fingerprint": fingerprint(body), "receipt": saved_receipt.model_dump(mode="json"), "charging": None}
        staged["simulator_restore"] = {"request_id": str(body.request_id), "fingerprint": fingerprint(body),
            "receipt": receipt.model_dump(mode="json")}
        staged["state_version"] += 1
        staged["control_version"] += 1
        # Leave telemetry/receive times untouched: only the restored node can report fresh state.
        await self.commit(vehicle_id, staged)
        return receipt

    def validate_checkpoint(self, vehicle_id, record, checkpoint, *, closing_unstarted=False):
        store, physical, command = self.store, checkpoint.physical, checkpoint.command
        if checkpoint.vehicle_id != vehicle_id or str(checkpoint.fleet_instance_id) != store.instance_id:
            raise HTTPException(409, "SIMULATOR_CHECKPOINT_IDENTITY_MISMATCH")
        configuration = checkpoint.simulation
        registration = record["registration"]
        if configuration is not None and (configuration.capacity_kwh != registration.battery_capacity_kwh
                or configuration.consumption_kwh_per_km != registration.consumption_kwh_per_km
                or configuration.time_scale != self.settings.simulation_time_scale):
            raise HTTPException(409, "SIMULATOR_CONFIGURATION_MISMATCH")
        if (physical.observed_at - store.clock.now()).total_seconds() > 5:
            raise HTTPException(422, "OBSERVATION_TIME_INVALID")
        if record["active_rental"] or record["active_charging_task"]:
            raise HTTPException(409, "SIMULATOR_TASK_CONFLICT")
        claim = record["active_claim"]
        if command is None:
            if claim is not None:
                saved = record.get("simulator_checkpoint")
                idle = SimulatorCheckpoint.model_validate(saved["data"]) if saved else None
                baseline = idle.physical if idle else None
                # A claim may arrive between idle publication and command poll.
                # Keep the owner online, but permit no motion/energy advancement.
                if (idle is None or idle.command is not None
                        or physical.position != baseline.position
                        or physical.soc_pct != baseline.soc_pct
                        or physical.odometer_km != baseline.odometer_km
                        or physical.speed_kph != 0
                        or physical.charging_state != "NOT_CHARGING" and not closing_unstarted):
                    raise HTTPException(409, "SIMULATOR_IDLE_CLAIM_CHANGED")
        else:
            entry = record["claims"].get(str(command.dispatch_id))
            receipt = entry["receipt"] if entry else None
            if (receipt is None or receipt["command_id"] != str(command.command_id)
                    or receipt["vehicle_id"] != vehicle_id or receipt["fleet_instance_id"] != store.instance_id
                    or receipt["target_zone_id"] != command.target_zone_id
                    or receipt["target_position"] != command.target_position.model_dump()
                    or claim is not None and claim != receipt):
                raise HTTPException(409, "SIMULATOR_CLAIM_MISMATCH")
            if receipt["status"] == "RELEASED":
                required_state = "ARRIVED" if receipt["outcome"] == "ARRIVED" else "IDLE"
                if physical.relocation_state != required_state:
                    raise HTTPException(409, "SIMULATOR_RELEASE_CONFLICT")
            elif claim is None:
                raise HTTPException(409, "SIMULATOR_CLAIM_MISMATCH")
            request = entry["request"]
            if (command.min_reserve_pct != request.min_reserve_pct or command.route_factor != request.route_factor
                    or command.energy_margin_factor != request.energy_margin_factor):
                raise HTTPException(409, "SIMULATOR_COMMAND_CHANGED")
            if checkpoint.accepted:
                route = command.route
                if (request.route_distance_km is None or abs(route.distance_km - request.route_distance_km) > 1e-9
                        or distance_km(route.geometry[0].model_dump(), receipt["origin"]) > .005
                        or distance_km(route.geometry[-1].model_dump(), receipt["target_position"]) > store.settings.arrival_tolerance_km):
                    raise HTTPException(409, "SIMULATOR_ROUTE_MISMATCH")
                self.validate_route_position(checkpoint)
        previous = record.get("simulator_checkpoint")
        old = SimulatorCheckpoint.model_validate(previous["data"]) if previous else None
        baseline = old.physical if old else record["telemetry"]
        if old and (physical.sequence < old.physical.sequence
                    or physical.sequence == old.physical.sequence and physical != old.physical):
            raise HTTPException(409, "SIMULATOR_CHECKPOINT_SEQUENCE_CONFLICT")
        if (old is None or old.command is None or command is not None) and checkpoint.completed_command_id != (
                old.completed_command_id if old else None):
            raise HTTPException(409, "SIMULATOR_COMPLETION_UNCONFIRMED")
        if baseline is not None:
            if physical.observed_at < baseline.observed_at:
                raise HTTPException(409, "OBSERVATION_OUT_OF_ORDER")
            if baseline.odometer_km is not None and (physical.odometer_km is None or physical.odometer_km < baseline.odometer_km):
                raise HTTPException(409, "SIMULATOR_ODOMETER_REGRESSION")
            if command is None and (old is None or old.command is None):
                if physical.position != baseline.position:
                    raise HTTPException(409, "SIMULATOR_IDLE_POSITION_CHANGED")
                if (physical.soc_pct < baseline.soc_pct or physical.soc_pct != baseline.soc_pct
                        and physical.charging_state not in ("CHARGING", "COMPLETE")):
                    raise HTTPException(409, "SIMULATOR_IDLE_SOC_CHANGED")
            if command is not None and not checkpoint.accepted and (
                    physical.position != baseline.position or physical.soc_pct != baseline.soc_pct
                    or physical.odometer_km != baseline.odometer_km):
                raise HTTPException(409, "SIMULATOR_REJECTED_COMMAND_MOVED")
        if old and old.command:
            if command is None:
                entry = record["claims"].get(str(old.command.dispatch_id))
                if (not entry or entry["receipt"]["status"] != "RELEASED"
                        or checkpoint.completed_command_id != old.command.command_id
                        or physical.position != old.physical.position or physical.soc_pct != old.physical.soc_pct):
                    raise HTTPException(409, "SIMULATOR_COMPLETION_UNCONFIRMED")
            elif command != old.command:
                raise HTTPException(409, "SIMULATOR_COMMAND_CHANGED")
            else:
                transitions = {"DISPATCHED": {"DISPATCHED", "RELOCATING", "ARRIVED", "FAILED"},
                               "RELOCATING": {"RELOCATING", "ARRIVED", "FAILED"},
                               "ARRIVED": {"ARRIVED"}, "FAILED": {"FAILED"}, "IDLE": {"IDLE"}}
                if (physical.relocation_state not in transitions[old.physical.relocation_state]
                        or physical.distance_travelled_km < old.physical.distance_travelled_km
                        or checkpoint.accepted != old.accepted or checkpoint.reason != old.reason
                        or checkpoint.next_event_sequence < old.next_event_sequence
                        or old.acknowledged and not checkpoint.acknowledged):
                    raise HTTPException(409, "SIMULATOR_EXECUTION_REGRESSION")
                if checkpoint.pending_event:
                    if old.pending_event and checkpoint.pending_event.sequence == old.pending_event.sequence:
                        if checkpoint.pending_event != old.pending_event:
                            raise HTTPException(409, "SIMULATOR_EVENT_CONFLICT")
                    elif checkpoint.pending_event.sequence < old.next_event_sequence:
                        raise HTTPException(409, "SIMULATOR_EVENT_CONFLICT")
        if command is not None and checkpoint.accepted and baseline is not None:
            prior_distance = old.physical.distance_travelled_km if old and old.command else 0
            registration = record["registration"]
            expected_soc = baseline.soc_pct - 100 * (physical.distance_travelled_km - prior_distance) * (
                registration.consumption_kwh_per_km / registration.battery_capacity_kwh)
            if abs(physical.soc_pct - expected_soc) > .001:
                raise HTTPException(409, "SIMULATOR_ENERGY_MISMATCH")

    @staticmethod
    def validate_route_position(checkpoint):
        route, physical = checkpoint.command.route, checkpoint.physical
        lengths = [distance_km(a.model_dump(), b.model_dump()) for a, b in zip(route.geometry, route.geometry[1:])]
        total = sum(lengths)
        if total <= 0 and route.distance_km > 0:
            raise HTTPException(409, "SIMULATOR_ROUTE_MISMATCH")
        remaining = total * min(1, physical.distance_travelled_km / route.distance_km) if route.distance_km else total
        position = route.geometry[-1].model_dump()
        for a, b, length in zip(route.geometry, route.geometry[1:], lengths):
            if remaining <= length and length > 0:
                ratio = remaining / length
                position = {key: getattr(a, key) + ratio * (getattr(b, key) - getattr(a, key))
                            for key in ("latitude", "longitude")}
                break
            remaining -= length
        if distance_km(physical.position.model_dump(), position) > .005:
            raise HTTPException(409, "SIMULATOR_POSITION_PROGRESS_MISMATCH")
        if physical.relocation_state == "ARRIVED" and abs(physical.distance_travelled_km - route.distance_km) > 1e-9:
            raise HTTPException(409, "SIMULATOR_ARRIVAL_PROGRESS_MISMATCH")

    def charging_context(self, vehicle_id, record, checkpoint,
                         evidence: ChargingEvidence | None):
        """Validate fresh external evidence against current locked Fleet state."""
        physical = checkpoint.physical
        if not self.needs_charging_evidence(checkpoint):
            return None
        if physical.parked_facility_id is None or physical.charging_power_kw is None:
            raise HTTPException(409, "SIMULATOR_CHARGING_CONTEXT_REQUIRED")
        if evidence is None:
            raise HTTPException(503, "SIMULATOR_CHARGING_EVIDENCE_UNAVAILABLE")
        age = self.store.clock.monotonic() - evidence.requested_tick
        if not 0 <= age < self.evidence_max_age:
            raise HTTPException(503, "SIMULATOR_CHARGING_EVIDENCE_EXPIRED")
        observation = evidence.observation
        parking = observation.parking
        site = parking.parking
        if (parking.vehicle_id != vehicle_id or parking.pending or site is None or not site.charging_allocated
                or site.facility_id != physical.parked_facility_id
                or site.charging_power_kw != physical.charging_power_kw
                or distance_km(physical.position.model_dump(), site.position.model_dump()) > self.store.settings.arrival_tolerance_km
                or physical.speed_kph not in (None, 0)
                or physical.charging_state == "CHARGING" and physical.availability != "UNAVAILABLE"):
            raise HTTPException(409, "SIMULATOR_CHARGING_CONTEXT_MISMATCH")
        previous = record.get("simulator_checkpoint")
        old = SimulatorCheckpoint.model_validate(previous["data"]) if previous else None
        baseline = old.physical if old else record["telemetry"]
        now = self.store.clock.now()
        proof = previous.get("charging") if previous else None
        if baseline is not None and physical.soc_pct > baseline.soc_pct:
            # Establish a verified, unchanged-SOC sample before drawing energy.
            # A restart changes the epoch, preventing charging downtime catch-up.
            if (proof is None or proof["epoch"] != self.charging_epoch
                    or proof["generation"] != record["simulator_owner"]["generation"]
                    or proof["session_id"] != record["simulator_owner"]["session_id"]
                    or proof["facility_id"] != site.facility_id or proof["power_kw"] != site.charging_power_kw
                    or proof["target_soc_pct"] != site.charging_target_soc_pct
                    or proof.get("allocation_id") != observation.allocation_id
                    or proof.get("facilities_instance_id") != str(observation.instance_id)
                    or baseline.charging_state != "CHARGING"):
                raise HTTPException(409, "SIMULATOR_CHARGING_BASELINE_REQUIRED")
            elapsed = max(0, min((physical.observed_at - baseline.observed_at).total_seconds(),
                                 (now - proof["verified_at"]).total_seconds()))
            capacity = record["registration"].battery_capacity_kwh
            limit = min(site.charging_target_soc_pct, baseline.soc_pct +
                        100 * site.charging_power_kw * elapsed * self.settings.simulation_time_scale / (3600 * capacity))
            if physical.soc_pct > limit + 1e-6:
                raise HTTPException(409, "SIMULATOR_CHARGING_ENERGY_MISMATCH")
        if ((physical.charging_state == "COMPLETE" and physical.soc_pct < site.charging_target_soc_pct)
                or (physical.charging_state == "CHARGING" and physical.soc_pct >= site.charging_target_soc_pct)):
            raise HTTPException(409, "SIMULATOR_CHARGING_STATE_MISMATCH")
        return {"epoch": self.charging_epoch, "facility_id": site.facility_id,
                "generation": record["simulator_owner"]["generation"],
                "session_id": record["simulator_owner"]["session_id"],
                "power_kw": site.charging_power_kw, "target_soc_pct": site.charging_target_soc_pct,
                "allocation_id": observation.allocation_id,
                "facilities_instance_id": str(observation.instance_id),
                "facilities_revision": observation.revision,
                "facilities_observed_at": observation.observed_at.isoformat(),
                "observation_id": str(observation.request_id),
                "verified_at": now}

    def checkpoint_record(self, vehicle_id, body):
        """Caller holds Fleet lock; admission is repeated after external I/O."""
        store = self.store
        self.gate(vehicle_id)
        record = store.record(vehicle_id)
        self.owner(record, body.session_id, body.generation, require_live=False)
        if body.checkpoint.physical.session_id != body.session_id:
            raise HTTPException(409, "SIMULATOR_OWNER_CONFLICT")
        previous = record.get("simulator_checkpoint")
        if previous and previous["request_id"] == str(body.request_id):
            if previous["request_fingerprint"] != fingerprint(body):
                raise HTTPException(409, "SIMULATOR_REQUEST_ID_CONFLICT")
            return record, SimulatorCheckpointReceipt.model_validate(previous["receipt"])
        self.owner(record, body.session_id, body.generation)
        if body.expected_checkpoint_revision != (previous["revision"] if previous else 0):
            raise HTTPException(409, "SIMULATOR_CHECKPOINT_REVISION_CONFLICT")
        self.validate_checkpoint(vehicle_id, record, body.checkpoint)
        return record, None

    async def checkpoint(self, vehicle_id, body):
        store = self.store
        async with store.lock:
            record, retry = self.checkpoint_record(vehicle_id, body)
            if retry:
                return retry
            if not self.needs_charging_evidence(body.checkpoint):
                return await self.commit_checkpoint(vehicle_id, record, body, None)
            saved = deepcopy(record)
        evidence = await self.read_charging_evidence(vehicle_id)
        async with store.lock:
            record, retry = self.checkpoint_record(vehicle_id, body)
            if retry:
                return retry
            if record != saved:
                raise HTTPException(503, "SIMULATOR_CHARGING_EVIDENCE_CHANGED")
            charging = self.charging_context(vehicle_id, record, body.checkpoint, evidence)
            return await self.commit_checkpoint(vehicle_id, record, body, charging)

    async def commit_checkpoint(self, vehicle_id, record, body, charging):
        """Caller holds Fleet lock; snapshot/history still use one writer."""
        store = self.store
        staged = deepcopy(record)
        sample = (store.stage_telemetry(vehicle_id, staged, body.checkpoint.physical)
                  if body.publish_telemetry else None)
        if sample is None:
            staged["state_version"] += 1
        receipt = SimulatorCheckpointReceipt(
            request_id=body.request_id, generation=body.generation,
            checkpoint_revision=body.expected_checkpoint_revision + 1,
            command_fingerprint=(fingerprint(body.checkpoint.command)
                                 if body.checkpoint.command else None),
            durable=store.enabled)
        staged["simulator_checkpoint"] = {"revision": receipt.checkpoint_revision,
            "data": body.checkpoint.model_dump(mode="json"), "request_id": str(body.request_id),
            "charging": charging,
            "request_fingerprint": fingerprint(body), "receipt": receipt.model_dump(mode="json")}
        await self.commit(vehicle_id, staged, sample)
        return receipt
