"""CRDS recommendation orchestration over the existing read APIs."""
import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

import httpx
from fastapi import HTTPException

from ev_fleet.common.parking import CHARGE_ON_ARRIVAL_BELOW_PCT, CHARGING_TARGET_SOC_PCT
from ev_fleet.common.travel import distance_km
from ev_fleet.contracts.crds import (CrdsEvaluation, CrdsFuture, CrdsInputs, CrdsMetadata, CrdsMove,
                                   CrdsMoveOutcome, CrdsRouteEstimate, CrdsRouteRequest)
from .crds import apply_move, shortage_by_zone, shortage_score
from .crds_evidence import (DecisionEvidence, VehicleDecision, availability_reason, publish_evidence,
                            ranked_decisions, route_failure_reason)
from .crds_execution import evaluate_candidates
from .crds_futures import MODEL_VERSION, build_futures, parameters_for
from .crds_inputs import collect_inputs, selected_vehicles_are_current
from .crds_routes import travel_minutes
from .policy import assess
from .traffic import assess_traffic


PARTIAL_ROUTE_COVERAGE = "PARTIAL_ROUTE_COVERAGE"


@dataclass
class CrdsPlan:
    inputs: CrdsInputs
    evaluation: CrdsEvaluation
    futures: list
    moves: list
    assessments: dict
    sites: dict
    metadata: CrdsMetadata
    deadline: float = float("inf")


def has_donor_stock(futures: list[CrdsFuture], origin_zone_id: str | None) -> bool:
    if origin_zone_id is None:
        return True
    # The first future is nominal. Protect one EV where there is observed demand.
    minimum_before_departure = 2 if futures[0].zones[origin_zone_id].desired_stock[0] else 1
    return all(min(future.zones[origin_zone_id].usable_stock) >= minimum_before_departure
               for future in futures)


def traffic_identity(traffic):
    """Return the small version evidence needed by a CRDS fingerprint."""
    identity = {
        key: traffic[key]
        for key in ("source", "mode", "status", "instance_id", "scenario_version")
        if key in traffic
    }
    identity["feeds"] = traffic.get("feeds", {})
    identity["simulated_events"] = traffic.get("simulated_events", [])
    observed = traffic.get("live_observations")
    if observed is not None:
        identity["live_observations"] = traffic_identity(observed)
    return identity


async def plan_one(client, policy, settings, clock, traffic, target_zone_id, *,
                   vehicle_id=None, facility_id=None, route_override=None, exclude_dispatch_id=None,
                   expand_age=None, for_batch=False):
    start = clock.monotonic()
    expand_age = settings.expand_age if expand_age is None else expand_age
    inputs = await collect_inputs(client, policy, settings.dispatch_url, clock, traffic,
                                  timeout_seconds=settings.deadline_seconds,
                                  exclude_dispatch_id=exclude_dispatch_id)
    target = next((z for z in inputs.demand.zones if z.zone_id == target_zone_id), None)
    if target is None:
        raise HTTPException(404, "TARGET_NOT_FOUND")
    params = parameters_for(settings)
    fingerprint = hashlib.sha256(json.dumps({
        "inputs": inputs.model_dump(mode="json", exclude={"traffic"}),
        "traffic": traffic_identity(inputs.traffic),
        "settings": settings.model_dump(), "model": MODEL_VERSION,
        "expand_age": expand_age,
    }, sort_keys=True).encode()).hexdigest()
    metadata = CrdsMetadata(decision="DEFER", reason_code="INCOMPLETE_INPUTS", parameters=params,
        scenario_model_version=MODEL_VERSION, scenario_count=0, epoch_id=uuid4(), input_fingerprint=fingerprint,
        source_references={"fleet_instance_id": str(inputs.fleet.instance_id),
            "fleet_version": inputs.fleet.snapshot_version, "demand_instance_id": str(inputs.demand.instance_id),
            "excluded_vehicle_ids": inputs.excluded_vehicle_ids,
            "relocation_exclusions": [row.model_dump(mode="json") for row in inputs.relocation_exclusions],
            "excluded_facility_ids": inputs.excluded_facility_ids,
            "unassigned_vehicle_ids": [v.vehicle_id for v in inputs.fleet.vehicles
                                       if v.vehicle_id not in inputs.vehicle_zones],
            "demand_versions": {z.zone_id: z.demand_version for z in inputs.demand.zones},
            "planning_instance_id": str(inputs.planning.instance_id), "planning_revision": inputs.planning.revision,
            "traffic_instance_id": traffic.get("instance_id"), "traffic_version": traffic.get("scenario_version"),
            "traffic_mode": traffic.get("source"), "evaluated_at": inputs.evaluated_at.isoformat(),
            "expand_age": expand_age,
            "simulation_time_scale": settings.simulation_time_scale})
    empty = CrdsPlan(inputs, CrdsEvaluation(decision="DEFER", reason_code="INCOMPLETE_INPUTS"),
                     [], [], {}, {}, metadata, start + settings.deadline_seconds)
    vehicles = {v.vehicle_id: VehicleDecision(outcome="INCOMPLETE", reason="ASSESSMENT_INCOMPLETE")
                for v in inputs.fleet.vehicles if not vehicle_id or v.vehicle_id == vehicle_id}
    for identifier, reason in inputs.telemetry_exclusions.items():
        if not vehicle_id or identifier == vehicle_id:
            vehicles[identifier] = VehicleDecision(outcome="EXCLUDED", reason=reason)
    for row in inputs.relocation_exclusions:
        if not vehicle_id or row.vehicle_id == vehicle_id:
            prior = vehicles.get(row.vehicle_id)
            reason = prior.reason if prior and row.reason == "TELEMETRY_UNAVAILABLE" else row.reason
            vehicles[row.vehicle_id] = VehicleDecision(
                outcome="EXCLUDED", reason=reason, dispatch_ids=row.dispatch_ids)
    if vehicle_id and vehicle_id not in vehicles:
        vehicles[vehicle_id] = VehicleDecision(outcome="EXCLUDED", reason="VEHICLE_MISSING")

    def defer(reason, evidence_reason=None):
        metadata.reason_code = reason
        empty.evaluation.reason_code = reason
        publish_evidence(metadata, vehicles, reasons=[evidence_reason] if evidence_reason else ())
        return empty

    def defer_route_failure(failure):
        reason = str(failure) if isinstance(failure, ValueError) else "ROUTE_EVIDENCE_UNAVAILABLE"
        return defer(reason, route_failure_reason(failure))

    if inputs.issues:
        return defer(",".join(inputs.issues))
    try:
        futures = await asyncio.to_thread(
            build_futures, inputs, target_zone_id, settings, expand_age=expand_age,
            min_reserve_pct=policy.min_reserve_pct)
    except ValueError as exc:
        return defer(str(exc), "INCOMPLETE_INPUTS")
    metadata.scenario_count = len(futures)
    reserved = {d.vehicle_id: d.dispatch_id for d in inputs.planning.dispatches}
    assessments, sites, candidates = {}, {}, []
    target_sites = [
        site for site in inputs.facilities.facilities
        if site.zone_id == target_zone_id and (not facility_id or site.facility_id == facility_id)
    ]
    has_withheld_site = any(
        site.zone_id == target_zone_id and (not facility_id or site.facility_id == facility_id)
        for site in inputs.withheld_facilities
    )
    for vehicle in sorted(inputs.fleet.vehicles, key=lambda v: v.vehicle_id):
        identifier = vehicle.vehicle_id
        if identifier not in vehicles:
            continue
        if identifier in reserved:
            vehicles[identifier] = VehicleDecision(
                outcome="EXCLUDED", reason="BUSY", dispatch_ids=[reserved[identifier]])
            continue
        if vehicle.effective_availability != "AVAILABLE":
            vehicles[identifier] = VehicleDecision(
                outcome="EXCLUDED", reason=availability_reason(vehicle))
            continue
        choices = []
        blockers = {"DESTINATION_HOLD_UNCONFIRMED"} if has_withheld_site else set()
        reserve_evidence = None
        for site in target_sites:
            if site.status != "OPEN":
                blockers.add("PARKING_CLOSED")
                continue
            if not site.is_fresh(inputs.evaluated_at):
                blockers.add("FACILITY_DATA_STALE")
                continue
            if site.parking_available < 1:
                blockers.add("PARKING_FULL")
                continue
            assessed_target = target.model_dump(mode="json")
            assessed_target.update(position=site.position.model_dump(mode="json"), charging={
                "observed_at": inputs.evaluated_at.isoformat(), "available_slots": site.charging_available})
            assessment = assess(vehicle.model_dump(mode="json"), assessed_target, policy, inputs.evaluated_at)
            if not assessment["eligible"]:
                blockers.update(assessment["exclusion_reasons"])
                if assessment["exclusion_reasons"] == ["BELOW_OPERATIONAL_RESERVE"]:
                    arrival = assessment["predicted_arrival_soc_pct"]
                    if reserve_evidence is None or arrival > reserve_evidence[0]:
                        reserve_evidence = (arrival, site.facility_id)
                continue
            needs_charge = assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
            if needs_charge and site.charging_available < 1:
                blockers.add("CHARGING_FULL")
                continue
            if site.facility_type == "CHARGING" and not needs_charge:
                blockers.add("CHARGING_ONLY_DESTINATION")
                continue
            choices.append((assessment["distance_km"], site.facility_id, site, assessment))
        if choices:
            if not has_donor_stock(futures, inputs.vehicle_zones.get(identifier)):
                vehicles[identifier] = VehicleDecision(outcome="EXCLUDED", reason="DONOR_SERVICE_FLOOR")
                continue
            _, _, site, assessment = min(choices, key=lambda x: (x[0], x[1]))
            candidates.append((vehicle, site, assessment))
        elif blockers == {"BELOW_OPERATIONAL_RESERVE"} and reserve_evidence:
            arrival, site_id = reserve_evidence
            vehicles[identifier] = VehicleDecision(
                outcome="EXCLUDED", reason="BELOW_OPERATIONAL_RESERVE", facility_id=site_id,
                arrival_soc_pct=arrival, reserve_pct=policy.min_reserve_pct)
        else:
            reasons = sorted(blockers)
            vehicles[identifier] = VehicleDecision(
                outcome="EXCLUDED",
                reason=reasons[0] if len(reasons) == 1 else "NO_SUITABLE_DESTINATION",
                related_reasons=reasons if len(reasons) > 1 else [])
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
            evaluation = await asyncio.to_thread(
                assess_traffic, [p.model_dump() for p in route.geometry],
                route.distance_km, route.duration_seconds, traffic, inputs.evaluated_at)
            if evaluation["blocked"]:
                return VehicleDecision(outcome="EXCLUDED", reason="ROAD_ROUTE_BLOCKED",
                                       facility_id=site.facility_id)
            energy = route.distance_km * vehicle.consumption_kwh_per_km * policy.energy_margin_factor
            arrival_soc = vehicle.telemetry.soc_pct - energy / vehicle.battery_capacity_kwh * 100
            if arrival_soc < policy.min_reserve_pct:
                return VehicleDecision(outcome="EXCLUDED", reason="ROUTE_BELOW_OPERATIONAL_RESERVE",
                                       facility_id=site.facility_id, arrival_soc_pct=arrival_soc,
                                       reserve_pct=policy.min_reserve_pct)
            need_charge = arrival_soc < CHARGE_ON_ARRIVAL_BELOW_PCT
            if need_charge and (site.charging_available < 1 or site.charging_power_kw is None):
                raise ValueError("CHARGING_AVAILABILITY_UNKNOWN")
            if site.facility_type == "CHARGING" and not need_charge:
                return VehicleDecision(outcome="EXCLUDED", reason="CHARGING_ONLY_DESTINATION",
                                       facility_id=site.facility_id)
            availability_delay = (max(0, CHARGING_TARGET_SOC_PCT - arrival_soc) / 100 *
                                  vehicle.battery_capacity_kwh / site.charging_power_kw * 60 /
                                  settings.simulation_time_scale) if need_charge else 0
            # Dispatch currently places its screening-adjusted duration in the
            # issued command. Match that motion baseline once, then apply caps.
            modeled_route = route if route.traffic_assessment is not None else route.model_copy(update={
                "duration_seconds": evaluation["estimated_duration_seconds"] or route.duration_seconds,
                "traffic_assessment": evaluation})
            minutes = await asyncio.to_thread(
                travel_minutes, modeled_route, traffic, inputs.evaluated_at,
                time_scale=settings.simulation_time_scale)
            origin = inputs.vehicle_zones.get(vehicle.vehicle_id)
            outcomes = {}
            for future in futures:
                extra = settings.journey_delay_minutes if future.future_id in ("combined", "delayed_inbound") else 0
                outcomes[future.future_id] = CrdsMoveOutcome(arrival_minutes=minutes + extra,
                    availability_delay_minutes=availability_delay, energy_required_kwh=energy)
            assessment.update(score=None, utilities=None, weighted_components=None,
                distance_km=route.distance_km, energy_required_kwh=energy,
                predicted_arrival_soc_pct=arrival_soc, traffic_assessment=evaluation)
            return CrdsMove(vehicle_id=vehicle.vehicle_id, facility_id=site.facility_id,
                            origin_zone_id=origin, target_zone_id=target_zone_id, outcomes=outcomes), assessment, site

    def route_priority(candidate):
        vehicle, _, assessment = candidate
        origin_id = inputs.vehicle_zones.get(vehicle.vehicle_id)
        origin = futures[0].zones[origin_id] if origin_id is not None else None
        donor_needs_stock = origin is not None and origin.usable_stock[0] <= origin.desired_stock[0]
        return donor_needs_stock, assessment["distance_km"], vehicle.vehicle_id

    deadline = start + settings.deadline_seconds
    # Share the reserved time between scenario scoring and selected EV verification.
    reserve = min(5.0, settings.deadline_seconds / 4) if route_override is None else 0.0
    remaining = deadline - clock.monotonic() - reserve
    if remaining <= 0:
        return defer("DECISION_DEADLINE")
    candidates.sort(key=route_priority)
    tasks = [asyncio.create_task(prepare(candidate)) for candidate in candidates]
    results = []
    if tasks:
        try:
            await asyncio.wait(tasks, timeout=remaining)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            results = await asyncio.gather(*tasks, return_exceptions=True)

    moves = []
    failures: list[BaseException] = []
    for (vehicle, destination, _), item in zip(candidates, results):
        if isinstance(item, asyncio.CancelledError):
            item = asyncio.TimeoutError()
        if isinstance(item, BaseException):
            failures.append(item)
            vehicles[vehicle.vehicle_id] = VehicleDecision(
                outcome="INCOMPLETE", reason=route_failure_reason(item), facility_id=destination.facility_id)
        elif isinstance(item, VehicleDecision):
            vehicles[vehicle.vehicle_id] = item
        else:
            move, assessment, site = item
            moves.append(move)
            assessments[move.vehicle_id] = assessment
            sites[(move.vehicle_id, move.facility_id)] = site
    failure = failures[0] if failures else None
    if failure is not None and not moves:
        return defer_route_failure(failure)
    metadata.input_fingerprint = hashlib.sha256(json.dumps({
        "observations": fingerprint, "futures": [f.model_dump(mode="json") for f in futures],
        "moves": [m.model_dump(mode="json") for m in moves]}, sort_keys=True).encode()).hexdigest()
    result, metadata.execution = await evaluate_candidates(client, settings, metadata.epoch_id,
        futures, moves, params, timeout_seconds=deadline - clock.monotonic())
    selected_id = result.selected_vehicle_id
    if (not for_batch and selected_id and not await selected_vehicles_are_current(
            client, policy, clock, inputs.fleet, {selected_id}, deadline)):
        result = CrdsEvaluation(decision="DEFER", reason_code="STALE_AFTER_ROUTE_LOOKUP")
    if result.decision == "HOLD" and not moves:
        result.reason_code = "NO_ELIGIBLE_RELOCATIONS"
    vehicles.update(ranked_decisions(result, params.minimum_gain_vehicle_minutes, inputs.vehicle_zones))
    if result.decision == "HOLD" and failure is not None:
        return defer_route_failure(failure)
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
    reasons = [result.reason_code, PARTIAL_ROUTE_COVERAGE] if result.decision == "MOVE" and failures else []
    publish_evidence(metadata, vehicles, reasons=reasons)
    return CrdsPlan(inputs, result, futures, moves, assessments, sites, metadata, deadline)


async def plan_batch(plan, quantity, client, settings, clock, *, policy):
    """Greedy supported prefix; all steps use the same complete future set."""
    if plan.metadata.decision == "DEFER" or not plan.moves:
        return [], plan.metadata, plan.metadata.reason_code
    params = plan.metadata.parameters
    futures = plan.futures
    remaining = list(plan.moves)
    parking = {s.facility_id: s.parking_available for s in plan.sites.values()}
    chargers = {s.facility_id: s.charging_available for s in plan.sites.values()}
    children = []
    executions = [plan.metadata.execution]
    stop_reason = "REQUESTED_QUANTITY_REACHED"
    stop_details: list[str] = []
    step_decisions: dict[str, VehicleDecision] = {}
    constrained: dict[str, VehicleDecision] = {}
    original_evidence = DecisionEvidence.model_validate(
        plan.metadata.source_references["decision_evidence"])
    partial_routes = PARTIAL_ROUTE_COVERAGE in original_evidence.assessment_reasons
    selected: dict[str, VehicleDecision] = {}

    def defer_batch(metadata, reason):
        metadata.decision, metadata.reason_code, metadata.approval_allowed = "DEFER", reason, False
        metadata.execution = execution_summary(executions, settings.execution_mode)
        vehicles = {
            identifier: row if row.outcome in ("EXCLUDED", "INCOMPLETE") else VehicleDecision(
                outcome="INCOMPLETE", reason="ASSESSMENT_INCOMPLETE", facility_id=row.facility_id)
            for identifier, row in original_evidence.vehicles.items()
        }
        publish_evidence(metadata, vehicles, batch_stop=[reason])
        return [], metadata, reason

    while len(children) < quantity:
        eligible = []
        constrained = {}
        for move in remaining:
            site_id = move.facility_id
            assessment = plan.assessments[move.vehicle_id]
            if parking[site_id] <= 0:
                reason = "BATCH_PARKING_LIMIT"
            elif (assessment["predicted_arrival_soc_pct"] < CHARGE_ON_ARRIVAL_BELOW_PCT
                  and chargers[site_id] <= 0):
                reason = "BATCH_CHARGING_LIMIT"
            elif not has_donor_stock(futures, move.origin_zone_id):
                reason = "BATCH_ORIGIN_STOCK_LIMIT"
            else:
                eligible.append(move)
                continue
            constrained[move.vehicle_id] = VehicleDecision(
                outcome="NOT_SELECTED", reason=reason, facility_id=site_id)
        if not eligible:
            if constrained:
                stop_reason = "CAPACITY_OR_STOCK_LIMIT"
                stop_details = sorted({row.reason for row in constrained.values()})
            else:
                stop_reason = PARTIAL_ROUTE_COVERAGE if partial_routes else "NO_USEFUL_MOVE"
                stop_details = [PARTIAL_ROUTE_COVERAGE if partial_routes else "NO_MORE_ELIGIBLE_EVS"]
            break
        result, execution = await evaluate_candidates(client, settings, plan.metadata.epoch_id,
            futures, eligible, params, timeout_seconds=plan.deadline - clock.monotonic())
        executions.append(execution)
        if result.decision == "DEFER":
            metadata = plan.metadata.model_copy(deep=True)
            return defer_batch(metadata, result.reason_code)
        step_decisions = ranked_decisions(result, params.minimum_gain_vehicle_minutes, plan.inputs.vehicle_zones)
        if result.decision != "MOVE":
            stop_reason = PARTIAL_ROUTE_COVERAGE if partial_routes else result.reason_code
            stop_details = [stop_reason]
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
        selected[move.vehicle_id] = step_decisions[move.vehicle_id]
        publish_evidence(metadata, {move.vehicle_id: selected[move.vehicle_id]})
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
        if not await selected_vehicles_are_current(client, policy, clock, plan.inputs.fleet,
                                                   set(selected), plan.deadline):
            return defer_batch(metadata, "STALE_AFTER_BATCH_PLANNING")
        effects = []
        for original, final in zip(plan.futures, futures):
            before, after = shortage_by_zone(original, params), shortage_by_zone(final, params)
            changes = {z: after[z] - before[z] for z in before}
            effects.append((shortage_score(original, params) - shortage_score(final, params),
                            original.future_id, changes))
        gain, future_id, changes = min(effects, key=lambda row: (row[0], row[1]))
        metadata.worst_case_gain_vehicle_minutes = gain
        metadata.worst_case_future_id = future_id
        metadata.shortage_changes_by_zone = changes
        metadata.arrival_minutes_min = min(c.metadata.arrival_minutes_min for c in children)
        metadata.arrival_minutes_max = max(c.metadata.arrival_minutes_max for c in children)
        metadata.decision = "MOVE"
    else:
        metadata.decision, metadata.reason_code, metadata.approval_allowed = "HOLD", stop_reason, False
    if not stop_details:
        step_decisions = {
            identifier: row.model_copy(update={"reason": "REQUESTED_QUANTITY_REACHED"})
            for identifier, row in step_decisions.items() if identifier not in selected
        }
    publish_evidence(metadata, original_evidence.vehicles | step_decisions | constrained | selected,
                     reasons=original_evidence.assessment_reasons if children else (), batch_stop=stop_details)
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
        explanation = (f"{selected} improves the modeled coverage score by at least "
                       f"{plan.metadata.worst_case_gain_vehicle_minutes:g} points across "
                       f"{plan.metadata.scenario_count} declared futures.")
    else:
        explanation = ("Wait: no assessed move has sufficient fleet-wide benefit." if plan.metadata.decision == "HOLD"
                       else "The relocation assessment could not be completed.")
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
            "EVs outside demand zones contribute no donor-zone stock",
            "Arrival stock requires usable SOC and modeled charging delay", "No idle-energy consumption modeled",
            "Wall-clock minutes; planned EVs must use the configured shared simulation time scale",
            "Advisory until explicit operator approval and fresh revalidation"]}
