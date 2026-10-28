from copy import deepcopy
from math import isfinite
from bisect import bisect_right
from ev_fleet.common.travel import distance_km, predicted_soc


class Movement:
    """Pure incremental simulation, advanced with elapsed simulated seconds."""
    def __init__(self, position, soc_pct, capacity_kwh=60, consumption_kwh_per_km=.18):
        if not all(isfinite(x) for x in (soc_pct, capacity_kwh, consumption_kwh_per_km)):
            raise ValueError("finite physical parameters required")
        if not 0 <= soc_pct <= 100 or capacity_kwh <= 0 or consumption_kwh_per_km <= 0:
            raise ValueError("invalid physical parameters")
        self.position = deepcopy(position)
        self.soc_pct = soc_pct
        self.capacity = capacity_kwh
        self.consumption = consumption_kwh_per_km
        self.command = None
        self.seen = {}
        self.state = "IDLE"
        self.accepted = None
        self.reason = None
        self.distance = 0.0
        self.total = 0.0
        self.origin = deepcopy(position)
        self.parking = None
        self.charging_state = "NOT_CHARGING"
        self.path_lengths = []

    def accept(self, command):
        identity = command["command_id"]
        if identity in self.seen:
            if self.seen[identity]["command"] != command:
                raise ValueError("command identity reused with different payload")
            return self.seen[identity]["accepted"]
        if self.command:
            raise ValueError("another command is active")
        if command.get("route") and distance_km(self.position, command["route"]["geometry"][0]) > .005:
            raise ValueError("route starts away from the road-aligned EV position")
        self.parking = None
        self.charging_state = "NOT_CHARGING"
        self.command = deepcopy(command)
        self.origin = deepcopy(self.position)
        self.distance = 0.0
        route = command.get("route")
        self.path_lengths = []
        if route:
            self.total = route["distance_km"]
            self.path_lengths = [0.0]
            for a, b in zip(route["geometry"], route["geometry"][1:]):
                self.path_lengths.append(self.path_lengths[-1] + distance_km(a, b))
            forecast = self.soc_pct - 100 * self.total * self.consumption * command["energy_margin_factor"] / self.capacity
            if self.total > 0 and self.path_lengths[-1] <= 0:
                raise ValueError("route has no usable path")
        else:
            self.total = distance_km(self.position, command["target_position"]) * command["route_factor"]
            forecast = predicted_soc(self.position, command["target_position"], self.soc_pct,
                                     self.capacity, self.consumption, command["route_factor"],
                                     command["energy_margin_factor"])
        self.accepted = forecast >= command["min_reserve_pct"]
        if route and self.accepted:
            # Only a few metres of route-start coordinate normalisation
            # before motion. All subsequent samples lie on OneMap geometry.
            self.position = deepcopy(route["geometry"][0])
            self.origin = deepcopy(self.position)
        self.reason = None if self.accepted else "INSUFFICIENT_LOCAL_SOC"
        self.state = "DISPATCHED" if self.accepted else "IDLE"
        self.seen[identity] = {"command": deepcopy(command), "accepted": self.accepted, "completed": False}
        return self.accepted

    def start(self):
        if self.command and self.accepted and self.state == "DISPATCHED":
            self.state = "RELOCATING"

    def advance(self, elapsed_seconds, speed_kph):
        if not isfinite(elapsed_seconds) or not isfinite(speed_kph) or elapsed_seconds < 0 or speed_kph <= 0:
            raise ValueError("invalid simulation step")
        if self.state != "RELOCATING":
            return
        remaining = max(0, self.total - self.distance)
        possible = self.soc_pct / 100 * self.capacity / self.consumption
        route = self.command.get("route")
        speed = self.total / route["duration_seconds"] if route and route["duration_seconds"] > 0 else speed_kph / 3600
        step = min(remaining, speed * elapsed_seconds, possible)
        self.distance += step
        self.soc_pct = max(0, self.soc_pct - 100 * step * self.consumption / self.capacity)
        fraction = 1 if self.total == 0 else min(1, self.distance / self.total)
        if route:
            along_path = fraction * self.path_lengths[-1]
            index = min(len(self.path_lengths) - 2, max(0, bisect_right(self.path_lengths, along_path) - 1))
            length = self.path_lengths[index + 1] - self.path_lengths[index]
            ratio = (along_path - self.path_lengths[index]) / length if length else 1
            a, b = route["geometry"][index:index + 2]
            self.position = {key: a[key] + ratio * (b[key] - a[key]) for key in ("latitude", "longitude")}
        else:
            self.position = {key: self.origin[key] + fraction * (self.command["target_position"][key] - self.origin[key])
                             for key in ("latitude", "longitude")}
        if self.distance >= self.total - 1e-9:
            self.position = deepcopy(route["geometry"][-1] if route else self.command["target_position"])
            self.state = "ARRIVED"
        elif self.soc_pct <= 1e-9:
            self.state = "FAILED"
            self.reason = "BATTERY_DEPLETED"

    def complete(self):
        if not self.command or self.state not in ("ARRIVED", "IDLE"):
            raise ValueError("cannot release an unfinished journey")
        self.seen[self.command["command_id"]]["completed"] = True
        self.command = None
        self.state = "IDLE"
        self.accepted = None
        self.reason = None

    def telemetry(self):
        return {"position": deepcopy(self.position), "soc_pct": self.soc_pct,
                "availability": "UNAVAILABLE" if self.command or self.charging_state == "CHARGING" else "AVAILABLE",
                "relocation_state": self.state,
                "distance_travelled_km": self.distance if self.command else None,
                "active_dispatch_id": self.command["dispatch_id"] if self.command else None,
                "parked_facility_id": self.parking["facility_id"] if self.parking else None,
                "charging_state": self.charging_state,
                "charging_power_kw": self.parking["charging_power_kw"] if self.parking and self.parking["charging_allocated"] else None}

    def park(self, parking):
        self.parking = None
        self.charging_state = "NOT_CHARGING"
        if self.command or not parking or distance_km(self.position, parking["position"]) > .05:
            return False
        self.parking = deepcopy(parking)
        if parking["charging_allocated"]:
            self.charging_state = ("CHARGING" if self.soc_pct < parking["charging_target_soc_pct"] else "COMPLETE")
        return True

    def charge(self, elapsed_seconds):
        if self.command or not self.parking or self.charging_state != "CHARGING":
            return
        # Same simulated clock as movement; physical EVs and LTA are unaffected.
        energy_kwh = self.parking["charging_power_kw"] * max(0, elapsed_seconds) / 3600
        self.soc_pct = min(self.parking["charging_target_soc_pct"], self.soc_pct + 100 * energy_kwh / self.capacity)
        if self.soc_pct >= self.parking["charging_target_soc_pct"]:
            self.charging_state = "COMPLETE"
