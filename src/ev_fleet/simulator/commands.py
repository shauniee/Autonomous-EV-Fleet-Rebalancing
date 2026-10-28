import asyncio
import logging
import random
import time
from contextlib import suppress
from uuid import uuid4
import httpx
from ev_fleet.contracts.common import utcnow
from ev_fleet.contracts.dispatch import RelocationCommand
from ev_fleet.contracts.facilities import VehicleParking
from .movement import Movement


class MovingNode:
    def __init__(self, fleet_url, dispatch_url, vehicle_id, position, soc_pct,
                 telemetry_interval=5, poll_interval=1, tick_seconds=.2,
                 speed_kph=30, time_scale=30, client=None, facilities_url=None,
                 road_match_initial=True):
        if min(telemetry_interval, poll_interval, tick_seconds, speed_kph, time_scale) <= 0:
            raise ValueError("simulation intervals and speeds must be positive")
        self.fleet_url, self.dispatch_url, self.vehicle_id = fleet_url, dispatch_url, vehicle_id
        self.telemetry_interval, self.poll_interval = telemetry_interval, poll_interval
        self.tick_seconds, self.speed_kph, self.time_scale = tick_seconds, speed_kph, time_scale
        self.movement = Movement(position, soc_pct)
        self.client = client
        self.session = None
        self.session_request = str(uuid4())
        self.sequence = 0
        self.event_sequence = 0
        self.acknowledged = False
        self.initialized = False
        self.ready = asyncio.Event()
        self.facilities_url = facilities_url
        self.road_match_initial = road_match_initial
        self.parking_verified = not bool(facilities_url)
        self.telemetry_changed = asyncio.Event()

    async def request(self, method, url, body=None):
        response = await self.client.request(method, url, json=body)
        response.raise_for_status()
        return response.json()

    async def register(self):
        state = await self.request("POST", self.fleet_url + "/api/v1/vehicles", {
            "vehicle_id": self.vehicle_id, "battery_capacity_kwh": self.movement.capacity,
            "consumption_kwh_per_km": self.movement.consumption})
        # A new process cannot safely reconstruct an in-flight journey from CLI defaults.
        previous = state.get("telemetry")
        if not self.initialized and (state.get("active_claim") or state.get("active_rental") or state.get("active_charging_task") or previous and (
            previous.get("active_dispatch_id") or previous["relocation_state"] != "IDLE")):
            raise RuntimeError("Vehicle has an unresolved journey; refusing to overwrite its physical state")
        if not self.initialized and previous:
            # Resume the actual idle position/SOC; never reconstruct a journey.
            self.movement.position = previous["position"]
            self.movement.soc_pct = previous["soc_pct"]
        if not self.initialized and self.road_match_initial:
            # In OneMap mode, place an IDLE simulated EV on a routable road
            # before its first telemetry and before any journey can be claimed.
            # This is a one-time initialisation, not an in-flight teleport.
            match = await self.request("POST", f"{self.dispatch_url}/api/v1/road-matches",
                                       self.movement.position)
            if (match.get("source") not in ("ONEMAP", "SIMULATED")
                    or not isinstance(match.get("position"), dict)):
                raise RuntimeError("Road matching returned an invalid position")
            self.movement.position = match["position"]
            self.movement.origin = match["position"].copy()
        session = await self.request("POST", f"{self.fleet_url}/api/v1/vehicles/{self.vehicle_id}/telemetry-sessions",
                                     {"request_id": self.session_request})
        self.session = session["session_id"]
        self.sequence = 0
        self.initialized = True
        if self.facilities_url:
            await self.refresh_parking()

    async def refresh_parking(self):
        self.parking_verified = False
        self.movement.park(None)
        state = VehicleParking.model_validate(await self.request("GET",
            f"{self.facilities_url}/api/v1/vehicles/{self.vehicle_id}/parking")).model_dump(mode="json")
        if state["vehicle_id"] != self.vehicle_id:
            raise RuntimeError("Parking response belongs to another vehicle")
        if state["pending"]:
            return
        if state["parking"]:
            self.parking_verified = self.movement.park(state["parking"])
        else:
            self.parking_verified = True

    async def send_telemetry(self):
        if self.session is None:
            await self.register()
        sequence = self.sequence
        self.sequence += 1  # A lost response must never reuse this number with a different payload.
        try:
            physical = self.movement.telemetry()
            if self.facilities_url and not self.parking_verified:
                physical["availability"] = "UNAVAILABLE"
            await self.request("POST", f"{self.fleet_url}/api/v1/vehicles/{self.vehicle_id}/telemetry", {
                "session_id": self.session, "sequence": sequence, "observed_at": utcnow().isoformat(),
                **physical})
            self.ready.set()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                self.session = None
                self.session_request = str(uuid4())
                self.ready.clear()
            raise

    async def command_step(self):
        await self.ready.wait()
        if self.movement.command is None:
            if self.facilities_url:
                await self.refresh_parking()
            response = await self.request("GET", f"{self.dispatch_url}/api/v1/vehicles/{self.vehicle_id}/commands")
            if not response["commands"]:
                return
            command = RelocationCommand.model_validate(response["commands"][0]).model_dump(mode="json")
            if command["vehicle_id"] != self.vehicle_id:
                raise RuntimeError("Command delivered to wrong vehicle")
            previous = self.movement.seen.get(command["command_id"])
            if previous and previous["completed"]:
                return  # Never execute a completed command again.
            self.movement.accept(command)
            self.telemetry_changed.set()
            self.acknowledged = False
            self.event_sequence = 0
        command = self.movement.command
        url = f"{self.dispatch_url}/api/v1/dispatches/{command['dispatch_id']}"
        # Capture the current decision once. A lost ACK response retries the identical payload.
        if not self.acknowledged:
            await self.request("POST", url + "/acknowledgements", {
                "command_id": command["command_id"], "vehicle_id": self.vehicle_id,
                "accepted": self.movement.accepted, "reason": self.movement.reason})
            self.acknowledged = True
            self.movement.start()
            self.telemetry_changed.set()
        state = await self.request("GET", url)
        if state["state"] in ("SUCCEEDED", "REJECTED") and state["claim_held"] is False:
            self.movement.complete()
            self.telemetry_changed.set()
            if self.facilities_url:
                await self.refresh_parking()
            return
        if not self.movement.accepted or self.movement.state == "DISPATCHED":
            return
        # Fresh snapshot, new sequence for each sample; delivery failure cannot repeat movement.
        sequence = self.event_sequence
        self.event_sequence += 1
        kind = {"RELOCATING": "PROGRESS", "ARRIVED": "ARRIVED", "FAILED": "FAILED"}[self.movement.state]
        await self.request("POST", url + "/events", {
            "command_id": command["command_id"], "vehicle_id": self.vehicle_id,
            "sequence": sequence, "observed_at": utcnow().isoformat(), "kind": kind,
            "distance_travelled_km": self.movement.distance, "reason": self.movement.reason})

    def telemetry_delay(self):
        if self.movement.state in ("DISPATCHED", "RELOCATING", "ARRIVED", "FAILED"):
            return min(self.telemetry_interval, 1.0)
        if self.movement.charging_state == "CHARGING":
            return min(self.telemetry_interval, 2.0)
        return self.telemetry_interval

    async def network_loop(self, operation, interval):
        failures = 0
        while True:
            delay = interval
            try:
                await operation()
                failures = 0
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                failures += 1
                # Identity/session conflicts are not connectivity failures and must not steal ownership.
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 409:
                    code = exc.response.json().get("error", {}).get("code", "")
                    if code in ("SESSION_INVALID", "SESSION_REQUEST_SUPERSEDED", "VEHICLE_REGISTRATION_CONFLICT"):
                        raise
                logging.warning("vehicle=%s operation=%s failure=%s", self.vehicle_id, operation.__name__, type(exc).__name__)
                delay = min(10, interval * 2 ** min(failures, 4)) + random.uniform(0, .2)
            if operation == self.send_telemetry and failures == 0:
                # Wake immediately on a command/state transition, otherwise
                # wait 1s in motion, 2s charging or 5s idle by default.
                self.telemetry_changed.clear()
                try:
                    await asyncio.wait_for(self.telemetry_changed.wait(), timeout=self.telemetry_delay())
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)

    async def movement_loop(self):
        last = time.monotonic()
        while True:
            await asyncio.sleep(self.tick_seconds)
            now = time.monotonic()
            previous = (self.movement.state, self.movement.charging_state)
            self.movement.advance((now - last) * self.time_scale, self.speed_kph)
            if self.parking_verified:
                self.movement.charge((now - last) * self.time_scale)
            if previous != (self.movement.state, self.movement.charging_state):
                self.telemetry_changed.set()
            last = now

    async def run(self):
        async with httpx.AsyncClient(timeout=2) as owned:
            self.client = self.client or owned
            tasks = [asyncio.create_task(self.network_loop(self.send_telemetry, self.telemetry_interval)),
                     asyncio.create_task(self.network_loop(self.command_step, self.poll_interval)),
                     asyncio.create_task(self.movement_loop())]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                for task in tasks:
                    with suppress(asyncio.CancelledError, Exception):
                        await task
