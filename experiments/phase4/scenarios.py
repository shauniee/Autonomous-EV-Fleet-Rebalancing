"""Seeded scenario specifications. Generation has no service-side effects."""
from dataclasses import asdict, dataclass, field
from hashlib import sha256
import json
import random
from ev_fleet.demand.store import SINGAPORE_ZONES


ZONE_POSITIONS = {zone_id: {"latitude": latitude, "longitude": longitude}
                  for zone_id, _, latitude, longitude in SINGAPORE_ZONES}


@dataclass(frozen=True)
class VehicleSpec:
    vehicle_id: str
    zone_id: str
    soc_pct: float
    battery_capacity_kwh: float = 60.0
    consumption_kwh_per_km: float = .18
    online: bool = True
    latitude_offset: float = 0.0
    longitude_offset: float = 0.0

    @property
    def position(self):
        base = ZONE_POSITIONS[self.zone_id]
        return {"latitude": base["latitude"] + self.latitude_offset,
                "longitude": base["longitude"] + self.longitude_offset}


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    seed: int
    target_zone_id: str
    demands: dict[str, int]
    charging_slots: int
    vehicles: tuple[VehicleSpec, ...]
    demand_change: int | None = None
    disconnect_selected: bool = False
    concurrent_requests: int = 1
    notes: tuple[str, ...] = field(default_factory=tuple)

    def canonical(self):
        data = asdict(self)
        for vehicle, raw in zip(self.vehicles, data["vehicles"]):
            raw["position"] = vehicle.position
        return data

    def state_hash(self):
        payload = json.dumps(self.canonical(), sort_keys=True, separators=(",", ":"))
        return sha256(payload.encode("utf-8")).hexdigest()


SCENARIO_NAMES = (
    "balanced_fleet", "nearest_low_soc", "farther_high_soc", "limited_charging",
    "multiple_eligible", "multiple_hotspots", "offline_before_selection",
    "disconnect_during_relocation", "changing_demand", "concurrent_requests",
)


def _vehicle(rng, name, zone, soc, online=True, consumption=.18):
    # Small paired perturbations avoid repeating one exact geometry while retaining scenario intent.
    return VehicleSpec(name, zone, round(soc + rng.uniform(-.7, .7), 4),
                       consumption_kwh_per_km=consumption, online=online,
                       latitude_offset=rng.uniform(-.001, .001),
                       longitude_offset=rng.uniform(-.001, .001))


def generate_scenario(name: str, seed: int) -> ScenarioSpec:
    if name not in SCENARIO_NAMES:
        raise ValueError(f"unknown scenario: {name}")
    rng = random.Random(f"phase4:{name}:{seed}")
    demands = {zone_id: 0 for zone_id in ZONE_POSITIONS}
    demands["cbd"] = 6
    charging = 2
    vehicles = [
        _vehicle(rng, "ev-near", "bishan", 72),
        _vehicle(rng, "ev-far", "tampines", 78),
        _vehicle(rng, "ev-west", "jurong-east", 75),
    ]
    options = {}

    if name == "balanced_fleet":
        vehicles = [_vehicle(rng, "ev-near", "bishan", 75),
                    _vehicle(rng, "ev-far", "tampines", 75),
                    _vehicle(rng, "ev-west", "jurong-east", 75)]
    elif name == "nearest_low_soc":
        vehicles = [_vehicle(rng, "ev-near", "bishan", 32),
                    _vehicle(rng, "ev-far", "tampines", 90),
                    _vehicle(rng, "ev-west", "jurong-east", 82)]
    elif name == "farther_high_soc":
        vehicles = [_vehicle(rng, "ev-near", "bishan", 48),
                    _vehicle(rng, "ev-far", "tampines", 96),
                    _vehicle(rng, "ev-west", "jurong-east", 88)]
    elif name == "limited_charging":
        charging = 0
        vehicles = [_vehicle(rng, "ev-near", "bishan", 38),
                    _vehicle(rng, "ev-far", "tampines", 88),
                    _vehicle(rng, "ev-west", "jurong-east", 80)]
    elif name == "multiple_eligible":
        vehicles = [_vehicle(rng, f"ev-{index + 1}", zone, soc, consumption=.16 + index * .015)
                    for index, (zone, soc) in enumerate((
                        ("bishan", 45), ("tampines", 92), ("jurong-east", 76),
                        ("woodlands", 84), ("punggol", 68)))]
    elif name == "multiple_hotspots":
        demands.update({"tampines": 8, "jurong-east": 5, "woodlands": 3})
    elif name == "offline_before_selection":
        vehicles = [_vehicle(rng, "ev-near-offline", "bishan", 95, online=False),
                    _vehicle(rng, "ev-far", "tampines", 78),
                    _vehicle(rng, "ev-west", "jurong-east", 72)]
    elif name == "disconnect_during_relocation":
        options["disconnect_selected"] = True
    elif name == "changing_demand":
        options["demand_change"] = 10
    elif name == "concurrent_requests":
        options["concurrent_requests"] = 2

    spec = ScenarioSpec(name, seed, "cbd", demands, charging, tuple(vehicles), **options)
    validate_scenario(spec)
    return spec


def validate_scenario(spec: ScenarioSpec):
    ids = [vehicle.vehicle_id for vehicle in spec.vehicles]
    if len(ids) != len(set(ids)) or not spec.vehicles:
        raise ValueError("scenario vehicles must be non-empty and unique")
    if spec.target_zone_id not in ZONE_POSITIONS or set(spec.demands) != set(ZONE_POSITIONS):
        raise ValueError("scenario must define all six existing zones")
    if any(not 0 <= vehicle.soc_pct <= 100 for vehicle in spec.vehicles):
        raise ValueError("scenario SOC outside 0..100")
    if spec.concurrent_requests < 1:
        raise ValueError("concurrent_requests must be positive")

