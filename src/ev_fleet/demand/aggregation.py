"""Pure, read-only composition of demand and one authoritative Fleet snapshot."""
from collections.abc import Mapping
from datetime import datetime

from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.facilities import VehicleParking
from ev_fleet.contracts.heatmap import ConfirmedParking, VehiclePlacement
from ev_fleet.contracts.vehicles import FleetSnapshot, VehicleState

from .settings import DemandSettings


# Match the simulator's parking access-point tolerance.
PARKED_POSITION_TOLERANCE_KM = .05
COUNT_FIELDS = ("available_evs", "inbound_relocating_evs", "inbound_pending_evs",
                "inbound_unconfirmed_evs", "arrived_pending_evs", "excluded_stale_evs", "excluded_low_soc_evs")
BALANCE_FIELDS = ("current_balance", "current_shortage", "current_surplus", "projected_supply",
                  "projected_balance", "projected_shortage", "projected_surplus", "shortage_ratio")


def balances(demand, available, inbound, critical_ratio=.5):
    current = demand - available
    projected_supply = available + inbound
    projected = demand - projected_supply
    shortage = max(current, 0)
    ratio = shortage / demand if demand else 0.0
    severity = "NORMAL" if not shortage else "CRITICAL" if ratio >= critical_ratio else "WARNING"
    return {"current_balance": current, "current_shortage": shortage, "current_surplus": max(-current, 0),
            "projected_supply": projected_supply, "projected_balance": projected,
            "projected_shortage": max(projected, 0), "projected_surplus": max(-projected, 0),
            "shortage_ratio": ratio, "hotspot_severity": severity}


def demand_level(value, settings):
    return "HIGH" if value >= settings.high_demand else "MEDIUM" if value >= settings.medium_demand else "LOW"


def assigned_zone(vehicle: VehicleState, zones: Mapping[str, ZoneState],
                  radius: float) -> str | None:
    if vehicle.telemetry is None:
        return None
    position = vehicle.telemetry.position.model_dump()
    distances = {key: distance_km(position, zone.position.model_dump()) for key, zone in zones.items()}
    current = vehicle.current_zone_id
    if current is not None and current in distances and distances[current] <= radius:
        return current
    candidates = [(distance, key) for key, distance in distances.items() if distance <= radius]
    return min(candidates)[1] if candidates else None


def telemetry_is_fresh(vehicle: VehicleState, fleet: FleetSnapshot,
                       settings: DemandSettings, now: datetime) -> bool:
    """Use Fleet freshness and recheck observation age after snapshot transport."""
    telemetry = vehicle.telemetry
    if telemetry is None:
        return False
    snapshot_age = max(0, (now - fleet.snapshot_at).total_seconds())
    age = (now - telemetry.observed_at).total_seconds()
    receive_age = (now - vehicle.received_at).total_seconds() if vehicle.received_at else None
    return (not vehicle.stale and vehicle.connectivity == "ONLINE"
            and -5 <= age < settings.telemetry_max_age_seconds
            and receive_age is not None and receive_age >= -5
            and vehicle.telemetry_age_seconds is not None
            and vehicle.telemetry_age_seconds + snapshot_age < settings.telemetry_max_age_seconds)


def vehicle_placements(
    fleet: FleetSnapshot, zones: Mapping[str, ZoneState], settings: DemandSettings,
    now: datetime, parking: Mapping[str, VehicleParking] | None,
) -> dict[str, VehiclePlacement]:
    placements = {}
    for vehicle in fleet.vehicles:
        vehicle_id = vehicle.vehicle_id
        allocation = parking.get(vehicle_id) if parking is not None else None
        site = allocation.parking if allocation is not None else None
        telemetry = vehicle.telemetry
        if (allocation and allocation.vehicle_id == vehicle_id and not allocation.pending
                and site and site.zone_id in zones and telemetry
                and telemetry_is_fresh(vehicle, fleet, settings, now)
                and telemetry.relocation_state == "IDLE" and telemetry.active_dispatch_id is None
                and not vehicle.active_claim and not vehicle.active_rental
                and telemetry.parked_facility_id == site.facility_id
                and distance_km(telemetry.position.model_dump(), site.position.model_dump())
                <= PARKED_POSITION_TOLERANCE_KM):
            placements[vehicle_id] = VehiclePlacement(
                zone_id=site.zone_id,
                parking=ConfirmedParking(facility_id=site.facility_id, name=site.name),
            )
        else:
            placements[vehicle_id] = VehiclePlacement(
                zone_id=assigned_zone(vehicle, zones, settings.zone_radius_km),
            )
    return placements


def capacity(
    fleet: FleetSnapshot, zones: Mapping[str, ZoneState], settings: DemandSettings,
    now: datetime, placements: Mapping[str, VehiclePlacement] | None = None,
):
    counts = {key: dict.fromkeys(COUNT_FIELDS, 0) for key in zones}
    diagnostics = {"unassigned_evs": 0, "excluded_stale_evs": 0,
                   "excluded_low_soc_evs": 0, "available_outside_zones": 0}
    issues = set()
    for vehicle in fleet.vehicles:
        placement = placements[vehicle.vehicle_id] if placements is not None else None
        zone_id = placement.zone_id if placement is not None else assigned_zone(
            vehicle, zones, settings.zone_radius_km)
        telemetry = vehicle.telemetry
        if zone_id is None:
            diagnostics["unassigned_evs"] += 1
        fresh = telemetry_is_fresh(vehicle, fleet, settings, now)
        if not fresh:
            diagnostics["excluded_stale_evs"] += 1
            if zone_id:
                counts[zone_id]["excluded_stale_evs"] += 1
        claim = vehicle.active_claim
        if claim:
            target = claim.target_zone_id
            if target not in zones:
                issues.add("UNKNOWN_CLAIM_TARGET")
                continue
            valid_claim = (claim.status == "ACTIVE" and claim.vehicle_id == vehicle.vehicle_id
                           and claim.fleet_instance_id == fleet.instance_id)
            if not fresh or telemetry is None or not valid_claim:
                counts[target]["inbound_unconfirmed_evs"] += 1
                continue
            matching = telemetry.active_dispatch_id == claim.dispatch_id
            if matching and telemetry.relocation_state == "RELOCATING":
                counts[target]["inbound_relocating_evs"] += 1
            elif matching and telemetry.relocation_state == "ARRIVED":
                counts[target]["arrived_pending_evs"] += 1
            elif telemetry.relocation_state in ("IDLE", "DISPATCHED") and (
                    telemetry.active_dispatch_id is None or matching):
                counts[target]["inbound_pending_evs"] += 1
            else:
                counts[target]["inbound_unconfirmed_evs"] += 1
            continue  # A claimed vehicle cannot also be available at its origin.
        if (fresh and telemetry is not None and telemetry.availability == "AVAILABLE"
                and vehicle.effective_availability == "AVAILABLE"
                and telemetry.relocation_state == "IDLE" and telemetry.active_dispatch_id is None):
            if telemetry.soc_pct < settings.min_reserve_pct:
                diagnostics["excluded_low_soc_evs"] += 1
                if zone_id:
                    counts[zone_id]["excluded_low_soc_evs"] += 1
                continue
            parking_unconfirmed = (placement is not None and placement.parking is None
                                   and telemetry.parked_facility_id is not None)
            if parking_unconfirmed:
                issues.add("PARKING_ASSIGNMENT_UNCONFIRMED")
            if zone_id:
                counts[zone_id]["available_evs"] += 1
            elif not parking_unconfirmed:
                diagnostics["available_outside_zones"] += 1
    return counts, diagnostics, issues


def compose_heatmap(demand, fleet, error, settings, now, *, parking=None):
    zones = {zone.zone_id: zone for zone in (ZoneState.model_validate(item) for item in demand["zones"])}
    issues = {error} if error else set()
    fleet_status = "INVALID" if error == "FLEET_INVALID_RESPONSE" else "UNAVAILABLE" if fleet is None else "FRESH"
    if fleet is not None:
        age = (now - fleet.snapshot_at).total_seconds()
        if not -5 <= age < settings.fleet_snapshot_max_age_seconds:
            fleet_status = "STALE"
            issues.add("FLEET_SNAPSHOT_STALE")
    usable = fleet is not None and fleet_status == "FRESH"
    placements = None
    if usable:
        placements = vehicle_placements(fleet, zones, settings, now, parking)
        counts, diagnostics, fleet_issues = capacity(fleet, zones, settings, now, placements)
        issues.update(fleet_issues)
        if diagnostics["unassigned_evs"]:
            issues.add("UNASSIGNED_VEHICLES")
        if diagnostics["excluded_stale_evs"]:
            issues.add("STALE_VEHICLES_EXCLUDED")
    else:
        counts = {key: dict.fromkeys(COUNT_FIELDS) for key in zones}
        diagnostics = dict.fromkeys(("unassigned_evs", "excluded_stale_evs", "excluded_low_soc_evs",
                                     "available_outside_zones"))
    result = []
    for key, zone in sorted(zones.items()):
        observation = zone.demand
        value = observation.desired_available_vehicles if observation else None
        fresh = observation is not None and -5 <= (now - observation.observed_at).total_seconds() < settings.demand_max_age_seconds
        status = "FRESH" if fresh else "STALE" if observation else "MISSING"
        if not fresh:
            issues.add(f"DEMAND_{status}:{key}")
        provenance = demand["provenance"][key]
        row = {"zone_id": key, "name": zone.name, "position": zone.position.model_dump(),
               "rental_demand": value, "demand_observed_at": observation.observed_at if observation else None,
               "demand_version": zone.demand_version, "demand_source": provenance["source"],
               "manual_hold_until": provenance["manual_hold_until"], "demand_status": status,
               "demand_level": demand_level(value, settings) if fresh else "UNKNOWN", **counts[key]}
        if fresh and usable:
            row.update(balances(value, row["available_evs"], row["inbound_relocating_evs"], settings.critical_shortage_ratio))
            row["data_quality"] = "DEGRADED" if row["excluded_stale_evs"] or row["inbound_unconfirmed_evs"] else "COMPLETE"
        else:
            row.update(dict.fromkeys(BALANCE_FIELDS))
            row.update(hotspot_severity="UNKNOWN", data_quality="UNKNOWN")
        if row["inbound_unconfirmed_evs"]:
            issues.add("UNCONFIRMED_INBOUND:" + key)
        result.append(row)
    return {"generated_at": now, "status": "PARTIAL" if issues else "COMPLETE", "issues": sorted(issues),
            "calculation_version": "zone-capacity-v2", "vehicle_placements": placements,
            "policy": {key: getattr(settings, key) for key in ("zone_radius_km", "min_reserve_pct",
                "critical_shortage_ratio", "medium_demand", "high_demand", "demand_max_age_seconds",
                "fleet_snapshot_max_age_seconds", "telemetry_max_age_seconds")},
            "demand_instance_id": demand["instance_id"], "demand_snapshot_at": demand["snapshot_at"],
            "fleet_instance_id": fleet.instance_id if fleet else None,
            "fleet_snapshot_version": fleet.snapshot_version if fleet else None,
            "fleet_snapshot_at": fleet.snapshot_at if fleet else None, "fleet_status": fleet_status,
            **diagnostics, "zones": result}
