import asyncio
from uuid import uuid4
from datetime import timedelta
from copy import deepcopy
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.settings import DispatchSettings
from ev_fleet.common.travel import distance_km, predicted_soc
from ev_fleet.common.persistence import SnapshotRepository


class FleetStore:
    def __init__(self, settings=None, clock=None, repository=None):
        self.settings = settings or DispatchSettings.from_env()
        self.clock = clock or Clock()
        self.lock = asyncio.Lock()
        self.vehicles = {}
        self.instance_id = str(uuid4())
        self.version = 0
        self.repository = repository or SnapshotRepository.from_env("fleet")

    async def start(self):
        await self.repository.start()
        payload = await self.repository.load()
        if payload:
            self.instance_id = payload["instance_id"]
            self.version = payload["version"]
            self.vehicles = payload["vehicles"]
            now, tick = self.clock.now(), self.clock.monotonic()
            for record in self.vehicles.values():
                if record["telemetry"] is not None:
                    received_at = record["received_at"]
                    record["initial_age"] = max(0, (now - received_at).total_seconds())
                    record["received_tick"] = tick
        else:
            await self.persist()

    async def close(self):
        await self.repository.close()

    async def persist(self):
        await self.repository.save({"instance_id": self.instance_id,
                                    "version": self.version,
                                    "vehicles": self.vehicles})

    async def register(self, registration):
        async with self.lock:
            key = registration.vehicle_id
            if key in self.vehicles:
                if self.vehicles[key]["registration"] != registration:
                    raise HTTPException(409, "VEHICLE_REGISTRATION_CONFLICT")
                return self.view(self.vehicles[key])
            record = {"registration": registration, "telemetry": None,
                      "session_id": None, "session_request_id": None,
                      "retired_requests": set(), "state_version": 1,
                      "active_claim": None, "claims": {}, "current_zone_id": None,
                      "active_rental": None, "rentals": {},
                      "rental_start_requests": {}, "rental_return_requests": {}}
            self.vehicles[key] = record
            self.version += 1
            await self.persist()
            return self.view(record)

    def record(self, vehicle_id):
        if vehicle_id not in self.vehicles:
            raise HTTPException(404, "VEHICLE_NOT_FOUND")
        return self.vehicles[vehicle_id]

    async def session(self, vehicle_id, request_id):
        async with self.lock:
            record = self.record(vehicle_id)
            if request_id in record["retired_requests"]:
                raise HTTPException(409, "SESSION_REQUEST_SUPERSEDED")
            if request_id != record["session_request_id"]:
                if record["session_request_id"]:
                    record["retired_requests"].add(record["session_request_id"])
                record["session_request_id"] = request_id
                record["session_id"] = uuid4()
                # Preserve physical status, but require new-session telemetry for eligibility.
                record["state_version"] += 1
                self.version += 1
                await self.persist()
            return {"session_id": str(record["session_id"])}

    async def telemetry(self, vehicle_id, update):
        async with self.lock:
            record = self.record(vehicle_id)
            if update.session_id != record["session_id"]:
                raise HTTPException(409, "SESSION_INVALID")
            previous = record["telemetry"]
            if previous and previous.session_id == update.session_id:
                if update.sequence == previous.sequence and update == previous:
                    return {"accepted": False, "duplicate": True, "vehicle": self.view(record)}
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
            record.update(telemetry=update, received_at=now,
                          received_tick=self.clock.monotonic(), initial_age=max(0, age))
            if update.relocation_state == "RELOCATING" or record["active_rental"] is not None:
                record["current_zone_id"] = None
            record["state_version"] += 1
            self.version += 1
            await self.persist()
            return {"accepted": True, "duplicate": False, "vehicle": self.view(record)}

    def view(self, record):
        telemetry = record["telemetry"]
        age = None if telemetry is None else record["initial_age"] + max(
            0, self.clock.monotonic() - record["received_tick"])
        current_session = telemetry is not None and telemetry.session_id == record["session_id"]
        stale = not current_session or age >= self.settings.stale_seconds
        return {**record["registration"].model_dump(mode="json"),
                "state_version": record["state_version"],
                "telemetry": telemetry.model_dump(mode="json") if telemetry else None,
                "received_at": record["received_at"].isoformat() if telemetry else None,
                "telemetry_age_seconds": age, "stale": stale,
                "active_claim": deepcopy(record["active_claim"]),
                "active_rental": deepcopy(record["active_rental"]),
                "current_zone_id": record["current_zone_id"],
                "effective_relocation_state": ("DISPATCHED" if record["active_claim"] and (
                    not telemetry or telemetry.relocation_state == "IDLE") else
                    telemetry.relocation_state.value if telemetry else "IDLE"),
                "effective_availability": "AVAILABLE" if (
                    not stale and not record["active_claim"] and record["active_rental"] is None and telemetry
                    and telemetry.availability == "AVAILABLE" and telemetry.relocation_state == "IDLE"
                    and telemetry.charging_state != "CHARGING"
                    and telemetry.active_dispatch_id is None) else "UNAVAILABLE",
                "connectivity": "OFFLINE" if age is None or age >= self.settings.offline_seconds else "ONLINE"}

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
            if record["state_version"] != body.expected_version:
                raise HTTPException(409, "VEHICLE_VERSION_CONFLICT")
            state = self.view(record)
            if state["effective_availability"] != "AVAILABLE":
                raise HTTPException(409, "VEHICLE_NOT_ELIGIBLE")
            telemetry = record["telemetry"]
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
            self.version += 1
            await self.persist()
            return deepcopy(receipt)

    async def get(self, vehicle_id):
        async with self.lock:
            return self.view(self.record(vehicle_id))

    async def snapshot(self):
        async with self.lock:
            return {"instance_id": self.instance_id, "snapshot_version": self.version,
                    "snapshot_at": self.clock.now().isoformat(),
                    "vehicles": [self.view(record) for record in self.vehicles.values()]}
