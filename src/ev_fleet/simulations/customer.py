"""Simulation-only customer rental journeys, using the existing Fleet rental API.

This is a finite Kubernetes Job / CLI experiment. Each customer car has exactly
one owner: a MovingNode in this process. It may also receive relocation commands
after a confirmed rental return. Existing 20 vehicle simulator IDs are untouched.
"""
import argparse
import asyncio
import json
import logging
import math
import os
import random
import time
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx

from ev_fleet.common.travel import distance_km, predicted_soc
from ev_fleet.contracts.common import utcnow
from ev_fleet.demand.store import SINGAPORE_ZONES
from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.movement import Movement

LOG = logging.getLogger(__name__)
ZONES = {item[0]: {"latitude": item[2], "longitude": item[3]} for item in SINGAPORE_ZONES}
START_ZONES = ("punggol", "woodlands", "tampines", "bishan", "cbd", "jurong-east")
PICKUP_WEIGHTS = {
    "morning": {"punggol": 7, "woodlands": 6, "tampines": 5, "bishan": 3, "cbd": 1, "jurong-east": 3},
    "evening": {"punggol": 1, "woodlands": 2, "tampines": 2, "bishan": 3, "cbd": 9, "jurong-east": 3},
    "balanced": {zone: 1 for zone in ZONES},
    "spike": {"punggol": 1, "woodlands": 1, "tampines": 1, "bishan": 1, "cbd": 9, "jurong-east": 1},
}
DESTINATION_WEIGHTS = {
    "morning": {"cbd": 9, "bishan": 3, "jurong-east": 1, "tampines": 1},
    "evening": {"punggol": 7, "woodlands": 6, "tampines": 5, "jurong-east": 3, "bishan": 2},
    "balanced": {zone: 1 for zone in ZONES},
    "spike": {"cbd": 9, "tampines": 3, "bishan": 2},
}


def zone_for(position):
    """Nearest representative simulation zone; not a real SG road catchment."""
    return min(ZONES, key=lambda zone: (distance_km(position, ZONES[zone]), zone))


def choose_destination(origin, scenario, rng):
    """Synthetic trip preferences, not observed Singapore commuter data."""
    weights = DESTINATION_WEIGHTS[scenario]
    candidates = [zone for zone in ZONES if zone != origin and weights.get(zone, 0) > 0]
    return rng.choices(candidates, weights=[weights[zone] for zone in candidates], k=1)[0]


class RentalMovement(Movement):
    """Use the same physical speed/energy model, keeping relocation state IDLE.

    The Fleet active_rental record (not an invented RELOCATING enum) excludes
    cars from dispatch while customers are driving.
    """

    def __init__(self, position, soc_pct):
        super().__init__(position, soc_pct)
        self.rental = None

    def can_rent_to(self, target, min_soc_pct=20):
        return (self.command is None and self.rental is None and self.state == "IDLE"
                and predicted_soc(self.position, target, self.soc_pct, self.capacity,
                                  self.consumption, 1.3, 1.15) >= min_soc_pct)

    def start_customer_trip(self, rental_id, target):
        if not self.can_rent_to(target):
            raise ValueError("customer trip conflicts with relocation or battery reserve")
        self.rental = {"id": rental_id, "origin": deepcopy(self.position),
                       "destination": deepcopy(target),
                       "total": distance_km(self.position, target) * 1.3,
                       "distance": 0.0, "arrived": False}

    def accept(self, command):
        if self.rental is not None:
            raise ValueError("cannot accept relocation while customer rental is active")
        return super().accept(command)

    def advance(self, elapsed_seconds, speed_kph):
        if not math.isfinite(elapsed_seconds) or not math.isfinite(speed_kph) or elapsed_seconds < 0 or speed_kph <= 0:
            raise ValueError("invalid simulation step")
        if self.rental is None:
            return super().advance(elapsed_seconds, speed_kph)
        trip = self.rental
        if trip["arrived"]:
            return
        remaining = max(0, trip["total"] - trip["distance"])
        possible = self.soc_pct / 100 * self.capacity / self.consumption
        step = min(remaining, speed_kph * elapsed_seconds / 3600, possible)
        trip["distance"] += step
        self.soc_pct = max(0, self.soc_pct - 100 * step * self.consumption / self.capacity)
        fraction = 1 if trip["total"] == 0 else min(1, trip["distance"] / trip["total"])
        self.position = {key: trip["origin"][key] + fraction * (trip["destination"][key] - trip["origin"][key])
                         for key in ("latitude", "longitude")}
        if trip["distance"] >= trip["total"] - 1e-9:
            self.position = deepcopy(trip["destination"])
            trip["arrived"] = True
        elif self.soc_pct <= 1e-9:
            raise RuntimeError("customer car ran out of simulated battery before return")

    def telemetry(self):
        data = super().telemetry()
        if self.rental is not None:
            data["availability"] = "UNAVAILABLE"
            # Fleet allows customer movement only with IDLE relocation state.
            data["relocation_state"] = "IDLE"
            data["active_dispatch_id"] = None
        return data

    def finish_customer_trip(self):
        if self.rental is None or not self.rental["arrived"]:
            raise ValueError("cannot finish an incomplete customer trip")
        self.rental = None


class RentalVehicle(MovingNode):
    def __init__(self, fleet_url, dispatch_url, vehicle_id, position, soc_pct, **kwargs):
        super().__init__(fleet_url, dispatch_url, vehicle_id, position, soc_pct, **kwargs)
        self.movement = RentalMovement(position, soc_pct)
        self.telemetry_lock = asyncio.Lock()

    async def register(self):
        # Existing test identity after a prior successful Job: preserve its final
        # position/SOC, rather than teleporting it back to its seed coordinates.
        state = await self.request("POST", self.fleet_url + "/api/v1/vehicles", {
            "vehicle_id": self.vehicle_id, "battery_capacity_kwh": self.movement.capacity,
            "consumption_kwh_per_km": self.movement.consumption})
        previous = state.get("telemetry")
        if not self.initialized and previous and not state.get("active_claim") and not state.get("active_rental") and previous["relocation_state"] == "IDLE":
            self.movement.position = deepcopy(previous["position"])
            self.movement.soc_pct = previous["soc_pct"]
        await super().register()

    async def send_telemetry(self):
        async with self.telemetry_lock:
            await super().send_telemetry()

    async def command_step(self):
        if self.movement.rental is not None:
            return
        await super().command_step()

    async def run_customer_trip(self, destination_zone, on_started=None):
        """One attempt; Fleet is authoritative on availability and return."""
        if not self.ready.is_set():
            return "not-ready"
        target = ZONES[destination_zone]
        if not self.movement.can_rent_to(target):
            return "ineligible"
        path = f"{self.fleet_url}/api/v1/vehicles/{self.vehicle_id}"
        before = await self.request("GET", path)
        if before["effective_availability"] != "AVAILABLE" or before.get("active_rental") or before.get("active_claim"):
            return "unavailable"
        if not self.movement.can_rent_to(target):
            return "ineligible"
        rental_id = str(uuid4())
        start_request = {"rental_id": rental_id, "request_id": str(uuid4()),
                         "expected_version": before["state_version"],
                         "expected_end_at": (utcnow() + timedelta(hours=2)).isoformat()}
        try:
            await self.request("POST", path + "/rentals", start_request)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                return "conflict"
            raise
        # Only the owning EV process changes physical position and SOC.
        self.movement.start_customer_trip(rental_id, target)
        if on_started is not None:
            on_started()
        LOG.info("CUSTOMER_START vehicle=%s rental=%s origin=%s destination=%s",
                 self.vehicle_id, rental_id, zone_for(before["telemetry"]["position"]), destination_zone)
        distance = self.movement.rental["total"]
        expected_wall_seconds = 3600 * distance / (self.speed_kph * self.time_scale)
        deadline = time.monotonic() + max(30, expected_wall_seconds * 2 + 20)
        while not self.movement.rental["arrived"]:
            if time.monotonic() > deadline:
                raise TimeoutError(f"Customer journey timed out; rental {rental_id} remains active")
            await asyncio.sleep(min(self.tick_seconds, 0.25))
        await self.send_telemetry()
        # Periodic telemetry can advance Fleet state_version during this request.
        # Retry only version conflicts, with a fresh state read each time.
        return_id = str(uuid4())
        for attempt in range(12):
            current = await self.request("GET", path)
            if not current.get("active_rental"):
                receipt = await self.request("GET", path + f"/rentals/{rental_id}")
                if receipt["status"] == "RETURNED":
                    self.movement.finish_customer_trip()
                    await self.send_telemetry()
                    return "returned"
                raise RuntimeError("Rental state disappeared without a confirmed return")
            request = {"request_id": return_id, "expected_version": current["state_version"]}
            try:
                receipt = await self.request("POST", path + f"/rentals/{rental_id}/return", request)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 409:
                    await asyncio.sleep(0.05)
                    continue
                raise
            if receipt["status"] != "RETURNED":
                raise RuntimeError("Unexpected rental return status")
            self.movement.finish_customer_trip()
            await self.send_telemetry()
            LOG.info("CUSTOMER_RETURN vehicle=%s rental=%s zone=%s soc_pct=%.2f",
                     self.vehicle_id, rental_id, destination_zone, self.movement.soc_pct)
            return "returned"
        raise RuntimeError(f"Unable to confirm return for rental {rental_id}; fail closed")


async def run_experiment(args):
    rng = random.Random(args.seed)
    zones = [START_ZONES[i % len(START_ZONES)] for i in range(args.vehicles)]
    nodes = [RentalVehicle(args.fleet_url, args.dispatch_url, f"customer-ev-{i+1:02d}",
                           ZONES[zone], 85 - (i % 4) * 3,
                           time_scale=args.time_scale, telemetry_interval=2,
                           tick_seconds=0.2, poll_interval=1)
             for i, zone in enumerate(zones)]
    tasks = [asyncio.create_task(node.run()) for node in nodes]
    active = set()
    trips = []
    metrics = {"attempted": 0, "started": 0, "returned": 0, "unfulfilled": 0, "errors": 0}
    wall_duration = args.duration_minutes * 60 / args.time_scale
    target_requests = max(0, round(args.rate_per_hour * args.duration_minutes / 60))
    if args.scenario == "spike":
        target_requests *= 2

    async def run_one(node, destination):
        try:
            result = await node.run_customer_trip(destination, on_started=lambda: metrics.__setitem__("started", metrics["started"] + 1))
            metrics["returned" if result == "returned" else "unfulfilled"] += 1
        except (httpx.HTTPError, RuntimeError, TimeoutError, ValueError) as exc:
            metrics["errors"] += 1
            LOG.error("CUSTOMER_ERROR vehicle=%s kind=%s detail=%s",
                      node.vehicle_id, type(exc).__name__, str(exc)[:160])
        finally:
            active.discard(node.vehicle_id)

    try:
        await asyncio.wait_for(asyncio.gather(*(node.ready.wait() for node in nodes)), timeout=60)
        start_time = time.monotonic()
        LOG.info("CUSTOMER_SIMULATION_START scenario=%s seed=%d vehicles=%d requests=%d",
                 args.scenario, args.seed, len(nodes), target_requests)
        for i in range(target_requests):
            due = start_time + i * wall_duration / max(1, target_requests)
            await asyncio.sleep(max(0, due - time.monotonic()))
            metrics["attempted"] += 1
            candidates = [node for node in nodes
                          if node.vehicle_id not in active and node.movement.rental is None
                          and node.movement.command is None and node.ready.is_set()]
            if not candidates:
                metrics["unfulfilled"] += 1
                continue
            weights = [PICKUP_WEIGHTS[args.scenario].get(zone_for(node.movement.position), 1)
                       for node in candidates]
            selected = rng.choices(candidates, weights=weights, k=1)[0]
            origin = zone_for(selected.movement.position)
            destination = choose_destination(origin, args.scenario, rng)
            if not selected.movement.can_rent_to(ZONES[destination]):
                metrics["unfulfilled"] += 1
                continue
            active.add(selected.vehicle_id)
            trips.append(asyncio.create_task(run_one(selected, destination)))
        if trips:
            await asyncio.gather(*trips)
        # A finite, seeded experiment; real network scheduling can vary results.
        LOG.info("CUSTOMER_SIMULATION_SUMMARY %s", json.dumps({
            "scenario": args.scenario, "seed": args.seed, "vehicles": args.vehicles,
            "time_scale": args.time_scale, "duration_minutes": args.duration_minutes, **metrics
        }, sort_keys=True))
        if metrics["errors"]:
            raise RuntimeError("Some customer trips failed; review logs before rerunning")
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description="Finite simulated customer rental Job (same ev-fleet Kubernetes cluster)")
    parser.add_argument("--fleet-url", default=os.getenv("FLEET_URL", "http://localhost:8001"))
    parser.add_argument("--dispatch-url", default=os.getenv("DISPATCH_URL", "http://localhost:8004"))
    parser.add_argument("--scenario", choices=sorted(PICKUP_WEIGHTS), default="morning")
    parser.add_argument("--vehicles", type=int, default=6)
    parser.add_argument("--duration-minutes", type=float, default=60)
    parser.add_argument("--rate-per-hour", type=float, default=18)
    parser.add_argument("--time-scale", type=float, default=float(os.getenv("SIMULATION_TIME_SCALE", "30")))
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if not 1 <= args.vehicles <= 12 or not all(math.isfinite(v) and v > 0 for v in (
            args.duration_minutes, args.rate_per_hour, args.time_scale)):
        parser.error("vehicles must be 1..12; duration, request rate and scale must be positive and finite")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run_experiment(args))


if __name__ == "__main__":
    main()
