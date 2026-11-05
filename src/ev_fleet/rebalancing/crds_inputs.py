"""Read-only CRDS observations; Fleet is the sole source of actual stock."""
import asyncio

from ev_fleet.contracts.crds import CrdsDemandSnapshot, CrdsInputs, CrdsPlanningSnapshot
from ev_fleet.contracts.facilities import FacilitiesSnapshot
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.demand.aggregation import assigned_zone, capacity, telemetry_is_fresh
from ev_fleet.demand.settings import DemandSettings
from .clients import get_json


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
    excluded = {v.vehicle_id for v in fleet.vehicles
                if not telemetry_is_fresh(v, fleet, policy, now)}
    # Unreachable EVs contribute neither usable stock nor predicted arrivals or
    # returns. Their retained claims remain untouched in the owning services.
    fleet = fleet.model_copy(update={"vehicles": [
        v for v in fleet.vehicles if v.vehicle_id not in excluded]})
    planning = planning.model_copy(update={"dispatches": [
        d for d in planning.dispatches
        if str(d.dispatch_id) != exclude_dispatch_id and d.vehicle_id not in excluded]})
    zones = {z.zone_id: z for z in demand.zones}
    counts, _, issues = capacity(fleet, zones, policy, now)
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
    dispatches = {str(d.dispatch_id): d for d in planning.dispatches}
    for vehicle in fleet.vehicles:
        claim = vehicle.active_claim
        if claim and str(claim.dispatch_id) != exclude_dispatch_id:
            dispatch = dispatches.get(str(claim.dispatch_id))
            if (dispatch is None or dispatch.command is None
                    or dispatch.vehicle_id != vehicle.vehicle_id
                    or claim.fleet_instance_id != fleet.instance_id
                    or dispatch.command.fleet_instance_id != fleet.instance_id
                    or dispatch.command.target_zone_id != claim.target_zone_id):
                issues.add("CLAIM_DISPATCH_MISMATCH")
    if any(d.command is None for d in planning.dispatches):
        issues.add("PENDING_DISPATCH_UNRESOLVED")
    vehicle_zones = {v.vehicle_id: zone for v in fleet.vehicles
                     if (zone := assigned_zone(v, zones, policy.zone_radius_km)) is not None}
    return CrdsInputs(
        evaluated_at=now, fleet=fleet, demand=demand, facilities=facilities, planning=planning,
        traffic=traffic, available_by_zone={z: row["available_evs"] for z, row in counts.items()},
        vehicle_zones=vehicle_zones, excluded_vehicle_ids=sorted(excluded), issues=sorted(issues))
