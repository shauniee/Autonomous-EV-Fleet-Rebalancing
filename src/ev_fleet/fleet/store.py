import asyncio
from uuid import uuid4
from datetime import timedelta
from copy import deepcopy
from collections import deque
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.settings import DispatchSettings
from ev_fleet.common.travel import distance_km, predicted_soc
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.vehicles import SimulatorRuntimeReport
from .recovery import SimulatorRecovery


class FleetStore:
    def __init__(self, settings=None, clock=None, repository=None, recovery_settings=None):
        self.settings = settings or DispatchSettings.from_env()
        self.clock = clock or Clock()
        self.lock = asyncio.Lock()
        self.vehicles = {}
        self.charging_tasks = {}
        self.instance_id = str(uuid4())
        self.version = 0
        self.repository = repository or SnapshotRepository.from_env("fleet")
        self.history_cache = {}  # bounded ephemeral-only cache; PostgreSQL is durable
        self.runtime_reports = {}  # Diagnostic only; never part of a durable snapshot.
        self.persistence_uncertain = False
        self._committed_state = (self.instance_id, self.version, {}, {})
        self.simulator_recovery = SimulatorRecovery(self, recovery_settings)

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if payload:
            self.instance_id = payload["instance_id"]
            self.version = payload["version"]
            self.vehicles = payload["vehicles"]
            for record in self.vehicles.values():
                record.setdefault("control_version", record["state_version"])
                record.setdefault("simulator_owner", None)
                record.setdefault("simulator_checkpoint", None)
                record.setdefault("active_charging_task", None)
            self.charging_tasks = payload.get("charging_tasks", {})
            now, tick = self.clock.now(), self.clock.monotonic()
            for record in self.vehicles.values():
                if record["telemetry"] is not None:
                    received_at = record["received_at"]
                    record["initial_age"] = max(0, (now - received_at).total_seconds())
                    record["received_tick"] = tick
            self._publish_committed_state()
        else:
            await self.persist()

    async def close(self):
        try:
            await self.simulator_recovery.close()
        finally:
            await self.repository.close()

    @property
    def enabled(self):
        return self.repository.enabled

    @property
    def ready(self):
        return self.repository.ready and not self.persistence_uncertain

    @property
    def faulted(self):
        return self.persistence_uncertain or getattr(self.repository, "faulted", False)

    def ensure_consistent(self):
        if self.faulted:
            raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN")

    def mark_persistence_uncertain(self):
        self.persistence_uncertain = True
        if isinstance(self.repository, SnapshotRepository):
            self.repository.mark_uncertain()

    async def persist(self, telemetry_sample=None):
        self.ensure_consistent()
        payload = {"instance_id": self.instance_id, "version": self.version,
                   "vehicles": self.vehicles, "charging_tasks": self.charging_tasks}
        try:
            if telemetry_sample is None:
                await self.repository.save(payload)
            else:
                await self.repository.save(payload, telemetry_sample=telemetry_sample)
            self._publish_committed_state()
        except asyncio.CancelledError:
            self.mark_persistence_uncertain()
            raise
        except Exception as exc:
            self.mark_persistence_uncertain()
            raise HTTPException(503, "PERSISTENCE_STATE_UNCERTAIN") from exc

    def _publish_committed_state(self):
        self._committed_state = (
            self.instance_id,
            self.version,
            deepcopy(self.vehicles),
            deepcopy(self.charging_tasks),
        )

    async def register(self, registration):
        async with self.lock:
            self.ensure_consistent()
            key = registration.vehicle_id
            if key in self.vehicles:
                if self.vehicles[key]["registration"] != registration:
                    raise HTTPException(409, "VEHICLE_REGISTRATION_CONFLICT")
                return self.view(self.vehicles[key])
            record = {"registration": registration, "telemetry": None,
                      "session_id": None, "session_request_id": None,
                      "retired_requests": set(), "state_version": 1,
                      "control_version": 1,
                      "active_claim": None, "claims": {}, "current_zone_id": None,
                      "active_rental": None, "active_charging_task": None, "rentals": {},
                      "rental_start_requests": {}, "rental_return_requests": {},
                      "simulator_owner": None, "simulator_checkpoint": None}
            self.vehicles[key] = record
            self.version += 1
            await self.persist()
            return self.view(record)

    def record(self, vehicle_id):
        self.ensure_consistent()
        if vehicle_id not in self.vehicles:
            raise HTTPException(404, "VEHICLE_NOT_FOUND")
        return self.vehicles[vehicle_id]

    async def session(self, vehicle_id, request_id):
        async with self.lock:
            record = self.record(vehicle_id)
            if self.simulator_recovery.guarded(vehicle_id, record):
                raise HTTPException(409, "SIMULATOR_SESSION_REQUIRED")
            if request_id in record["retired_requests"]:
                raise HTTPException(409, "SESSION_REQUEST_SUPERSEDED")
            if request_id != record["session_request_id"]:
                if record["session_request_id"]:
                    record["retired_requests"].add(record["session_request_id"])
                record["session_request_id"] = request_id
                record["session_id"] = uuid4()
                # Preserve physical status, but require new-session telemetry for eligibility.
                record["state_version"] += 1
                record["control_version"] += 1
                self.version += 1
                await self.persist()
            return {"session_id": str(record["session_id"])}

    async def telemetry(self, vehicle_id, update):
        async with self.lock:
            record = self.record(vehicle_id)
            if self.simulator_recovery.guarded(vehicle_id, record):
                raise HTTPException(409, "SIMULATOR_CHECKPOINT_REQUIRED")
            sample = self.stage_telemetry(vehicle_id, record, update)
            if sample is None:
                return {"accepted": False, "duplicate": True, "vehicle": self.view(record)}
            self.version += 1
            await self.persist(telemetry_sample=sample)
            self.cache_telemetry(sample)
            return {"accepted": True, "duplicate": False, "vehicle": self.view(record)}

    def stage_telemetry(self, vehicle_id, record, update):
        """Validate/apply to the supplied record; caller owns the lock and commit."""
        if update.session_id != record["session_id"]:
            raise HTTPException(409, "SESSION_INVALID")
        previous = record["telemetry"]
        if previous and previous.session_id == update.session_id:
            if update.sequence == previous.sequence and update == previous:
                return None
            if update.sequence <= previous.sequence:
                raise HTTPException(409, "SEQUENCE_CONFLICT")
            if update.observed_at < previous.observed_at:
                raise HTTPException(409, "OBSERVATION_OUT_OF_ORDER")
        now = self.clock.now()
        age = (now - update.observed_at).total_seconds()
        if age < -5 or age >= self.settings.stale_seconds:
            raise HTTPException(422, "OBSERVATION_TIME_INVALID")
        # Reject impossible relocation telemetry BEFORE mutating the vehicle record.
        if record["active_rental"] is not None and (update.relocation_state != "IDLE" or update.active_dispatch_id is not None):
            raise HTTPException(409, "RENTED_RELOCATION_CONFLICT")
        # Continuous speed/SOC/GPS reports do not invalidate a pending
        # operator approval. Control transitions still advance the separate
        # compare-and-swap epoch and claims recheck current physical data.
        changed_control = previous is None or any(
            getattr(previous, field) != getattr(update, field) for field in (
                "availability", "relocation_state", "active_dispatch_id",
                "charging_state", "parked_facility_id"))
        record.update(telemetry=update, received_at=now,
                      received_tick=self.clock.monotonic(), initial_age=max(0, age))
        if update.relocation_state == "RELOCATING" or record["active_rental"] is not None:
            record["current_zone_id"] = None
        record["state_version"] += 1
        if changed_control:
            record["control_version"] += 1
        sample = {"vehicle_id": vehicle_id, "session_id": update.session_id,
                  "sequence": update.sequence, "observed_at": update.observed_at,
                  "received_at": now, "telemetry": update.model_dump(mode="json")}
        return sample

    def cache_telemetry(self, sample):
        if not self.enabled:
            self.history_cache.setdefault(sample["vehicle_id"], deque(maxlen=500)).append({
                **{k: v for k, v in sample.items() if k != "telemetry"},
                "session_id": str(sample["session_id"]),
                "observed_at": sample["observed_at"].isoformat(),
                "received_at": sample["received_at"].isoformat(),
                "telemetry": deepcopy(sample["telemetry"])})

    async def telemetry_history(self, vehicle_id, limit=100):
        async with self.lock:
            self.record(vehicle_id)
            if getattr(self.repository, "enabled", False):
                samples = await self.repository.telemetry_history(vehicle_id, limit)
            else:
                samples = list(self.history_cache.get(vehicle_id, []))[-limit:]
        return {"vehicle_id": vehicle_id, "samples": deepcopy(samples),
                "storage": "postgresql" if getattr(self.repository, "enabled", False) else "ephemeral",
                "limit": limit}

    async def report_runtime(self, vehicle_id: str, report: SimulatorRuntimeReport):
        # No external waits, writer lock, persistence or physical metadata changes.
        # This also remains responsive while a snapshot transaction is in flight.
        record = self.record(vehicle_id)
        if report.vehicle_id != vehicle_id:
            raise HTTPException(409, "SIMULATOR_RUNTIME_IDENTITY_MISMATCH")
        owner = record.get("simulator_owner")
        if owner is not None:
            matches = (str(report.session_id) == owner["session_id"]
                       and report.generation == owner["generation"])
            if (not matches and (report.session_id is not None
                                 or owner["expires_at"] > self.clock.now())):
                raise HTTPException(409, "SIMULATOR_RUNTIME_OWNER_MISMATCH")
        previous = self.runtime_reports.get(vehicle_id)
        if (previous and previous["report"].process_id == report.process_id
                and report.sequence <= previous["report"].sequence):
            raise HTTPException(409, "SIMULATOR_RUNTIME_SEQUENCE_CONFLICT")
        self.runtime_reports[vehicle_id] = {
            "report": report, "received_tick": self.clock.monotonic(),
            "reported_at": self.clock.now()}
        return {"accepted": True}

    def runtime_view(self, record):
        entry = self.runtime_reports.get(record["registration"].vehicle_id)
        if entry is None:
            return None
        report = entry["report"]
        age = max(0, self.clock.monotonic() - entry["received_tick"])
        owner = record.get("simulator_owner")
        obsolete = owner is not None and owner["expires_at"] > self.clock.now() and (
            str(report.session_id) != owner["session_id"]
            or report.generation != owner["generation"])
        heartbeat_age = (None if report.heartbeat_age_seconds is None
                         else report.heartbeat_age_seconds + age)
        live = bool(report.live and report.initialized and heartbeat_age is not None
                    and heartbeat_age < report.heartbeat_timeout_seconds)
        status = "STALE" if age >= 15 or obsolete else "RESPONSIVE" if live else "UNRESPONSIVE"
        return {**report.model_dump(mode="json"), "status": status,
                "live": status == "RESPONSIVE",
                "ready": status == "RESPONSIVE" and report.ready and report.mode in (
                    "RUNNING", "RETRYING", "RECOVERY_WAIT"),
                "reported_at": entry["reported_at"], "report_age_seconds": age,
                "heartbeat_age_seconds": heartbeat_age}

    def view(self, record, *, include_runtime=False, charging_tasks=None):
        telemetry = record["telemetry"]
        age = None if telemetry is None else record["initial_age"] + max(
            0, self.clock.monotonic() - record["received_tick"])
        current_session = telemetry is not None and telemetry.session_id == record["session_id"]
        owner = record.get("simulator_owner")
        guarded = self.simulator_recovery.guarded(record["registration"].vehicle_id, record)
        configured = (self.simulator_recovery.settings.enabled
                      and record["registration"].vehicle_id in self.simulator_recovery.settings.vehicle_ids)
        lease_active = not guarded or (configured and owner is not None and owner["expires_at"] > self.clock.now())
        runtime = self.runtime_view(record)
        # Absence is unknown, allowing older fenced publishers during a rolling
        # upgrade. An observed unhealthy/recovering runtime still vetoes dispatch.
        runtime_available = (runtime is None or (
            runtime["live"] and runtime["ready"] and runtime["mode"] == "RUNNING"))
        stale = not current_session or age >= self.settings.stale_seconds or not lease_active or self.persistence_uncertain
        checkpoint = record.get("simulator_checkpoint")
        recovery = None if owner is None else {
            "generation": owner["generation"], "owner_revision": owner["revision"],
            "expires_at": owner["expires_at"],
            "status": "BLOCKED" if self.persistence_uncertain or not configured else "ACTIVE" if lease_active else "EXPIRED",
            "checkpoint_revision": checkpoint["revision"] if checkpoint else 0,
            "checkpoint_at": checkpoint["data"]["physical"]["observed_at"] if checkpoint else None,
            "durable": self.enabled}
        return {**record["registration"].model_dump(mode="json"),
                "state_version": record["state_version"],
                "control_version": record.get("control_version", record["state_version"]),
                "simulator_recovery": recovery,
                **({"simulator_runtime": runtime} if include_runtime else {}),
                "telemetry": telemetry.model_dump(mode="json") if telemetry else None,
                "received_at": record["received_at"].isoformat() if telemetry else None,
                "telemetry_age_seconds": age, "stale": stale,
                "active_claim": deepcopy(record["active_claim"]),
                "active_rental": deepcopy(record["active_rental"]),
                "active_charging_task": self._public_task(
                    record.get("active_charging_task"), charging_tasks),
                "current_zone_id": record["current_zone_id"],
                "effective_relocation_state": ("DISPATCHED" if record["active_claim"] and (
                    not telemetry or telemetry.relocation_state == "IDLE") else
                    telemetry.relocation_state.value if telemetry else "IDLE"),
                "effective_availability": "AVAILABLE" if (
                    not stale and runtime_available and not record["active_claim"] and record["active_rental"] is None
                    and record.get("active_charging_task") is None and telemetry
                    and (not record.get("stranded_after_stop")
                         or telemetry.parked_facility_id and telemetry.soc_pct > 0)
                    and telemetry.availability == "AVAILABLE" and telemetry.relocation_state == "IDLE"
                    and telemetry.charging_state != "CHARGING"
                    and telemetry.active_dispatch_id is None) else "UNAVAILABLE",
                "connectivity": "OFFLINE" if age is None or age >= self.settings.offline_seconds else "ONLINE"}


    def _public_task(self, task_id, tasks=None):
        task = (self.charging_tasks if tasks is None else tasks).get(task_id)
        return None if task is None else {k: deepcopy(v) for k, v in task.items() if k != "requests"}

    async def list_charging_tasks(self):
        async with self.lock:
            self.ensure_consistent()
            return {"tasks": [self._public_task(key) for key in sorted(self.charging_tasks)]}

    async def create_charging_task(self, body):
        """The dispatcher queues a low-SOC EV; no real charger is operated."""
        async with self.lock:
            self.ensure_consistent()
            for task in self.charging_tasks.values():
                if task["create_request_id"] == str(body.request_id):
                    if task["vehicle_id"] != body.vehicle_id:
                        raise HTTPException(409, "CHARGING_REQUEST_CONFLICT")
                    return self._public_task(task["task_id"])
            record = self.record(body.vehicle_id)
            state = self.view(record)
            telemetry = record["telemetry"]
            if body.expected_version != record["state_version"]:
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            if record.get("active_charging_task") or record["active_rental"] or record["active_claim"]:
                raise HTTPException(409, "VEHICLE_BUSY")
            if (state["stale"] or state["connectivity"] != "ONLINE" or not telemetry
                    or telemetry.soc_pct > 30 or telemetry.relocation_state != "IDLE"):
                raise HTTPException(409, "NOT_ELIGIBLE_FOR_CHARGING_TASK")
            key = str(uuid4())
            now = self.clock.now().isoformat()
            self.charging_tasks[key] = {
                "task_id": key, "vehicle_id": body.vehicle_id,
                "status": "PENDING", "soc_at_creation": telemetry.soc_pct,
                "created_at": now, "updated_at": now, "assigned_staff": None,
                "create_request_id": str(body.request_id), "requests": {}}
            record["active_charging_task"] = key
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            await self.persist()
            return self._public_task(key)

    async def update_charging_task(self, task_id, body):
        """Progress is approved manually; completion requires actual fresh SOC evidence."""
        async with self.lock:
            self.ensure_consistent()
            task = self.charging_tasks.get(str(task_id))
            if task is None:
                raise HTTPException(404, "CHARGING_TASK_NOT_FOUND")
            request_key = str(body.request_id)
            if request_key in task["requests"]:
                if task["requests"][request_key]["body"] != body:
                    raise HTTPException(409, "CHARGING_REQUEST_CONFLICT")
                return deepcopy(task["requests"][request_key]["receipt"])
            record = self.record(task["vehicle_id"])
            if body.expected_version != record["state_version"]:
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            status = task["status"]
            if body.action == "ASSIGN" and status == "PENDING" and body.staff_name:
                task["status"], task["assigned_staff"] = "ASSIGNED", body.staff_name
            elif body.action == "START_WORK" and status == "ASSIGNED":
                task["status"] = "IN_PROGRESS"
            elif body.action == "CANCEL" and status in ("PENDING", "ASSIGNED"):
                task["status"] = "CANCELLED"
                record["active_charging_task"] = None
            elif body.action == "COMPLETE" and status == "IN_PROGRESS":
                state = self.view(record)
                telemetry = record["telemetry"]
                if (not telemetry or state["stale"] or state["connectivity"] != "ONLINE"
                        or telemetry.charging_state != "COMPLETE" or telemetry.soc_pct < 50):
                    raise HTTPException(409, "CHARGING_EVIDENCE_REQUIRED")
                task["status"] = "COMPLETED"
                record["active_charging_task"] = None
            else:
                raise HTTPException(409, "INVALID_CHARGING_TRANSITION")
            task["updated_at"] = self.clock.now().isoformat()
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            receipt = self._public_task(str(task_id))
            task["requests"][request_key] = {"body": body, "receipt": deepcopy(receipt)}
            await self.persist()
            return receipt

    async def start_rental(self, vehicle_id, body):
        """Start a simulated free-floating rental. A rental blocks both ranking and claims."""
        key = str(body.rental_id)
        async with self.lock:
            record = self.record(vehicle_id)
            previous = record["rentals"].get(key)
            if previous:
                if previous["start_request"] != body:
                    raise HTTPException(409, "RENTAL_ID_CONFLICT")
                return deepcopy(previous["start_receipt"])
            if str(body.request_id) in record["rental_start_requests"]:
                raise HTTPException(409, "RENTAL_REQUEST_ID_CONFLICT")
            if record["active_rental"] is not None:
                raise HTTPException(409, "RENTAL_ALREADY_ACTIVE")
            if record["state_version"] != body.expected_version:
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            if self.view(record)["effective_availability"] != "AVAILABLE":
                raise HTTPException(409, "VEHICLE_NOT_ELIGIBLE")
            now = self.clock.now()
            if body.expected_end_at <= now or body.expected_end_at > now + timedelta(days=30):
                raise HTTPException(422, "RENTAL_END_TIME_INVALID")
            telemetry = record["telemetry"]
            data = {"rental_id": key, "status": "RENTED",
                    "expected_end_at": body.expected_end_at.isoformat(),
                     "expected_return_zone_id": body.expected_return_zone_id,
                    "started_at": now.isoformat(),
                    "start_position": telemetry.position.model_dump(mode="json"),
                    "start_soc_pct": telemetry.soc_pct,
                    "returned_at": None, "return_position": None,
                    "return_soc_pct": None, "return_observed_at": None}
            record["rentals"][key] = {
                "start_request": body,
                "start_receipt": deepcopy(data),
                "data": data,
                "start_session_id": str(telemetry.session_id),
                "start_sequence": telemetry.sequence,
                "return_request": None,
                "return_receipt": None,
            }
            record["active_rental"] = deepcopy(data)
            record["rental_start_requests"][str(body.request_id)] = key
            record["current_zone_id"] = None  # A free-floating customer controls the return position.
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            await self.persist()
            return deepcopy(data)

    async def return_rental(self, vehicle_id, rental_id, body):
        """Confirm actual return using new, fresh vehicle telemetry, not a guessed destination."""
        key = str(rental_id)
        async with self.lock:
            record = self.record(vehicle_id)
            entry = record["rentals"].get(key)
            if entry is None:
                raise HTTPException(404, "RENTAL_NOT_FOUND")
            if entry["return_request"] is not None:
                if entry["return_request"] != body:
                    raise HTTPException(409, "RENTAL_RETURN_CONFLICT")
                return deepcopy(entry["return_receipt"])
            if str(body.request_id) in record["rental_return_requests"]:
                raise HTTPException(409, "RENTAL_RETURN_REQUEST_ID_CONFLICT")
            if record["active_rental"] is None or record["active_rental"]["rental_id"] != key:
                raise HTTPException(409, "RENTAL_NOT_ACTIVE")
            if record["state_version"] != body.expected_version:
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            state = self.view(record)
            telemetry = record["telemetry"]
            if state["stale"] or state["connectivity"] != "ONLINE" or not telemetry:
                raise HTTPException(409, "RETURN_TELEMETRY_STALE")
            if (str(telemetry.session_id) == entry["start_session_id"]
                    and telemetry.sequence <= entry["start_sequence"]):
                raise HTTPException(409, "RETURN_TELEMETRY_REQUIRED")
            if telemetry.relocation_state != "IDLE" or telemetry.active_dispatch_id or record["active_claim"]:
                raise HTTPException(409, "VEHICLE_BUSY")
            data = entry["data"]
            data.update(status="RETURNED",
                        returned_at=self.clock.now().isoformat(),
                        return_position=telemetry.position.model_dump(mode="json"),
                        return_soc_pct=telemetry.soc_pct,
                        return_observed_at=telemetry.observed_at.isoformat())
            entry["return_request"] = body
            entry["return_receipt"] = deepcopy(data)
            record["rental_return_requests"][str(body.request_id)] = key
            record["active_rental"] = None
            record["current_zone_id"] = None
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            await self.persist()
            return deepcopy(data)

    async def get_rental(self, vehicle_id, rental_id):
        async with self.lock:
            entry = self.record(vehicle_id)["rentals"].get(str(rental_id))
            if not entry:
                raise HTTPException(404, "RENTAL_NOT_FOUND")
            return deepcopy(entry["data"])

    async def claim(self, vehicle_id, dispatch_id, body):
        key = str(dispatch_id)
        async with self.lock:
            record = self.record(vehicle_id)
            if str(body.expected_instance_id) != self.instance_id:
                raise HTTPException(409, "FLEET_INSTANCE_CHANGED")
            previous = record["claims"].get(key)
            if previous:
                if previous["request"] != body:
                    raise HTTPException(409, "CLAIM_ID_CONFLICT")
                return deepcopy(previous["receipt"])
            if body.approve_before <= self.clock.now():
                raise HTTPException(409, "APPROVAL_EXPIRED")
            if (record["control_version"] != body.expected_control_version
                    if body.expected_control_version is not None
                    else record["state_version"] != body.expected_version):
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            state = self.view(record)
            if state["effective_availability"] != "AVAILABLE":
                raise HTTPException(409, "VEHICLE_NOT_ELIGIBLE")
            telemetry = record["telemetry"]
            if (body.route_origin is not None and
                    distance_km(telemetry.position.model_dump(), body.route_origin.model_dump()) > .005):
                raise HTTPException(409, "ROUTE_ORIGIN_CHANGED")
            arrival = predicted_soc(telemetry.position.model_dump(), body.target_position.model_dump(),
                                    telemetry.soc_pct, record["registration"].battery_capacity_kwh,
                                    record["registration"].consumption_kwh_per_km,
                                    max(body.route_factor, self.settings.route_factor),
                                    max(body.energy_margin_factor, self.settings.energy_margin_factor))
            if body.route_distance_km is not None:
                road_arrival = telemetry.soc_pct - 100 * body.route_distance_km * (
                    record["registration"].consumption_kwh_per_km * max(body.energy_margin_factor, self.settings.energy_margin_factor)
                    / record["registration"].battery_capacity_kwh)
                arrival = min(arrival, road_arrival)
            if arrival < max(body.min_reserve_pct, self.settings.min_reserve_pct):
                raise HTTPException(409, "BELOW_OPERATIONAL_RESERVE")
            receipt = {"dispatch_id": key, "command_id": str(body.command_id),
                       "fleet_instance_id": self.instance_id, "vehicle_id": vehicle_id,
                       "target_zone_id": body.target_zone_id,
                       "target_position": body.target_position.model_dump(),
                       "origin": telemetry.position.model_dump(),
                       "acquired_at": self.clock.now().isoformat(), "status": "ACTIVE",
                       "outcome": None, "released_at": None}
            record["claims"][key] = {"request": body, "receipt": receipt}
            record["active_claim"] = receipt
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            await self.persist()
            return deepcopy(receipt)

    async def get_claim(self, vehicle_id, dispatch_id):
        async with self.lock:
            entry = self.record(vehicle_id)["claims"].get(str(dispatch_id))
            if not entry:
                raise HTTPException(404, "CLAIM_NOT_FOUND")
            return deepcopy(entry["receipt"])

    async def release(self, vehicle_id, dispatch_id, body):
        async with self.lock:
            record = self.record(vehicle_id)
            entry = record["claims"].get(str(dispatch_id))
            if not entry:
                raise HTTPException(404, "CLAIM_NOT_FOUND")
            receipt = entry["receipt"]
            if receipt["command_id"] != str(body.command_id):
                raise HTTPException(409, "COMMAND_ID_CONFLICT")
            if receipt["status"] == "RELEASED":
                if receipt["outcome"] != body.outcome:
                    raise HTTPException(409, "RELEASE_OUTCOME_CONFLICT")
                return deepcopy(receipt)
            state = self.view(record)
            telemetry = record["telemetry"]
            if state["stale"] or not telemetry or str(telemetry.active_dispatch_id) != str(dispatch_id):
                raise HTTPException(409, "COMPLETION_TELEMETRY_PENDING")
            required_state = "ARRIVED" if body.outcome == "ARRIVED" else "IDLE"
            target = receipt["target_position"] if body.outcome == "ARRIVED" else receipt["origin"]
            tolerance = getattr(self.settings, "arrival_tolerance_km", .05)
            if telemetry.relocation_state != required_state or distance_km(telemetry.position.model_dump(), target) > tolerance:
                raise HTTPException(409, "COMPLETION_NOT_CONFIRMED")
            # A confirmed physical arrival may have low SOC: record it, don't fabricate availability.
            receipt.update(status="RELEASED", outcome=body.outcome, released_at=self.clock.now().isoformat())
            record["active_claim"] = None
            if body.outcome == "ARRIVED":
                record["current_zone_id"] = receipt["target_zone_id"]
            record["state_version"] += 1
            record["control_version"] += 1
            self.version += 1
            await self.persist()
            return deepcopy(receipt)

    async def get(self, vehicle_id):
        async with self.lock:
            return self.view(self.record(vehicle_id))

    @staticmethod
    def active_snapshot_vehicle(state):
        """Archive stopped customer-demo identities without deleting their history."""
        return not (
            state["vehicle_id"].startswith("customer-ev-")
            and state["connectivity"] == "OFFLINE"
            and state["active_claim"] is None
            and state["active_rental"] is None
            and state["active_charging_task"] is None
            and state["effective_relocation_state"] == "IDLE"
        )

    async def snapshot(self, *, include_runtime=False):
        self.ensure_consistent()
        if self.lock.locked():
            instance_id, version, records, charging_tasks = self._committed_state
        else:
            instance_id, version = self.instance_id, self.version
            records, charging_tasks = self.vehicles, self.charging_tasks
        vehicles = [self.view(record, include_runtime=include_runtime,
                              charging_tasks=charging_tasks)
                    for record in records.values()]
        return {"instance_id": instance_id, "snapshot_version": version,
                "snapshot_at": self.clock.now().isoformat(),
                "vehicles": [state for state in vehicles
                             if self.active_snapshot_vehicle(state)]}
