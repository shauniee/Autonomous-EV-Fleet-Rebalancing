from datetime import datetime
from ev_fleet.common.travel import distance_km


def parse_timestamp(value):
    # Python 3.10 does not accept the RFC3339 Z suffix directly.
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def assess(vehicle, target, settings, now):
    telemetry = vehicle["telemetry"]
    reasons = []
    result = {"vehicle_id": vehicle["vehicle_id"], "state_version": vehicle["state_version"],
              "eligible": False, "exclusion_reasons": reasons}
    if vehicle["connectivity"] != "ONLINE":
        reasons.append("OFFLINE")
    if telemetry is None:
        reasons.append("NO_TELEMETRY")
        return result
    # Recheck observation age after transport as well as fleet-derived freshness.
    age = (now - parse_timestamp(telemetry["observed_at"])).total_seconds()
    if vehicle["stale"] or age >= settings.stale_seconds or age < -5:
        reasons.append("STALE_TELEMETRY")
    if telemetry["availability"] != "AVAILABLE":
        reasons.append("UNAVAILABLE")
    if telemetry.get("charging_state", "NOT_CHARGING") != "NOT_CHARGING":
        reasons.append("CHARGING")
    if vehicle.get("active_rental") is not None:
        reasons.append("RENTED")
    if vehicle.get("active_charging_task") is not None:
        reasons.append("CHARGING_TASK")
    if telemetry["relocation_state"] != "IDLE" or telemetry.get("active_dispatch_id") or vehicle.get("active_claim"):
        reasons.append("BUSY")
    distance = distance_km(telemetry["position"], target["position"]) * settings.route_factor
    energy = distance * vehicle["consumption_kwh_per_km"] * settings.energy_margin_factor
    arrival_soc = telemetry["soc_pct"] - 100 * energy / vehicle["battery_capacity_kwh"]
    result.update(distance_km=distance, energy_required_kwh=energy,
                  current_soc_pct=telemetry["soc_pct"], predicted_arrival_soc_pct=arrival_soc)
    if arrival_soc < settings.min_reserve_pct:
        reasons.append("BELOW_OPERATIONAL_RESERVE")
    if reasons:
        return result
    charging = target.get("charging")
    fresh = charging is not None and -5 <= (now - parse_timestamp(
        charging["observed_at"])).total_seconds() < settings.charging_max_age_seconds
    available = bool(fresh and charging["available_slots"] > 0)
    utilities = {
        "distance": 1 - min(distance / settings.distance_scale_km, 1),
        "current_soc": telemetry["soc_pct"] / 100,
        "arrival_reserve": (arrival_soc - settings.min_reserve_pct) / (100 - settings.min_reserve_pct),
        "charging_fit": arrival_soc / 100 + int(available) * (1 - arrival_soc / 100),
    }
    weights = {"distance": .35, "current_soc": .20, "arrival_reserve": .35, "charging_fit": .10}
    components = {key: value * weights[key] for key, value in utilities.items()}
    result.update(eligible=True, utilities=utilities, weighted_components=components,
                  score=sum(components.values()),
                  charging_assessment="AVAILABLE" if available else "NONE" if fresh else "UNKNOWN_OR_STALE")
    return result
