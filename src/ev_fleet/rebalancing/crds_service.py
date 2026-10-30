"""CRDS recommendation orchestration over the existing read APIs."""
import asyncio
import hashlib
import json
from math import fsum
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import HTTPException

from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT, CHARGING_TARGET_SOC_PCT
from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.crds import (CrdsEvaluation, CrdsMetadata, CrdsMove,
                                   CrdsMoveOutcome, CrdsRouteEstimate, CrdsRouteRequest)
from .crds import apply_move, shortage_by_zone
from .crds_execution import evaluate_candidates
from .crds_futures import MODEL_VERSION, build_futures, parameters_for
from .crds_inputs import collect_inputs
from .crds_routes import travel_minutes
from .policy import assess
from .traffic import assess_traffic


@dataclass
class CrdsPlan:
    inputs: object
    evaluation: CrdsEvaluation
    futures: list
    moves: list
    assessments: dict
    sites: dict
    metadata: CrdsMetadata
    deadline: float = float("inf")


async def plan_one(client, policy, settings, clock, traffic, target_zone_id, *,
                   vehicle_id=None, facility_id=None, route_override=None, exclude_dispatch_id=None,
                   expand_age=True):
    start = clock.monotonic()
    inputs = await collect_inputs(client, policy, settings.dispatch_url, clock, traffic,
                                  timeout_seconds=settings.deadline_seconds,
                                  exclude_dispatch_id=exclude_dispatch_id)
    target = next((z for z in inputs.demand.zones if z.zone_id == target_zone_id), None)
    if target is None:
        raise HTTPException(404, "TARGET_NOT_FOUND")
    params = parameters_for(settings)
    fingerprint = hashlib.sha256(json.dumps(
        {"inputs": inputs.model_dump(mode="json"), "settings": settings.model_dump(),
         "model": MODEL_VERSION, "expand_age": expand_age}, sort_keys=True).encode()).hexdigest()
    metadata = CrdsMetadata(decision="DEFER", reason_code="INCOMPLETE_INPUTS", parameters=params,
        scenario_model_version=MODEL_VERSION, scenario_count=0, epoch_id=uuid4(), input_fingerprint=fingerprint,
        source_references={"fleet_instance_id": str(inputs.fleet.instance_id),
            "fleet_version": inputs.fleet.snapshot_version, "demand_instance_id": str(inputs.demand.instance_id),
            "demand_versions": {z.zone_id: z.demand_version for z in inputs.demand.zones},
            "planning_instance_id": str(inputs.planning.instance_id), "planning_revision": inputs.planning.revision,
            "traffic_instance_id": traffic.get("instance_id"), "traffic_version": traffic.get("scenario_version"),
            "traffic_mode": traffic.get("source"), "evaluated_at": inputs.evaluated_at.isoformat()})
    empty = CrdsPlan(inputs, CrdsEvaluation(decision="DEFER", reason_code="INCOMPLETE_INPUTS"),
                     [], [], {}, {}, metadata, start + settings.deadline_seconds)
    if inputs.issues:
        metadata.reason_code = ",".join(inputs.issues)
        empty.evaluation.reason_code = metadata.reason_code
        return empty
    try:
        futures = build_futures(inputs, target_zone_id, settings, expand_age=expand_age)
    except ValueError as exc:
        metadata.reason_code = str(exc)
        return empty
    metadata.scenario_count = len(futures)
    reserved = {d.vehicle_id for d in inputs.planning.dispatches}
    assessments, sites, candidates = {}, {}, []
    for vehicle in sorted(inputs.fleet.vehicles, key=lambda v: v.vehicle_id):
        if (vehicle_id and vehicle.vehicle_id != vehicle_id) or vehicle.vehicle_id in reserved:
            continue
        choices = []
        for site in inputs.facilities.facilities:
            if (site.zone_id != target_zone_id or site.status != "OPEN" or site.parking_available < 1
                    or not site.is_fresh(inputs.evaluated_at) or (facility_id and site.facility_id != facility_id)):
                continue
            assessed_target = target.model_dump(mode="json")
            assessed_target.update(position=site.position.model_dump(mode="json"), charging={
                "observed_at": inputs.evaluated_at.isoformat(), "available_slots": site.charging_available})
            assessment = assess(vehicle.model_dump(mode="json"), assessed_target, policy, inputs.evaluated_at)
            if not assessment["eligible"]:
                continue
            needs_charge = assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
            if (needs_charge and site.charging_available < 1) or (site.facility_type == "CHARGING" and not needs_charge):
                continue
            choices.append((assessment["distance_km"], site.facility_id, site, assessment))
        if choices:
            _, _, site, assessment = min(choices, key=lambda x: (x[0], x[1]))
            candidates.append((vehicle, site, assessment))
    semaphore = asyncio.Semaphore(4)

    async def prepare(candidate):
        vehicle, site, assessment = candidate
        async with semaphore:
            if route_override is not None:
                route = route_override
                if route.source != "ONEMAP" or distance_km(
                        vehicle.telemetry.position.model_dump(), route.geometry[0].model_dump()) > .005:
                    raise ValueError("ROUTE_ORIGIN_CHANGED")
            else:
                body = CrdsRouteRequest(vehicle_id=vehicle.vehicle_id, facility_id=site.facility_id,
                    expected_fleet_instance_id=inputs.fleet.instance_id,
                    expected_control_version=vehicle.control_version)
                response = await client.post(settings.dispatch_url + "/api/v1/route-estimates",
                    json=body.model_dump(mode="json"), timeout=settings.deadline_seconds)
                response.raise_for_status()
                estimate = CrdsRouteEstimate.model_validate(response.json())
                if (estimate.vehicle_id != vehicle.vehicle_id or estimate.facility_id != site.facility_id
                        or estimate.fleet_instance_id != inputs.fleet.instance_id
                        or estimate.control_version != vehicle.control_version
                        or estimate.origin != vehicle.telemetry.position):
                    raise ValueError("ROUTE_ESTIMATE_IDENTITY_CHANGED")
                route = estimate.route
            evaluation = assess_traffic([p.model_dump() for p in route.geometry],
                route.distance_km, route.duration_seconds, traffic, inputs.evaluated_at)
            if evaluation["blocked"]:
                return None
            energy = route.distance_km * vehicle.consumption_kwh_per_km * policy.energy_margin_factor
            arrival_soc = vehicle.telemetry.soc_pct - energy / vehicle.battery_capacity_kwh * 100
            if arrival_soc < policy.min_reserve_pct:
                return None
            need_charge = arrival_soc < CHARGE_ON_ARRIVAL_BELOW_PCT
            if need_charge and (site.charging_available < 1 or site.charging_power_kw is None):
                raise ValueError("CHARGING_AVAILABILITY_UNKNOWN")
            if site.facility_type == "CHARGING" and not need_charge:
                return None
            availability_delay = (max(0, CHARGING_TARGET_SOC_PCT - arrival_soc) / 100 *
                                  vehicle.battery_capacity_kwh / site.charging_power_kw * 60) if need_charge else 0
            minutes = travel_minutes(route, traffic, inputs.evaluated_at)
            origin = inputs.vehicle_zones.get(vehicle.vehicle_id)
            if origin is None:
                raise ValueError("UNKNOWN_MOVE_ORIGIN")
            outcomes = {}
            for future in futures:
                extra = settings.journey_delay_minutes if future.future_id in ("combined", "delayed_inbound") else 0
                if future.future_id.startswith("age_"):
                    extra = settings.journey_delay_minutes * int(future.future_id.split("_")[1])
                outcomes[future.future_id] = CrdsMoveOutcome(arrival_minutes=minutes + extra,
                    availability_delay_minutes=availability_delay, energy_required_kwh=energy)
            assessment.update(score=None, utilities=None, weighted_components=None,
                distance_km=route.distance_km, energy_required_kwh=energy,
                predicted_arrival_soc_pct=arrival_soc, traffic_assessment=evaluation)
            return CrdsMove(vehicle_id=vehicle.vehicle_id, facility_id=site.facility_id,
                            origin_zone_id=origin, target_zone_id=target_zone_id, outcomes=outcomes), assessment, site

    remaining = settings.deadline_seconds - (clock.monotonic() - start)
    if remaining <= 0:
        metadata.reason_code = "DECISION_DEADLINE"
        return empty
    try:
        prepared = await asyncio.wait_for(asyncio.gather(*(prepare(c) for c in candidates)), remaining)
    except (asyncio.TimeoutError, httpx.HTTPError, ValueError) as exc:
        metadata.reason_code = "ROUTE_EVIDENCE_UNAVAILABLE" if not isinstance(exc, ValueError) else str(exc)
        return empty
    moves = []
    for item in prepared:
        if item is not None:
            move, assessment, site = item
            moves.append(move)
            assessments[move.vehicle_id] = assessment
            sites[(move.vehicle_id, move.facility_id)] = site
    metadata.input_fingerprint = hashlib.sha256(json.dumps({
        "observations": fingerprint, "futures": [f.model_dump(mode="json") for f in futures],
        "moves": [m.model_dump(mode="json") for m in moves]}, sort_keys=True).encode()).hexdigest()
    result, metadata.execution = await evaluate_candidates(client, settings, metadata.epoch_id,
        futures, moves, params, timeout_seconds=start + settings.deadline_seconds - clock.monotonic())
    if result.selected_vehicle_id:
        vehicle = next(v for v in inputs.fleet.vehicles if v.vehicle_id == result.selected_vehicle_id)
        if (clock.now() - vehicle.telemetry.observed_at).total_seconds() >= policy.stale_seconds:
            result = CrdsEvaluation(decision="DEFER", reason_code="STALE_AFTER_ROUTE_LOOKUP")
    metadata.decision, metadata.reason_code = result.decision, result.reason_code
    metadata.approval_allowed = settings.mode == "ACTIVE" and result.decision == "MOVE"
    if result.decision == "MOVE":
        winner = result.ranked_candidates[0]
        move = next(m for m in moves if m.vehicle_id == winner.vehicle_id and m.facility_id == winner.facility_id)
        metadata.worst_case_gain_vehicle_minutes = winner.worst_case_gain_vehicle_minutes
        metadata.worst_case_future_id = winner.worst_case_future_id
        metadata.shortage_changes_by_zone = winner.shortage_changes_by_zone
        metadata.arrival_minutes_min = min(o.arrival_minutes for o in move.outcomes.values())
        metadata.arrival_minutes_max = max(o.arrival_minutes for o in move.outcomes.values())
    return CrdsPlan(inputs, result, futures, moves, assessments, sites, metadata, start + settings.deadline_seconds)


async def plan_batch(plan, quantity, client, settings, clock):
    """Greedy supported prefix; all steps use the same complete future set."""
    if plan.metadata.decision == "DEFER":
        return [], plan.metadata, plan.metadata.reason_code
    params = plan.metadata.parameters
    futures = plan.futures
    remaining = list(plan.moves)
    parking = {s.facility_id: s.parking_available for s in plan.sites.values()}
    chargers = {s.facility_id: s.charging_available for s in plan.sites.values()}
    children = []
    executions = [plan.metadata.execution]
    stop_reason = "REQUESTED_QUANTITY_REACHED"
    while len(children) < quantity:
        eligible = [m for m in remaining if parking[m.facility_id] > 0 and
                    (plan.assessments[m.vehicle_id]["predicted_arrival_soc_pct"] >= CHARGE_ON_ARRIVAL_BELOW_PCT
                     or chargers[m.facility_id] > 0) and
                    all(min(f.zones[m.origin_zone_id].usable_stock) >= 1 for f in futures)]
        result, execution = await evaluate_candidates(client, settings, plan.metadata.epoch_id,
            futures, eligible, params, timeout_seconds=plan.deadline - clock.monotonic())
        executions.append(execution)
        if result.decision == "DEFER":
            metadata = plan.metadata.model_copy(deep=True)
            metadata.decision, metadata.reason_code, metadata.approval_allowed = "DEFER", result.reason_code, False
            metadata.execution = execution_summary(executions, settings.execution_mode)
            return [], metadata, result.reason_code
        if result.decision != "MOVE":
            stop_reason = "CAPACITY_OR_STOCK_LIMIT" if not eligible and remaining else result.reason_code
            break
        gain = result.ranked_candidates[0]
        move = next(m for m in eligible if (m.vehicle_id, m.facility_id) == (gain.vehicle_id, gain.facility_id))
        metadata = plan.metadata.model_copy(deep=True)
        metadata.execution = execution
        metadata.decision, metadata.reason_code = "MOVE", "USEFUL_SHORTAGE_REDUCTION"
        metadata.worst_case_gain_vehicle_minutes = gain.worst_case_gain_vehicle_minutes
        metadata.worst_case_future_id = gain.worst_case_future_id
        metadata.shortage_changes_by_zone = gain.shortage_changes_by_zone
        metadata.arrival_minutes_min = min(o.arrival_minutes for o in move.outcomes.values())
        metadata.arrival_minutes_max = max(o.arrival_minutes for o in move.outcomes.values())
        metadata.source_references["virtual_prefix"] = [c.evaluation.selected_vehicle_id for c in children]
        children.append(CrdsPlan(plan.inputs, result, futures, eligible, plan.assessments, plan.sites, metadata, plan.deadline))
        need_charge = plan.assessments[move.vehicle_id]["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
        if need_charge or parking[move.facility_id] == chargers[move.facility_id]:
            chargers[move.facility_id] -= 1
        parking[move.facility_id] -= 1
        futures = [apply_move(f, params, move) for f in futures]
        remaining = [m for m in remaining if m.vehicle_id != move.vehicle_id]
    metadata = plan.metadata.model_copy(deep=True)
    metadata.execution = execution_summary(executions, settings.execution_mode)
    if children:
        effects = []
        for original, final in zip(plan.futures, futures):
            before, after = shortage_by_zone(original, params), shortage_by_zone(final, params)
            changes = {z: after[z] - before[z] for z in before}
            effects.append((-fsum(changes.values()), original.future_id, changes))
        gain, future_id, changes = min(effects, key=lambda row: (row[0], row[1]))
        metadata.worst_case_gain_vehicle_minutes = gain
        metadata.worst_case_future_id = future_id
        metadata.shortage_changes_by_zone = changes
        metadata.arrival_minutes_min = min(c.metadata.arrival_minutes_min for c in children)
        metadata.arrival_minutes_max = max(c.metadata.arrival_minutes_max for c in children)
        metadata.decision = "MOVE"
    else:
        metadata.decision, metadata.reason_code, metadata.approval_allowed = "HOLD", stop_reason, False
    return children, metadata, stop_reason


def execution_summary(executions, mode):
    return {"mode": mode, **{key: sum(e.get(key, 0) for e in executions)
        for key in ("requests", "retries", "request_bytes", "response_bytes", "elapsed_ms")}}


def recommendation_record(plan, policy, clock, target_zone_id, *, started_at=None):
    now = clock.now()
    selected = plan.evaluation.selected_vehicle_id
    facility = plan.evaluation.selected_facility_id
    site = plan.sites.get((selected, facility))
    target = next(z for z in plan.inputs.demand.zones if z.zone_id == target_zone_id)
    assessments = []
    for gain in plan.evaluation.ranked_candidates:
        assessment = dict(plan.assessments[gain.vehicle_id])
        assessment["crds_gain"] = gain.model_dump(mode="json")
        assessments.append(assessment)
    if selected:
        explanation = (f"{selected} reduces modeled shortage by at least "
                       f"{plan.metadata.worst_case_gain_vehicle_minutes:g} vehicle-minutes across "
                       f"{plan.metadata.scenario_count} declared futures.")
    else:
        explanation = ("Wait: no assessed move has sufficient fleet-wide benefit." if plan.metadata.decision == "HOLD"
                       else "Assessment incomplete: " + plan.metadata.reason_code + ". Request fresh information.")
    return {"recommendation_id": str(uuid4()), "created_at": now.isoformat(),
        "expires_at": (now + timedelta(seconds=policy.recommendation_ttl_seconds)).isoformat(),
        "policy_version": "crds-v1", "policy_settings": policy.model_dump(exclude={"fleet_url", "demand_url", "facilities_url"}),
        "target": target.model_dump(mode="json"),
        "fleet_snapshot": {"instance_id": str(plan.inputs.fleet.instance_id),
            "snapshot_version": plan.inputs.fleet.snapshot_version, "snapshot_at": plan.inputs.fleet.snapshot_at.isoformat()},
        "selected_vehicle_id": selected, "parking_facility_id": facility,
        "target_position": site.position.model_dump(mode="json") if site else None,
        "requires_charging": bool(selected and plan.assessments[selected]["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT),
        "purpose": "REBALANCE", "ranked_candidates": assessments, "excluded_candidates": [],
        "decision_latency_ms": max(0, (clock.monotonic() - started_at) * 1000) if started_at is not None else 0,
        "explanation": explanation, "crds": plan.metadata.model_dump(mode="json"),
        "assumptions": ["Finite synthetic futures, not a probability guarantee", "One nearest eligible facility per EV",
            "Arrival stock requires usable SOC and modeled charging delay", "No idle-energy consumption modeled",
            "Advisory until explicit operator approval and fresh revalidation"]}
