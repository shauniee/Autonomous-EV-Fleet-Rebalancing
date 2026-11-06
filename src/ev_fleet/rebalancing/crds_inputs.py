"""Read-only CRDS observations; Fleet is the sole source of actual stock."""
import asyncio
from collections import Counter, defaultdict

from ev_fleet.contracts.crds import (CrdsDemandSnapshot, CrdsInputs, CrdsPlanningSnapshot,
                                   CrdsRelocationExclusion)
from ev_fleet.contracts.facilities import FacilitiesSnapshot
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.demand.aggregation import assigned_zone, capacity, telemetry_is_fresh
from ev_fleet.demand.settings import DemandSettings
from .clients import get_json


ACTIVE_JOURNEY_STATES = {"PENDING", "SENT", "ACKNOWLEDGED", "IN_PROGRESS", "ARRIVAL_PENDING"}


def journey_matches(dispatch, vehicle, fleet_id, zones):
    command, claim, telemetry = dispatch.command, vehicle.active_claim, vehicle.telemetry
    if not (command and claim and claim.status == "ACTIVE" and telemetry and command.route
            and dispatch.claim_held is True and claim.dispatch_id == dispatch.dispatch_id == command.dispatch_id
            and claim.command_id == command.command_id
            and claim.vehicle_id == vehicle.vehicle_id == dispatch.vehicle_id == command.vehicle_id
            and claim.fleet_instance_id == fleet_id == command.fleet_instance_id
            and claim.target_zone_id == command.target_zone_id and command.target_zone_id in zones
            and claim.target_position == command.target_position
            and dispatch.distance_travelled_km <= command.route.distance_km
            and (dispatch.parking_facility_id is None or dispatch.parking_status == "RESERVED")
            and not vehicle.active_rental and not vehicle.active_charging_task):
        return False
    if telemetry.active_dispatch_id == dispatch.dispatch_id:
        return telemetry.relocation_state in ("DISPATCHED", "RELOCATING", "ARRIVED")
    return (telemetry.active_dispatch_id is None and telemetry.relocation_state == "IDLE"
            and dispatch.state in ("PENDING", "SENT", "ACKNOWLEDGED"))


def isolate_relocations(fleet, dispatches, zones, stale_ids):
    by_vehicle = defaultdict(list)
    claim_counts = Counter(v.active_claim.dispatch_id for v in fleet.vehicles if v.active_claim)
    issues, excluded_sites, exclusions = set(), set(), []
    for dispatch in dispatches:
        identities = {dispatch.vehicle_id, dispatch.command.vehicle_id if dispatch.command else None} - {None}
        if not identities:
            issues.add("PENDING_DISPATCH_UNRESOLVED")
        for vehicle_id in identities:
            by_vehicle[vehicle_id].append(dispatch)
        if dispatch.parking_facility_id and (dispatch.parking_status is None
                or dispatch.claim_held is not True and dispatch.state not in ("FAILED", "REJECTED", "SUCCEEDED")):
            excluded_sites.add(dispatch.parking_facility_id)

    healthy = []
    for vehicle in fleet.vehicles:
        records = by_vehicle.pop(vehicle.vehicle_id, [])
        claim = vehicle.active_claim
        if not records and not claim:
            continue
        dispatch = records[0] if len(records) == 1 else None
        if vehicle.vehicle_id in stale_ids:
            reason = "TELEMETRY_UNAVAILABLE"
        elif dispatch and dispatch.state in ("FAILED", "REJECTED"):
            reason = "RELOCATION_FAILED"
        elif dispatch and (dispatch.state == "UNKNOWN" or dispatch.recovery
                           and dispatch.recovery.status in ("REQUESTED", "RESTORING", "BLOCKED")):
            reason = "RELOCATION_UNCONFIRMED"
        elif (dispatch and claim and claim_counts[claim.dispatch_id] == 1
              and dispatch.state in ACTIVE_JOURNEY_STATES
              and journey_matches(dispatch, vehicle, fleet.instance_id, zones)):
            healthy.append(dispatch)
            continue
        else:
            reason = "RELOCATION_OWNERSHIP_UNCONFIRMED"
        exclusions.append(CrdsRelocationExclusion(vehicle_id=vehicle.vehicle_id, reason=reason,
            dispatch_ids=sorted({d.dispatch_id for d in records})))

    for vehicle_id, records in by_vehicle.items():
        exclusions.append(CrdsRelocationExclusion(vehicle_id=vehicle_id, reason="VEHICLE_MISSING",
            dispatch_ids=sorted({d.dispatch_id for d in records})))
    return healthy, sorted(exclusions, key=lambda row: row.vehicle_id), excluded_sites, issues


async def collect_inputs(client, settings, dispatch_url, clock, traffic, *, timeout_seconds=12,
                         exclude_dispatch_id=None):
    reads = [f"{settings.fleet_url}/api/v1/vehicles",
             f"{settings.demand_url}/api/v1/zones",
             f"{settings.facilities_url}/api/v1/facilities",
             f"{dispatch_url}/api/v1/dispatch-planning-state"]
    raw = await asyncio.wait_for(
        asyncio.gather(*(get_json(client, url) for url in reads)), timeout_seconds)
    fleet = FleetSnapshot.model_validate(raw[0])
    demand = CrdsDemandSnapshot.model_validate(raw[1])
    facilities = FacilitiesSnapshot.model_validate(raw[2])
    planning = CrdsPlanningSnapshot.model_validate(raw[3])
    now = clock.now()
    policy = DemandSettings.from_env()
    policy = policy.model_copy(update={"min_reserve_pct": settings.min_reserve_pct,
        "telemetry_max_age_seconds": min(policy.telemetry_max_age_seconds, settings.stale_seconds)})
    stale_ids = {v.vehicle_id for v in fleet.vehicles
                 if not telemetry_is_fresh(v, fleet, policy, now)}
    zones = {z.zone_id: z for z in demand.zones}
    healthy, relocation_exclusions, excluded_sites, issues = isolate_relocations(
        fleet, [d for d in planning.dispatches if str(d.dispatch_id) != exclude_dispatch_id], zones, stale_ids)
    excluded = stale_ids | {row.vehicle_id for row in relocation_exclusions}
    # Count origin stock only after filtering. Healthy inbound EVs already
    # have a Fleet claim, so capacity() does not count them at their origin.
    fleet = fleet.model_copy(update={"vehicles": [v for v in fleet.vehicles if v.vehicle_id not in excluded]})
    planning = planning.model_copy(update={"dispatches": healthy})
    counts, _, capacity_issues = capacity(fleet, zones, policy, now)
    issues.update(capacity_issues)
    # Dispatch blocks overlapping approvals while acquisition is unresolved.
    # Keep the affected site out of planning until ownership is reconciled.
    facilities = facilities.model_copy(update={"facilities": [
        site for site in facilities.facilities if site.facility_id not in excluded_sites]})
    if not -5 <= (now - fleet.snapshot_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
        issues.add("FLEET_SNAPSHOT_STALE")
    if not -5 <= (now - planning.observed_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
        issues.add("DISPATCH_SNAPSHOT_STALE")
    for zone in zones.values():
        if zone.demand is None or not -5 <= (now - zone.demand.observed_at).total_seconds() < policy.demand_max_age_seconds:
            issues.add("DEMAND_UNKNOWN_OR_STALE")
    if facilities.data_source == "LTA_DATAMALL" and not any(
            f.name == "parking" and f.status == "FRESH" for f in facilities.feeds):
        issues.add("PARKING_FEED_UNAVAILABLE")
    vehicle_zones = {v.vehicle_id: zone for v in fleet.vehicles
                     if (zone := assigned_zone(v, zones, policy.zone_radius_km)) is not None}
    return CrdsInputs(
        evaluated_at=now, fleet=fleet, demand=demand, facilities=facilities, planning=planning,
        traffic=traffic, available_by_zone={z: row["available_evs"] for z, row in counts.items()},
        vehicle_zones=vehicle_zones, excluded_vehicle_ids=sorted(excluded),
        relocation_exclusions=relocation_exclusions,
        excluded_facility_ids=sorted(excluded_sites), issues=sorted(issues))
