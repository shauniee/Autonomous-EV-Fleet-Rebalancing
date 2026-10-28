"""Deterministic global-greedy EV-to-mock-facility matcher.

Not a guaranteed globally optimal matching. It observes mock capacity but
does not reserve space until a dispatcher explicitly approves.
"""
from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT
from ev_fleet.rebalancing.policy import assess


def rank_pairs(fleet, zone, facilities, policy, now, quantity):
    edges = []
    left = {}
    left_charge = {}
    for site in facilities:
        if (site.zone_id != zone["zone_id"] or site.status != "OPEN"
                or not site.is_fresh(now) or site.parking_available < 1):
            continue
        left[site.facility_id] = site.parking_available
        left_charge[site.facility_id] = site.charging_available
        target = {**zone, "position": site.position.model_dump(mode="json"),
                  "charging": {"observed_at": now.isoformat(),
                               "available_slots": site.charging_available}}
        for vehicle in fleet["vehicles"]:
            assessment = assess(vehicle, target, policy, now)
            if not assessment["eligible"]:
                continue
            need_charge = assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
            if site.facility_type == "CHARGING" and not need_charge:
                continue  # Do not send high-SOC cars to occupy a charger as ordinary parking.
            if need_charge and site.charging_available <= 0:
                continue
            # LTA does not supply total carpark capacity. A constant neutral
            # utility avoids presenting available counts as an occupancy ratio.
            parking_fit = (site.parking_available / max(1, site.parking_capacity)
                           if site.parking_capacity is not None else 0.5)
            score = .85 * assessment["score"] + .15 * parking_fit
            edges.append({
                "vehicle_id": vehicle["vehicle_id"], "facility_id": site.facility_id,
                "facility_name": site.name, "target_position": site.position.model_dump(mode="json"),
                "requires_charging": need_charge, "distance_km": assessment["distance_km"],
                "predicted_arrival_soc_pct": assessment["predicted_arrival_soc_pct"],
                "score": min(1.0, score), "assessment": assessment,
            })
    edges.sort(key=lambda e: (-e["score"], -e["predicted_arrival_soc_pct"],
                               e["distance_km"], e["vehicle_id"], e["facility_id"]))
    assigned_vehicles = set()
    selected = []
    for edge in edges:
        if len(selected) == quantity:
            break
        site, vehicle = edge["facility_id"], edge["vehicle_id"]
        if vehicle in assigned_vehicles or left[site] < 1:
            continue
        if edge["requires_charging"] and left_charge[site] < 1:
            continue
        charging = edge["requires_charging"] or (
            left[site] == left_charge[site] and left_charge[site] > 0)
        left[site] -= 1
        if charging:
            left_charge[site] -= 1
        assigned_vehicles.add(vehicle)
        selected.append(edge)
    return selected
