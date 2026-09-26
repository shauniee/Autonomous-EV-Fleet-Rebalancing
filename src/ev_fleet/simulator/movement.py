from copy import deepcopy
from math import isfinite
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

    def accept(self, command):
        identity = command["command_id"]
        if identity in self.seen:
            if self.seen[identity]["command"] != command:
                raise ValueError("command identity reused with different payload")
            return self.seen[identity]["accepted"]
        if self.command:
            raise ValueError("another command is active")
        self.command = deepcopy(command)
        self.origin = deepcopy(self.position)
        self.distance = 0.0
        self.total = distance_km(self.position, command["target_position"]) * command["route_factor"]
        forecast = predicted_soc(self.position, command["target_position"], self.soc_pct,
                                 self.capacity, self.consumption, command["route_factor"],
                                 command["energy_margin_factor"])
        self.accepted = forecast >= command["min_reserve_pct"]
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
        step = min(remaining, speed_kph * elapsed_seconds / 3600, possible)
        self.distance += step
        self.soc_pct = max(0, self.soc_pct - 100 * step * self.consumption / self.capacity)
        fraction = 1 if self.total == 0 else min(1, self.distance / self.total)
        self.position = {key: self.origin[key] + fraction * (self.command["target_position"][key] - self.origin[key])
                         for key in ("latitude", "longitude")}
        if self.distance >= self.total - 1e-9:
            self.position = deepcopy(self.command["target_position"])
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
                "availability": "UNAVAILABLE" if self.command else "AVAILABLE",
                "relocation_state": self.state,
                "distance_travelled_km": self.distance if self.command else None,
                "active_dispatch_id": self.command["dispatch_id"] if self.command else None}
