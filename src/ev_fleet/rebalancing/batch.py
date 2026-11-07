"""Deterministic global-greedy EV-to-mock-facility matcher.

Not a guaranteed globally optimal matching. It observes mock capacity but
does not reserve space until a dispatcher explicitly approves.
"""
from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT, CHARGING_TARGET_SOC_PCT
from ev_fleet.rebalancing.policy import assess
from ev_fleet.rebalancing.traffic import assess_traffic, rank_cost

MIN_TRAFFIC_CANDIDATES = 8
TRAFFIC_CANDIDATE_HEADROOM = 6


def _baseline_key(edge, purpose):
    return (
        edge["assessment"]["current_soc_pct"] if purpose == "CHARGE" else 0,
        -edge["score"], -edge["predicted_arrival_soc_pct"],
        edge["distance_km"], edge["vehicle_id"], edge["facility_id"],
    )


def _greedy_select(edges, quantity, capacity, charging_capacity):
    left = capacity.copy()
    left_charge = charging_capacity.copy()
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


def rank_pairs(fleet, zone, facilities, policy, now, quantity, purpose="REBALANCE", vehicle_id=None,
               facility_id=None, traffic=None):
    edges = []
    left = {}
    left_charge = {}
    for site in facilities:
        if facility_id and site.facility_id != facility_id:
            continue
        if (site.zone_id != zone["zone_id"] or site.status != "OPEN"
                or not site.is_fresh(now) or site.parking_available < 1):
            continue
        left[site.facility_id] = site.parking_available
        left_charge[site.facility_id] = site.charging_available
        target = {**zone, "position": site.position.model_dump(mode="json"),
                  "charging": {"observed_at": now.isoformat(),
                               "available_slots": site.charging_available}}
        for vehicle in fleet["vehicles"]:
            if vehicle_id and vehicle["vehicle_id"] != vehicle_id:
                continue
            assessment = assess(vehicle, target, policy, now)
            if not assessment["eligible"]:
                continue
            if purpose == "CHARGE" and assessment["current_soc_pct"] >= CHARGING_TARGET_SOC_PCT:
                continue
            need_charge = purpose == "CHARGE" or assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
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
                "traffic_assessment": None, "traffic_scored": False,
                "origin": vehicle["telemetry"]["position"],
            })
    if traffic and traffic.get("source") in ("LTA_DATAMALL", "SIMULATION"):
        # Live feeds can expose hundreds of parking sites. Scoring every
        # EV-to-site pair against the full road index can monopolise the API
        # worker long enough for Kubernetes to kill it. First rank the cheap
        # baseline, then traffic-check a bounded shortlist for each EV.
        ordered = sorted(edges, key=lambda edge: _baseline_key(edge, purpose))
        shortlisted = _greedy_select(ordered, quantity, left, left_charge)
        selected_keys = {(edge["vehicle_id"], edge["facility_id"])
                         for edge in shortlisted}
        represented = {edge["vehicle_id"] for edge in shortlisted}
        limit = max(MIN_TRAFFIC_CANDIDATES, quantity + TRAFFIC_CANDIDATE_HEADROOM)
        # Prefer another EV's best baseline option before spending the bounded
        # budget on alternate parking sites for an already represented EV.
        for edge in ordered:
            key = (edge["vehicle_id"], edge["facility_id"])
            if len(shortlisted) == limit:
                break
            if key not in selected_keys and edge["vehicle_id"] not in represented:
                shortlisted.append(edge)
                selected_keys.add(key)
                represented.add(edge["vehicle_id"])
        for edge in ordered:
            key = (edge["vehicle_id"], edge["facility_id"])
            if len(shortlisted) == limit:
                break
            if key not in selected_keys:
                shortlisted.append(edge)
                selected_keys.add(key)
        edges = []
        for edge in shortlisted:
            travel_seconds = 3600 * edge["distance_km"] / 30
            evaluation = assess_traffic([edge["origin"], edge["target_position"]],
                edge["distance_km"], travel_seconds, traffic, now)
            if evaluation["blocked"]:
                continue
            edge["traffic_assessment"] = evaluation
            cost = rank_cost(evaluation, edge["assessment"]["energy_required_kwh"])
            if cost is not None and evaluation["status"] == "FRESH":
                edge["score"] = 1 - cost
                edge["traffic_scored"] = True
            edges.append(edge)
    for edge in edges:
        edge.pop("origin", None)
    if any(edge["traffic_scored"] for edge in edges):
        # Traffic-aware score differs from baseline only for observed roads.
        # Prefer observed candidates; unobserved alternatives are explicitly
        # uncertain rather than assumed free-flow.
        edges.sort(key=lambda e: (not e["traffic_scored"], -e["score"],
                                  e["distance_km"], e["vehicle_id"], e["facility_id"]))
    else:
        # Preserve the original low-SOC-first CHARGE policy whenever traffic is
        # disabled, missing, stale or covers none of the candidate roads.
        edges.sort(key=lambda edge: _baseline_key(edge, purpose))
    return _greedy_select(edges, quantity, left, left_charge)
