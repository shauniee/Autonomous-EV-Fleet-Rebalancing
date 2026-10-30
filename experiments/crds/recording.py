"""Append-only recording of observed episodes, separate from planner futures."""
import json
from collections import Counter
from datetime import datetime
from math import isfinite
from pathlib import Path
from statistics import median


POLICIES = ("weighted", "crds-current", "crds-delayed")


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("episode timestamps must include a UTC offset")
    return result


class EpisodeRecorder:
    """Consume actual observations/decisions supplied by an isolated episode driver.

    The recorder never creates demand, sees future events, approves a dispatch,
    starts services or resets data. Missing observation intervals remain unknown.
    """

    def __init__(self, output, manifest):
        required = ("case_id", "seed", "policy", "initial_state_hash", "workload_hash", "transport",
                    "duration_seconds", "zone_ids")
        if any(key not in manifest for key in required) or manifest["policy"] not in POLICIES:
            raise ValueError("episode manifest requires case, seed, policy, state/workload hashes and transport")
        if manifest["transport"] not in ("inprocess", "http"):
            raise ValueError("transport must identify inprocess or http observations")
        if (not isfinite(manifest["duration_seconds"]) or manifest["duration_seconds"] <= 0
                or not manifest["zone_ids"] or len(set(manifest["zone_ids"])) != len(manifest["zone_ids"])):
            raise ValueError("episode duration and unique observed zone IDs are required")
        self.directory = Path(output)
        self.directory.mkdir(parents=True, exist_ok=False)
        self.manifest = dict(manifest)
        self.file = (self.directory / "events.jsonl").open("x", encoding="utf-8")
        self.previous = None
        self.first_at = None
        self.known_shortage = 0.0
        self.unknown_minutes = 0.0
        self.zone_shortage = Counter()
        self.decisions = Counter()
        self.dispatches = {}
        self.latencies = []
        self.execution = Counter()
        self.errors = []
        self.observations = 0
        self.distance = 0.0
        self.energy = 0.0
        self.distance_complete = True
        self.energy_complete = True
        self.recommendations = {}
        self.arrival_errors = []
        self.arrivals = set()
        self.write({"kind": "manifest", **manifest})

    def write(self, event):
        self.file.write(json.dumps(event, sort_keys=True, allow_nan=False) + "\n")
        self.file.flush()

    def observe(self, event):
        now = timestamp(event["observed_at"])
        if self.first_at is None:
            self.first_at = now
        if self.previous is not None:
            minutes = (now - timestamp(self.previous["observed_at"])).total_seconds() / 60
            if minutes <= 0:
                raise ValueError("observation times must increase")
            zones = self.previous["zones"]
            maximum_gap = self.manifest.get("max_observation_gap_seconds", 120)
            known = (minutes * 60 <= maximum_gap and set(zones) == set(self.manifest["zone_ids"])
                     and all(isinstance(z.get(key), int) and not isinstance(z.get(key), bool)
                             and z[key] >= 0 for z in zones.values() for key in ("desired_stock", "usable_stock")))
            if not known:
                self.unknown_minutes += minutes
            else:
                for zone_id, zone in zones.items():
                    shortage = max(0, zone["desired_stock"] - zone["usable_stock"]) * minutes
                    self.known_shortage += shortage
                    self.zone_shortage[zone_id] += shortage
        if "dispatches" not in event:
            self.distance_complete = self.energy_complete = False
        for dispatch in event.get("dispatches", []):
            key = dispatch["dispatch_id"]
            previous = self.dispatches.get(key)
            travelled = dispatch.get("distance_travelled_km", 0)
            if "distance_travelled_km" not in dispatch:
                self.distance_complete = False
            # Optional cumulative measurement supplied by the episode driver.
            # Never substitute predicted energy or fleet-wide rental SOC loss.
            energy = dispatch.get("measured_energy_kwh")
            if not isinstance(energy, (int, float)) or not isfinite(energy) or energy < 0:
                self.energy_complete = False
            elif self.observations:
                self.energy += max(0, energy - ((previous or {}).get("measured_energy_kwh") or 0))
            if self.observations:
                self.distance += max(0, travelled - (previous or {}).get("distance_travelled_km", 0))
            self.dispatches[key] = dispatch
            recommendation = self.recommendations.get(dispatch.get("recommendation_id"))
            issued = (dispatch.get("command") or {}).get("issued_at")
            completed = dispatch.get("completed_at")
            if (key not in self.arrivals and dispatch["state"] == "SUCCEEDED" and recommendation
                    and issued and completed and recommendation.get("crds")):
                metadata = recommendation["crds"]
                low, high = metadata.get("arrival_minutes_min"), metadata.get("arrival_minutes_max")
                if low is not None and high is not None:
                    actual = (timestamp(completed) - timestamp(issued)).total_seconds() / 60
                    self.arrival_errors.append(actual - (low + high) / 2)
                    self.arrivals.add(key)
        self.previous = event
        self.observations += 1
        self.write(event)

    def decision(self, event):
        record = event["recommendation"]
        metadata = record.get("crds")
        policy = self.manifest["policy"]
        if policy == "weighted" and metadata:
            raise ValueError("weighted episode received a CRDS response")
        if policy != "weighted":
            expected = policy == "crds-delayed"
            if metadata is None or metadata["source_references"].get("expand_age") is not expected:
                raise ValueError("observed CRDS policy does not match the declared ablation")
        self.decisions[metadata["decision"] if metadata else "LEGACY"] += 1
        self.latencies.append(event["latency_ms"])
        if metadata:
            self.execution.update({k: v for k, v in metadata.get("execution", {}).items()
                                   if isinstance(v, (int, float))})
        if "recommendation_id" in record:
            self.recommendations[record["recommendation_id"]] = record
        # Batch children retain their distinct IDs; a driver can also record
        # their full responses to measure per-child arrival predictions.
        self.write(event)

    def error(self, event):
        self.errors.append(event)
        self.write(event)

    def finish(self, completed):
        duration = ((timestamp(self.previous["observed_at"]) - self.first_at).total_seconds()
                    if self.previous is not None else 0)
        complete = (completed and self.observations >= 2 and self.unknown_minutes == 0
                    and abs(duration - self.manifest["duration_seconds"]) < .001
                    and not any(e.get("infrastructure", True) for e in self.errors))
        latencies = sorted(self.latencies)
        summary = {**self.manifest, "complete": bool(complete), "observations": self.observations,
            "shortage_vehicle_minutes": self.known_shortage if complete else None,
            "known_shortage_vehicle_minutes": self.known_shortage,
            "unknown_minutes": self.unknown_minutes, "zone_shortage_vehicle_minutes": dict(self.zone_shortage),
            "decisions": dict(self.decisions), "error_count": len(self.errors),
            "dispatch_outcomes": dict(Counter(d["state"] for d in self.dispatches.values())),
            "observed_duration_seconds": duration,
            "measurement_method": "left-held observed stock; missing intervals remain unknown",
            "relocation_distance_km_in_window": self.distance if self.distance_complete else None,
            "measured_relocation_energy_kwh_in_window": self.energy if self.energy_complete else None,
            "arrival_error_minutes_from_range_midpoint": self.arrival_errors,
            "decision_latency_p50_ms": median(latencies) if latencies else None,
            "decision_latency_p95_ms": latencies[max(0, (95 * len(latencies) + 99) // 100 - 1)] if latencies else None,
            "execution": dict(self.execution)}
        self.write({"kind": "completion", "complete": bool(complete)})
        self.file.close()
        (self.directory / "summary.json").write_text(json.dumps(summary, indent=2, allow_nan=False) + "\n")
        return summary


def observed_snapshot(fleet, demand, dispatches, now):
    """Normalize real service observations; never accept planner future stock."""
    from ev_fleet.contracts.vehicles import FleetSnapshot
    from ev_fleet.contracts.crds import CrdsDemandSnapshot
    from ev_fleet.demand.aggregation import capacity
    from ev_fleet.demand.settings import DemandSettings

    fleet = FleetSnapshot.model_validate(fleet)
    demand = CrdsDemandSnapshot.model_validate(demand)
    settings = DemandSettings.from_env()
    zones = {z.zone_id: z for z in demand.zones}
    counts, _, _ = capacity(fleet, zones, settings, now)
    fleet_fresh = -5 <= (now - fleet.snapshot_at).total_seconds() < settings.fleet_snapshot_max_age_seconds
    return {"kind": "observation", "observed_at": now.isoformat(), "zones": {
        key: {"desired_stock": z.demand.desired_available_vehicles if z.demand and
              -5 <= (now - z.demand.observed_at).total_seconds() < settings.demand_max_age_seconds else None,
              "usable_stock": counts[key]["available_evs"] if fleet_fresh else None}
        for key, z in zones.items()}, "vehicles": fleet.model_dump(mode="json")["vehicles"],
        "dispatches": dispatches["dispatches"]}
