"""Read-only CRDS observations; Fleet is the sole source of actual stock."""
import asyncio
from collections import Counter, defaultdict
from typing import TypeVar

import httpx
from fastapi import HTTPException

from ev_fleet.common.clock import Clock
from ev_fleet.common.settings import Settings
from ev_fleet.contracts.crds import (CrdsDemandSnapshot, CrdsInputs, CrdsPlanningSnapshot,
                                   CrdsRelocationExclusion)
from ev_fleet.contracts.facilities import FacilitiesSnapshot
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.demand.aggregation import capacity, demand_is_current, telemetry_is_fresh, vehicle_placements
from ev_fleet.demand.clients import parking_snapshot
from ev_fleet.demand.settings import DemandSettings
from .clients import get_json


_Snapshot = TypeVar("_Snapshot", FleetSnapshot, CrdsPlanningSnapshot)

ACTIVE_JOURNEY_STATES = {"PENDING", "SENT", "ACKNOWLEDGED", "IN_PROGRESS", "ARRIVAL_PENDING"}


async def selected_vehicles_are_current(
    client: httpx.AsyncClient, settings: Settings, clock: Clock, original: FleetSnapshot,
    vehicle_ids: set[str], deadline: float,
) -> bool:
    """Confirm that newer heartbeats still support the frozen route and score."""
    policy = DemandSettings.from_env()
    policy.telemetry_max_age_seconds = min(policy.telemetry_max_age_seconds, settings.stale_seconds)
    selected = {v.vehicle_id: v for v in original.vehicles if v.vehicle_id in vehicle_ids}
    if selected.keys() != vehicle_ids:
        return False
    now = clock.now()
    if all(telemetry_is_fresh(v, original, policy, now) for v in selected.values()):
        return True

    remaining = deadline - clock.monotonic()
    if remaining <= 0:
        return False
    try:
        raw = await asyncio.wait_for(get_json(client, f"{settings.fleet_url}/api/v1/vehicles"),
                                     min(remaining, policy.fleet_timeout_seconds))
        current = FleetSnapshot.model_validate(raw)
    except (asyncio.TimeoutError, HTTPException, httpx.HTTPError, ValueError):
        return False

    now = clock.now()
    if (current.instance_id != original.instance_id or current.snapshot_version < original.snapshot_version
            or not -5 <= (now - current.snapshot_at).total_seconds() < policy.fleet_snapshot_max_age_seconds):
        return False
    latest = {v.vehicle_id: v for v in current.vehicles}
    for vehicle_id, before in selected.items():
        after = latest.get(vehicle_id)
        if (after is None or before.telemetry is None or after.telemetry is None
                or not telemetry_is_fresh(after, current, policy, now)
                or after.effective_availability != "AVAILABLE"
                or after.control_version != before.control_version
                or after.current_zone_id != before.current_zone_id
                or after.battery_capacity_kwh != before.battery_capacity_kwh
                or after.consumption_kwh_per_km != before.consumption_kwh_per_km
                or after.telemetry.position != before.telemetry.position
                or after.telemetry.soc_pct != before.telemetry.soc_pct):
            return False
    return True


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
    policy = DemandSettings.from_env()
    reads = [f"{settings.fleet_url}/api/v1/vehicles",
             f"{settings.demand_url}/api/v1/zones?include_fixed_targets=true",
             f"{settings.facilities_url}/api/v1/facilities",
             f"{dispatch_url}/api/v1/dispatch-planning-state"]

    deadline = clock.monotonic() + timeout_seconds

    async def read_inputs():
        raw = await asyncio.gather(*(get_json(client, url) for url in reads))
        fleet = FleetSnapshot.model_validate(raw[0])
        demand = CrdsDemandSnapshot.model_validate(raw[1])
        facilities = FacilitiesSnapshot.model_validate(raw[2])
        planning = CrdsPlanningSnapshot.model_validate(raw[3])
        # A full Facilities catalogue may outlast the small snapshots' freshness
        # window. Read those again once after assembling the catalogue, using
        # the original deadline. Never relabel an old snapshot as fresh.
        now = clock.now()
        remaining = min(policy.fleet_timeout_seconds, deadline - clock.monotonic())

        async def refresh(url: str, model: type[_Snapshot], original: _Snapshot) -> _Snapshot:
            if remaining <= 0:
                return original
            try:
                data = await asyncio.wait_for(get_json(client, url), remaining)
                return model.model_validate(data)
            except (asyncio.TimeoutError, HTTPException, httpx.HTTPError, ValueError):
                return original  # The unchanged freshness checks still defer.

        async def current_fleet() -> FleetSnapshot:
            if -5 <= (now - fleet.snapshot_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
                return fleet
            return await refresh(reads[0], FleetSnapshot, fleet)

        async def current_planning() -> CrdsPlanningSnapshot:
            if -5 <= (now - planning.observed_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
                return planning
            return await refresh(reads[3], CrdsPlanningSnapshot, planning)

        fleet, planning = await asyncio.gather(current_fleet(), current_planning())
        parking = None
        if any(v.telemetry and v.telemetry.parked_facility_id and not v.stale
               for v in fleet.vehicles):
            parking = await parking_snapshot(client, settings.facilities_url, policy.fleet_timeout_seconds)
        return fleet, demand, facilities, planning, parking

    fleet, demand, facilities, planning, parking = await asyncio.wait_for(read_inputs(), timeout_seconds)
    now = clock.now()
    policy = policy.model_copy(update={"min_reserve_pct": settings.min_reserve_pct,
        "telemetry_max_age_seconds": min(policy.telemetry_max_age_seconds, settings.stale_seconds)})
    stale_ids = {v.vehicle_id for v in fleet.vehicles
                 if not telemetry_is_fresh(v, fleet, policy, now)}
    zones = {z.zone_id: z for z in demand.zones}
    healthy, relocation_exclusions, excluded_sites, issues = isolate_relocations(
        fleet, [d for d in planning.dispatches if str(d.dispatch_id) != exclude_dispatch_id], zones, stale_ids)
    telemetry_exclusions = {
        v.vehicle_id: ("NO_TELEMETRY" if v.telemetry is None else
                       "OFFLINE" if v.connectivity != "ONLINE" else "STALE_TELEMETRY")
        for v in fleet.vehicles if v.vehicle_id in stale_ids
    }
    excluded = stale_ids | {row.vehicle_id for row in relocation_exclusions}
    # Count origin stock only after filtering. Healthy inbound EVs already
    # have a Fleet claim, so capacity() does not count them at their origin.
    fleet = fleet.model_copy(update={"vehicles": [v for v in fleet.vehicles if v.vehicle_id not in excluded]})
    planning = planning.model_copy(update={"dispatches": healthy})
    placements = vehicle_placements(fleet, zones, policy, now, parking)
    counts, _, capacity_issues = capacity(fleet, zones, policy, now, placements)
    issues.update(capacity_issues)
    # Dispatch blocks overlapping approvals while acquisition is unresolved.
    # Keep the affected site out of planning until ownership is reconciled.
    withheld = [site for site in facilities.facilities if site.facility_id in excluded_sites]
    facilities = facilities.model_copy(update={"facilities": [
        site for site in facilities.facilities if site.facility_id not in excluded_sites]})
    if not -5 <= (now - fleet.snapshot_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
        issues.add("FLEET_SNAPSHOT_STALE")
    if not -5 <= (now - planning.observed_at).total_seconds() < policy.fleet_snapshot_max_age_seconds:
        issues.add("DISPATCH_SNAPSHOT_STALE")
    fixed_targets = set(demand.fixed_demand_zone_ids)
    for zone in zones.values():
        if not demand_is_current(zone, policy, now, fixed=zone.zone_id in fixed_targets):
            issues.add("DEMAND_UNKNOWN_OR_STALE")
    if facilities.data_source == "LTA_DATAMALL" and not any(
            f.name == "parking" and f.status == "FRESH" for f in facilities.feeds):
        issues.add("PARKING_FEED_UNAVAILABLE")
    vehicle_zones = {vehicle_id: placement.zone_id for vehicle_id, placement in placements.items()
                     if placement.zone_id is not None}
    return CrdsInputs(
        evaluated_at=now, fleet=fleet, demand=demand, facilities=facilities, planning=planning,
        traffic=traffic, available_by_zone={z: row["available_evs"] for z, row in counts.items()},
        vehicle_zones=vehicle_zones, excluded_vehicle_ids=sorted(excluded),
        relocation_exclusions=relocation_exclusions,
        excluded_facility_ids=sorted(excluded_sites), issues=sorted(issues),
        telemetry_exclusions=telemetry_exclusions, withheld_facilities=withheld)
