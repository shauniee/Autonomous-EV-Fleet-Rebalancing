from copy import deepcopy
from math import atan2, cos, degrees, isfinite, radians
from bisect import bisect_right
from ev_fleet.common.travel import distance_km, predicted_soc
from ev_fleet.contracts.recovery import SimulatorCheckpoint, SimulatorConfiguration


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
        self.speed_kph = 0.0
        self.accepted = None
        self.reason = None
        self.distance = 0.0
        self.total = 0.0
        self.origin = deepcopy(position)
        self.parking = None
        self.charging_state = "NOT_CHARGING"
        self.path_lengths = []
        self.odometer_km = 0.0
        self.speed_kph = 0.0
        self.heading_deg = 0.0
        self.completed_command_id = None

    def snapshot(self):
        """Physical state and the original command decision, without network identity."""
        decision = self.seen.get(self.command["command_id"], {}) if self.command else {}
        return {"physical": self.telemetry(), "command": deepcopy(self.command),
                "accepted": self.accepted, "reason": decision.get("reason"),
                "completed_command_id": self.completed_command_id}

    @classmethod
    def from_checkpoint(cls, checkpoint, configuration):
        """Restore a recorded trip directly; never re-accept or re-run its origin."""
        saved = SimulatorCheckpoint.model_validate(checkpoint)
        configuration = SimulatorConfiguration.model_validate(configuration)
        if saved.simulation is None or saved.simulation != configuration:
            raise ValueError("checkpoint simulation model/configuration does not match")
        physical = saved.physical
        movement = cls(physical.position.model_dump(), physical.soc_pct,
                       configuration.capacity_kwh, configuration.consumption_kwh_per_km)
        movement.odometer_km, movement.heading_deg = physical.odometer_km, physical.heading_deg
        movement.command = saved.command.model_dump(mode="json") if saved.command else None
        movement.accepted, movement.reason = saved.accepted, saved.reason
        movement.state = physical.relocation_state.value
        movement.distance = physical.distance_travelled_km or 0.0
        movement.completed_command_id = str(saved.completed_command_id) if saved.completed_command_id else None
        if movement.completed_command_id:
            movement.seen[movement.completed_command_id] = {"command": None, "accepted": None, "completed": True}
        if saved.command:
            route = movement.command.get("route")
            if route:
                movement.total = route["distance_km"]
                movement.origin = deepcopy(route["geometry"][0])
                movement.path_lengths = [0.0]
                for a, b in zip(route["geometry"], route["geometry"][1:]):
                    movement.path_lengths.append(movement.path_lengths[-1] + distance_km(a, b))
                if saved.accepted and (movement.total > 0 and movement.path_lengths[-1] <= 0
                        or distance_km(movement.position, movement.route_position()) > .005):
                    raise ValueError("checkpoint position and route progress disagree")
            movement.seen[str(saved.command.command_id)] = {"command": deepcopy(movement.command),
                "accepted": saved.accepted, "reason": saved.reason, "completed": False}
        if movement.state == "FAILED":
            if saved.pending_event and saved.pending_event.kind == "FAILED" and saved.pending_event.reason:
                movement.reason = saved.pending_event.reason
            elif "BATTERY_DEPLETED" in physical.fault_codes:
                movement.reason = "BATTERY_DEPLETED"
            else:
                raise ValueError("failed checkpoint has no recorded execution failure reason")
        # Parking must be verified anew; no downtime is advanced or charged.
        movement.speed_kph = 0.0
        return movement

    def route_position(self):
        route = self.command["route"]
        fraction = 1 if self.total == 0 else min(1, self.distance / self.total)
        along_path = fraction * self.path_lengths[-1]
        index = min(len(self.path_lengths) - 2, max(0, bisect_right(self.path_lengths, along_path) - 1))
        length = self.path_lengths[index + 1] - self.path_lengths[index]
        ratio = (along_path - self.path_lengths[index]) / length if length else 1
        a, b = route["geometry"][index:index + 2]
        return {key: a[key] + ratio * (b[key] - a[key]) for key in ("latitude", "longitude")}

    def accept(self, command):
        identity = command["command_id"]
        if identity in self.seen:
            if self.seen[identity]["command"] != command:
                raise ValueError("command identity reused with different payload")
            return self.seen[identity]["accepted"]
        if self.command:
            raise ValueError("another command is active")
        if self.charging_state != "NOT_CHARGING":
            raise ValueError("charging cable handoff must finish before movement")
        route = command.get("route")
        # The signed-off command carries validated on-road geometry. OneMap
        # remains primary; a configured OSRM alternate is equally road-only.
        if not route or route.get("source") not in ("ONEMAP", "OSRM"):
            self.command = deepcopy(command)
            self.accepted = False
            self.reason = "ONEMAP_ROUTE_REQUIRED"
            self.state = "IDLE"
            self.distance = 0.0
            self.seen[identity] = {"command": deepcopy(command), "accepted": False,
                                   "reason": self.reason, "completed": False}
            return False
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
        self.seen[identity] = {"command": deepcopy(command), "accepted": self.accepted,
                               "reason": self.reason, "completed": False}
        return self.accepted

    def start(self):
        if self.command and self.accepted and self.state == "DISPATCHED":
            self.state = "RELOCATING"

    def advance(self, elapsed_seconds, speed_kph, *, traffic_speed_cap_kph=None):
        if not isfinite(elapsed_seconds) or not isfinite(speed_kph) or elapsed_seconds < 0 or speed_kph <= 0:
            raise ValueError("invalid simulation step")
        if self.state != "RELOCATING":
            self.speed_kph = 0.0
            return
        remaining = max(0, self.total - self.distance)
        possible = self.soc_pct / 100 * self.capacity / self.consumption
        route = self.command.get("route")
        speed = self.total / route["duration_seconds"] if route and route["duration_seconds"] > 0 else speed_kph / 3600
        # A road route normally follows its OneMap/OSRM average journey speed.
        # A *simulated* traffic cap may only SLOW that authoritative speed;
        # it never makes a vehicle faster or creates an off-road shortcut.
        if traffic_speed_cap_kph is not None:
            if not isfinite(traffic_speed_cap_kph) or traffic_speed_cap_kph < 0:
                raise ValueError("invalid synthetic traffic speed cap")
            speed = min(speed, traffic_speed_cap_kph / 3600)
        step = min(remaining, speed * elapsed_seconds, possible)
        previous_position = deepcopy(self.position)
        self.distance += step
        self.odometer_km += step
        self.speed_kph = (step * 3600 / elapsed_seconds) if elapsed_seconds else 0.0
        self.soc_pct = max(0, self.soc_pct - 100 * step * self.consumption / self.capacity)
        fraction = 1 if self.total == 0 else min(1, self.distance / self.total)
        if route:
            self.position = self.route_position()
        else:
            self.position = {key: self.origin[key] + fraction * (self.command["target_position"][key] - self.origin[key])
                             for key in ("latitude", "longitude")}
        if distance_km(previous_position, self.position) > 1e-9:
            dy = self.position["latitude"] - previous_position["latitude"]
            dx = (self.position["longitude"] - previous_position["longitude"]) * cos(radians(self.position["latitude"]))
            self.heading_deg = (degrees(atan2(dx, dy)) + 360) % 360
        if self.distance >= self.total - 1e-9:
            self.position = deepcopy(route["geometry"][-1] if route else self.command["target_position"])
            self.state = "ARRIVED"
            self.speed_kph = 0.0
        elif self.soc_pct <= 1e-9:
            self.state = "FAILED"
            self.speed_kph = 0.0
            self.reason = "BATTERY_DEPLETED"

    def complete(self):
        if not self.command or self.state not in ("ARRIVED", "IDLE"):
            raise ValueError("cannot release an unfinished journey")
        self.seen[self.command["command_id"]]["completed"] = True
        self.completed_command_id = self.command["command_id"]
        self.seen = {self.completed_command_id: self.seen[self.completed_command_id]}
        self.command = None
        self.state = "IDLE"
        self.accepted = None
        self.reason = None

    def telemetry(self):
        # Synthetic condition estimates for demonstration, not measured sensors.
        charging_kw = self.parking["charging_power_kw"] if self.parking and self.charging_state == "CHARGING" else 0.0
        traction_kw = self.speed_kph * self.consumption
        return {"position": deepcopy(self.position), "soc_pct": self.soc_pct,
                "speed_kph": round(self.speed_kph, 3), "heading_deg": round(self.heading_deg, 3),
                "odometer_km": round(self.odometer_km, 6),
                "battery_temp_c": round(28 + min(12, traction_kw * .4 + charging_kw * .03), 2),
                "motor_temp_c": round(29 + min(40, traction_kw * 1.4), 2),
                "power_kw": round(traction_kw - charging_kw, 3),
                "battery_soh_pct": 98.0, "gps_quality": "SIMULATED",
                "fault_codes": ["BATTERY_DEPLETED"] if self.state == "FAILED" and self.reason == "BATTERY_DEPLETED" else [],
                "availability": "UNAVAILABLE" if self.command or self.charging_state != "NOT_CHARGING" else "AVAILABLE",
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
        self.speed_kph = 0.0
        self.parking = deepcopy(parking)
        if parking["charging_allocated"]:
            cable = parking.get("cable_status", "AWAITING_CONNECTION")
            if cable == "CONNECTED":
                self.charging_state = ("CHARGING" if self.soc_pct < parking["charging_target_soc_pct"] else "COMPLETE")
            elif cable != "DISCONNECTED":
                self.charging_state = "AWAITING_CONNECTION"
        return True

    def charge(self, elapsed_seconds):
        if self.command or not self.parking or self.charging_state != "CHARGING":
            return
        # Same simulated clock as movement; physical EVs and LTA are unaffected.
        energy_kwh = self.parking["charging_power_kw"] * max(0, elapsed_seconds) / 3600
        self.soc_pct = min(self.parking["charging_target_soc_pct"], self.soc_pct + 100 * energy_kwh / self.capacity)
        if self.soc_pct >= self.parking["charging_target_soc_pct"]:
            self.charging_state = "COMPLETE"
