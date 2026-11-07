"""CRDS plans with reachable stock without trusting excluded EV forecasts."""
import asyncio
import json
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.common.settings import CrdsSettings, Settings
from ev_fleet.contracts.demand import DemandUpdate
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.rebalancing.crds import evaluate_moves
from ev_fleet.contracts.crds import CrdsEvaluation, CrdsEvaluationTask, CrdsTaskResult
from ev_fleet.rebalancing.crds_service import plan_batch, plan_one
from tests.dispatch_support import Cluster
from tests.routing_fixture import fixture_route


@pytest.fixture
def observations(clock):
    now = clock.now().isoformat()
    fleet_id = str(uuid4())
    vehicle = {"vehicle_id": "healthy", "battery_capacity_kwh": 60,
        "consumption_kwh_per_km": .18, "state_version": 1,
        "received_at": now, "telemetry_age_seconds": 0, "stale": False,
        "connectivity": "ONLINE", "effective_availability": "AVAILABLE",
        "telemetry": {"session_id": str(uuid4()), "sequence": 1, "observed_at": now,
            "position": {"latitude": 1.35, "longitude": 103.8513}, "soc_pct": 80}}
    zones = [{"zone_id": key, "name": key, "position": position,
        "instance_id": str(uuid4()), "version": 1, "demand_version": 1,
        "charging_version": 0, "charging": None,
        "demand": {"observed_at": now, "desired_available_vehicles": demand}}
        for key, position, demand in [
            ("bishan", vehicle["telemetry"]["position"], 0),
            ("cbd", BY_ID["mock-cbd-01"].position.model_dump(), 6)]]
    return {
        "/api/v1/vehicles": {"instance_id": fleet_id, "snapshot_version": 1,
            "snapshot_at": now, "vehicles": [vehicle, deepcopy(vehicle) | {"vehicle_id": "healthy-2"}]},
        "/api/v1/zones": {"instance_id": str(uuid4()), "snapshot_at": now, "zones": zones},
        "/api/v1/facilities": {"generated_at": now,
            "facilities": [BY_ID["mock-cbd-01"].model_dump(mode="json")]},
        "/api/v1/dispatch-planning-state": {"instance_id": str(uuid4()), "revision": 0,
            "observed_at": now, "dispatches": []}}


def dispatch_for(vehicle, observations, clock, *, command=True):
    identifier = str(uuid4())
    target = BY_ID["mock-cbd-01"].position.model_dump()
    return {"dispatch_id": identifier, "request_id": str(uuid4()),
        "recommendation_id": str(uuid4()), "vehicle_id": vehicle["vehicle_id"],
        "command": {"dispatch_id": identifier, "command_id": str(uuid4()),
            "vehicle_id": vehicle["vehicle_id"],
            "fleet_instance_id": observations["/api/v1/vehicles"]["instance_id"],
            "target_zone_id": "cbd", "target_position": target,
            "issued_at": clock.now().isoformat(), "min_reserve_pct": 20,
            "route_factor": 1.2, "energy_margin_factor": 1.15,
            "route": fixture_route(vehicle["telemetry"]["position"], target).model_dump(mode="json")
        } if command else None,
        "state": "UNKNOWN", "created_at": clock.now().isoformat(),
        "updated_at": clock.now().isoformat(), "acknowledged_at": None,
        "completed_at": None, "last_contact_at": None, "delivery_attempts": 1,
        "distance_travelled_km": 0, "arrival_soc_pct": None,
        "failure_reason": None, "claim_held": True}


async def plan(observations, clock, *, batch=False, worker=False, route_error=None,
               route_distance=None, traffic=None, **selection):
    def respond(request):
        if request.url.path == "/api/v1/route-estimates":
            if route_error:
                return httpx.Response(503, json={"error": {"code": route_error}})
            body = json.loads(request.content)
            fleet = observations["/api/v1/vehicles"]
            vehicle = next(v for v in fleet["vehicles"] if v["vehicle_id"] == body["vehicle_id"])
            origin = vehicle["telemetry"]["position"]
            road = fixture_route(origin, BY_ID[body["facility_id"]].position.model_dump())
            if route_distance:
                road = road.model_copy(update={"distance_km": route_distance})
            return httpx.Response(200, json={"vehicle_id": body["vehicle_id"],
                "facility_id": body["facility_id"], "fleet_instance_id": fleet["instance_id"],
                "control_version": 1, "origin": origin,
                "route": road.model_dump(mode="json")})
        if request.url.path == "/api/v1/crds/evaluate":
            task = CrdsEvaluationTask.model_validate_json(request.content)
            evaluation = evaluate_moves(task.futures, task.moves, task.parameters)
            reply = CrdsTaskResult(epoch_id=task.epoch_id, model_version=task.model_version,
                input_fingerprint=task.input_fingerprint, future_ids=[f.future_id for f in task.futures],
                evaluation=evaluation)
            return httpx.Response(200, json=reply.model_dump(mode="json"))
        return httpx.Response(200, json=observations[request.url.path])

    config = CrdsSettings(mode="ACTIVE", dispatch_url="http://dispatch.test",
        execution_mode="HTTP" if worker else "LOCAL",
        worker_urls=["http://worker1.test", "http://worker2.test"] if worker else [])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await plan_one(client, Settings(), config, clock, traffic or {}, "cbd", **selection)
        if batch:
            return result, await plan_batch(result, 5, client, config, clock)
        return result


@pytest.mark.parametrize("failure", ["stale", "offline", "missing", "observation_age", "transport_age"])
def test_unreachable_ev_does_not_block_single_or_batch(observations, clock, failure):
    vehicles = observations["/api/v1/vehicles"]["vehicles"]
    excluded = deepcopy(vehicles[0])
    excluded["vehicle_id"] = "unreachable"
    # An old unassigned location and unresolved dispatch used to block all EVs.
    excluded["telemetry"]["position"] = {"latitude": 1.16, "longitude": 103.61}
    if failure == "stale":
        excluded["stale"] = True
    elif failure == "offline":
        excluded["connectivity"] = "OFFLINE"
    elif failure == "missing":
        excluded["telemetry"] = None
    elif failure == "observation_age":
        excluded["telemetry"]["observed_at"] = (clock.now() - timedelta(seconds=15)).isoformat()
    else:
        excluded["telemetry_age_seconds"] = 15
    vehicles.append(excluded)
    observations["/api/v1/dispatch-planning-state"]["dispatches"].append(
        dispatch_for(vehicles[0], observations, clock, command=False) | {"vehicle_id": "unreachable"})
    before = deepcopy(observations)
    result, (children, metadata, _) = asyncio.run(plan(observations, clock, batch=True))
    assert result.metadata.decision == "MOVE"
    assert result.evaluation.selected_vehicle_id == "healthy"
    assert result.metadata.source_references["excluded_vehicle_ids"] == ["unreachable"]
    assert result.inputs.available_by_zone == {"bishan": 2, "cbd": 0}
    assert metadata.decision == "MOVE" and len(children) == 1
    row = result.metadata.source_references["decision_evidence"]["vehicles"]["unreachable"]
    assert row["outcome"] == "EXCLUDED"
    assert row["reason"] == {"offline": "OFFLINE", "missing": "NO_TELEMETRY"}.get(failure, "STALE_TELEMETRY")
    assert len(row["dispatch_ids"]) == 1
    assert all(max(f.zones["cbd"].usable_stock) == 0 for f in result.futures)
    assert observations == before  # Filtering does not mutate owning state.


def test_stale_claim_and_rental_forecasts_are_excluded(observations, clock):
    vehicles = observations["/api/v1/vehicles"]["vehicles"]
    stale = deepcopy(vehicles[0])
    stale.update(vehicle_id="stale", stale=True)
    dispatch = dispatch_for(stale, observations, clock)
    stale["active_claim"] = {"dispatch_id": dispatch["dispatch_id"],
        "command_id": dispatch["command"]["command_id"], "fleet_instance_id": str(uuid4()),
        "vehicle_id": "stale", "target_zone_id": "unknown",
        "target_position": dispatch["command"]["target_position"],
        "origin": stale["telemetry"]["position"], "acquired_at": clock.now().isoformat()}
    rental = deepcopy(stale)
    rental.update(vehicle_id="stale-rental", active_claim=None, active_rental={
        "rental_id": str(uuid4()), "status": "RENTED", "expected_end_at": clock.now().isoformat(),
        "started_at": clock.now().isoformat(), "expected_return_zone_id": "cbd",
        "start_position": stale["telemetry"]["position"], "start_soc_pct": 80})
    vehicles.extend([stale, rental])
    observations["/api/v1/dispatch-planning-state"]["dispatches"].append(dispatch)
    result = asyncio.run(plan(observations, clock))
    assert result.metadata.decision == "MOVE"
    assert result.inputs.issues == []
    assert all(max(f.zones["cbd"].usable_stock) == 0 for f in result.futures)


@pytest.mark.parametrize("failure,reason", [
    ("fleet", "FLEET_SNAPSHOT_STALE"), ("demand", "DEMAND_UNKNOWN_OR_STALE"),
    ("planning", "DISPATCH_SNAPSHOT_STALE"), ("unresolved", "PENDING_DISPATCH_UNRESOLVED"),
    ("unassigned", "UNASSIGNED_VEHICLES")])
def test_live_data_issues_and_outside_zone_stock(observations, clock, failure, reason):
    old = (clock.now() - timedelta(seconds=301)).isoformat()
    if failure == "fleet":
        observations["/api/v1/vehicles"]["snapshot_at"] = old
    elif failure == "demand":
        observations["/api/v1/zones"]["zones"][0]["demand"]["observed_at"] = old
    elif failure == "planning":
        observations["/api/v1/dispatch-planning-state"]["observed_at"] = old
    elif failure == "unresolved":
        observations["/api/v1/dispatch-planning-state"]["dispatches"].append(
            dispatch_for(observations["/api/v1/vehicles"]["vehicles"][0], observations, clock, command=False)
            | {"vehicle_id": None})
    else:
        observations["/api/v1/vehicles"]["vehicles"][0]["telemetry"]["position"] = {
            "latitude": 1.16, "longitude": 103.61}
    result = asyncio.run(plan(observations, clock))
    if failure == "unassigned":
        assert result.metadata.decision == "MOVE"
        assert result.evaluation.selected_vehicle_id == "healthy"
        assert next(m for m in result.moves if m.vehicle_id == "healthy").origin_zone_id is None
        assert result.metadata.source_references["unassigned_vehicle_ids"] == ["healthy"]
        return
    assert result.metadata.decision == "DEFER"
    assert reason in result.metadata.reason_code


def test_all_stale_fleet_holds_without_selecting_an_ev(observations, clock):
    for vehicle in observations["/api/v1/vehicles"]["vehicles"]:
        vehicle["stale"] = True
    result = asyncio.run(plan(observations, clock))
    assert result.metadata.decision == "HOLD"
    assert result.evaluation.selected_vehicle_id is None
    assert result.metadata.approval_allowed is False
    rows = result.metadata.source_references["decision_evidence"]["vehicles"]
    assert {row["reason"] for row in rows.values()} == {"STALE_TELEMETRY"}


def test_fresh_telemetry_returns_ev_to_planning(observations, clock):
    vehicle = observations["/api/v1/vehicles"]["vehicles"][0]
    vehicle["stale"] = True
    before = asyncio.run(plan(observations, clock))
    assert before.metadata.decision == "HOLD"
    vehicle["stale"] = False
    after = asyncio.run(plan(observations, clock))
    assert after.metadata.decision == "MOVE"
    assert after.inputs.excluded_vehicle_ids == []


def test_fresh_committed_ev_keeps_its_inbound_forecast(observations, clock):
    vehicles = observations["/api/v1/vehicles"]["vehicles"]
    vehicle = deepcopy(vehicles[0]) | {"vehicle_id": "inbound", "effective_availability": "UNAVAILABLE"}
    dispatch = dispatch_for(vehicle, observations, clock) | {"state": "IN_PROGRESS"}
    vehicle["active_claim"] = {"dispatch_id": dispatch["dispatch_id"],
        "command_id": dispatch["command"]["command_id"],
        "fleet_instance_id": observations["/api/v1/vehicles"]["instance_id"],
        "vehicle_id": "inbound", "target_zone_id": "cbd",
        "target_position": dispatch["command"]["target_position"],
        "origin": vehicle["telemetry"]["position"], "acquired_at": clock.now().isoformat()}
    vehicle["telemetry"].update(availability="UNAVAILABLE", relocation_state="RELOCATING",
                                active_dispatch_id=dispatch["dispatch_id"])
    vehicles.append(vehicle)
    observations["/api/v1/dispatch-planning-state"]["dispatches"].append(dispatch)
    result = asyncio.run(plan(observations, clock))
    assert result.inputs.issues == []
    assert result.inputs.available_by_zone["bishan"] == 2
    assert "inbound" not in {m.vehicle_id for m in result.moves}
    assert max(next(f for f in result.futures if f.future_id == "nominal").zones["cbd"].usable_stock) == 1
    dispatch["state"] = "FAILED"
    failed = asyncio.run(plan(observations, clock))
    assert failed.metadata.decision == "MOVE"
    assert failed.inputs.relocation_exclusions[0].reason == "RELOCATION_FAILED"
    assert all(max(f.zones["cbd"].usable_stock) == 0 for f in failed.futures)

    dispatch["state"] = "IN_PROGRESS"
    # An inconsistent claim removes just the affected EV from the forecast.
    vehicle["active_claim"]["fleet_instance_id"] = str(uuid4())
    result = asyncio.run(plan(observations, clock))
    assert result.metadata.decision == "MOVE"
    assert result.inputs.relocation_exclusions[0].reason == "RELOCATION_OWNERSHIP_UNCONFIRMED"
    assert all(max(f.zones["cbd"].usable_stock) == 0 for f in result.futures)


@pytest.mark.parametrize("state", ["FAILED", "REJECTED", "UNKNOWN", "PENDING"])
def test_unresolved_relocation_removes_stock_and_blocks_only_its_site(observations, clock, state):
    vehicles = observations["/api/v1/vehicles"]["vehicles"]
    affected = deepcopy(vehicles[0]) | {"vehicle_id": "affected"}
    vehicles.append(affected)
    dispatch = dispatch_for(affected, observations, clock) | {
        "state": state, "parking_facility_id": "mock-cbd-01", "claim_held": None,
    }
    observations["/api/v1/dispatch-planning-state"]["dispatches"].append(dispatch)
    observations["/api/v1/facilities"]["facilities"].append(BY_ID["mock-cbd-02"].model_dump(mode="json"))
    result, (children, _, _) = asyncio.run(plan(observations, clock, batch=True))
    assert result.metadata.decision == "MOVE"
    assert result.inputs.available_by_zone["bishan"] == 2
    assert result.evaluation.selected_vehicle_id == "healthy"
    assert result.evaluation.selected_facility_id == "mock-cbd-02"
    assert len(children) == 1
    assert result.inputs.excluded_facility_ids == ["mock-cbd-01"]
    assert all(max(f.zones["cbd"].usable_stock) == 0 for f in result.futures)


    route = fixture_route(vehicles[0]["telemetry"]["position"], BY_ID["mock-cbd-02"].position.model_dump())
    revalidated = asyncio.run(plan(observations, clock, vehicle_id="healthy",
        facility_id="mock-cbd-02", route_override=route, exclude_dispatch_id=str(uuid4())))
    assert revalidated.metadata.approval_allowed
    blocked = asyncio.run(plan(observations, clock, vehicle_id="healthy", facility_id="mock-cbd-01"))
    assert blocked.metadata.reason_code == "NO_ELIGIBLE_RELOCATIONS"


@pytest.mark.parametrize("churn", [1, 2])
def test_active_crds_approval_retries_one_changed_planning_revision(clock, monkeypatch, churn):
    monkeypatch.setenv("CRDS_MODE", "ACTIVE")
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch.test")

    async def scenario():
        cluster = Cluster(clock)
        route = cluster.route
        attempts = []

        async def respond(request):
            response = await route(request)
            if request.url.path == "/api/v1/crds/revalidate":
                attempts.append(json.loads(request.content)["expected_planning_revision"])
                if len(attempts) <= churn:
                    cluster.dispatch.planning_revision += 1
            return response

        monkeypatch.setattr(cluster, "route", respond)
        async with cluster:
            for zone_id in cluster.demand.zones:
                await cluster.demand.update(zone_id, "demand", DemandUpdate(expected_version=0,
                    observed_at=clock.now(), desired_available_vehicles=6 if zone_id == "cbd" else 0))
            for name in ("healthy", "healthy-2"):
                await cluster.vehicle(name)
            recommendation = await cluster.recommend()
            assert recommendation["crds"]["decision"] == "MOVE"
            vehicle_id = recommendation["selected_vehicle_id"]
            if churn == 1:
                dispatch, _ = await cluster.approve(recommendation)
                assert dispatch["claim_held"] is True
            else:
                with pytest.raises(HTTPException, match="CRDS_PLANNING_CHANGED"):
                    await cluster.approve(recommendation)
                assert (await cluster.fleet.get(vehicle_id))["active_claim"] is None
                assert not cluster.facilities.holds
            assert len(attempts) == 2 and attempts[1] == attempts[0] + 1

    asyncio.run(scenario())


def test_recommendation_explains_same_ranking_locally_and_over_http(observations, clock):
    local = asyncio.run(plan(observations, clock))
    distributed = asyncio.run(plan(observations, clock, worker=True))
    assert distributed.evaluation == local.evaluation
    assert distributed.metadata.approval_allowed == local.metadata.approval_allowed is True
    evidence = local.metadata.source_references["decision_evidence"]
    assert distributed.metadata.source_references["decision_evidence"] == evidence
    assert evidence["vehicles"]["healthy"]["outcome"] == "RECOMMENDED"
    assert evidence["vehicles"]["healthy-2"]["reason"] == "STABLE_ORDER_TIEBREAK"


def test_batch_explains_parking_limit_and_keeps_child_evidence_scoped(observations, clock):
    site = observations["/api/v1/facilities"]["facilities"][0]
    site.update(parking_available=1, charging_available=0)
    _, (children, metadata, reason) = asyncio.run(plan(observations, clock, batch=True))
    assert reason == "CAPACITY_OR_STOCK_LIMIT"
    assert len(children) == 1 and metadata.approval_allowed
    evidence = metadata.source_references["decision_evidence"]
    assert evidence["batch_stop_reasons"] == ["BATCH_PARKING_LIMIT"]
    assert evidence["vehicles"]["healthy-2"]["reason"] == "BATCH_PARKING_LIMIT"
    child = children[0].metadata.source_references["decision_evidence"]
    assert list(child["vehicles"]) == ["healthy"]


def test_requested_ev_missing_and_road_reserve_are_distinct_exclusions(observations, clock):
    missing = asyncio.run(plan(observations, clock, vehicle_id="unknown"))
    assert missing.metadata.decision == "HOLD"
    rows = missing.metadata.source_references["decision_evidence"]["vehicles"]
    assert list(rows) == ["unknown"]
    assert rows["unknown"]["reason"] == "VEHICLE_MISSING"
    low_reserve = asyncio.run(plan(observations, clock, vehicle_id="healthy", route_distance=250))
    assert low_reserve.metadata.decision == "HOLD"
    row = low_reserve.metadata.source_references["decision_evidence"]["vehicles"]["healthy"]
    assert row["reason"] == "ROUTE_BELOW_OPERATIONAL_RESERVE"
    assert row["arrival_soc_pct"] < row["reserve_pct"]


def test_route_access_defers_without_reporting_eligible_evs_as_rejected(observations, clock):
    result, (children, metadata, reason) = asyncio.run(
        plan(observations, clock, batch=True, route_error="ONEMAP_AUTH_FAILED"))
    assert not children and reason == "ROUTE_EVIDENCE_UNAVAILABLE"
    assert metadata.decision == result.evaluation.decision == "DEFER"
    evidence = metadata.source_references["decision_evidence"]
    assert evidence["assessment_reasons"] == ["ROUTING_ACCESS_UNAVAILABLE"]
    assert {row["outcome"] for row in evidence["vehicles"].values()} == {"INCOMPLETE"}
    assert "ONEMAP_AUTH_FAILED" not in json.dumps(evidence)


def test_late_batch_defer_discards_provisional_recommendations(observations, clock, monkeypatch):
    from ev_fleet.rebalancing import crds_service

    async def scenario():
        initial = await plan(observations, clock)
        evaluate = crds_service.evaluate_candidates
        calls = 0

        async def evaluate_with_late_failure(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                return CrdsEvaluation(decision="DEFER", reason_code="WORKER_RESULTS_INCOMPLETE"), {}
            return await evaluate(*args, **kwargs)

        monkeypatch.setattr(crds_service, "evaluate_candidates", evaluate_with_late_failure)
        config = CrdsSettings(mode="ACTIVE", dispatch_url="http://dispatch.test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client:
            children, metadata, reason = await plan_batch(initial, 3, client, config, clock)
        assert calls == 2 and not children
        assert reason == "WORKER_RESULTS_INCOMPLETE" and not metadata.approval_allowed
        evidence = metadata.source_references["decision_evidence"]
        assert {row["outcome"] for row in evidence["vehicles"].values()} == {"INCOMPLETE"}

    asyncio.run(scenario())


def test_known_road_closure_excludes_the_ev(observations, clock):
    origin = observations["/api/v1/vehicles"]["vehicles"][0]["telemetry"]["position"]
    destination = BY_ID["mock-cbd-01"].position.model_dump()
    traffic = {"source": "SIMULATION", "simulated_events": [
        {"id": "closure", "kind": "ROAD_CLOSED", "start": origin, "end": destination}
    ]}
    result = asyncio.run(plan(observations, clock, vehicle_id="healthy", traffic=traffic))
    assert result.metadata.decision == "HOLD"
    row = result.metadata.source_references["decision_evidence"]["vehicles"]["healthy"]
    assert row["outcome"] == "EXCLUDED" and row["reason"] == "ROAD_ROUTE_BLOCKED"


def test_eligible_ev_below_benefit_threshold_is_not_excluded(observations, clock):
    observations["/api/v1/zones"]["zones"][1]["demand"]["desired_available_vehicles"] = 0
    result = asyncio.run(plan(observations, clock))
    assert result.metadata.decision == "HOLD"
    rows = result.metadata.source_references["decision_evidence"]["vehicles"]
    assert {row["outcome"] for row in rows.values()} == {"NOT_SELECTED"}
    assert {row["reason"] for row in rows.values()} == {"NO_USEFUL_MOVE"}


@pytest.mark.parametrize("reason,outcome,gain", [
    ("NO_USEFUL_MOVE", "NOT_SELECTED", 0.5),
    ("RENTED", "EXCLUDED", None),
])
def test_dispatch_preserves_fresh_crds_rejection_reason(clock, monkeypatch, reason, outcome, gain):
    monkeypatch.setenv("CRDS_MODE", "ACTIVE")
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch.test")

    async def scenario():
        cluster = Cluster(clock)
        route = cluster.route

        async def respond(request):
            response = await route(request)
            if request.url.path != "/api/v1/crds/revalidate":
                return response
            assert response.status_code == 200
            await response.aread()
            payload = response.json()
            metadata = payload["metadata"]
            metadata.update(decision="HOLD", approval_allowed=False,
                            reason_code="NO_USEFUL_MOVE" if gain is not None else "NO_ELIGIBLE_RELOCATIONS")
            row = metadata["source_references"]["decision_evidence"]["vehicles"][selected]
            row.update(outcome=outcome, reason=reason, gain_vehicle_minutes=gain)
            return httpx.Response(200, json={**payload, "allowed": False})

        monkeypatch.setattr(cluster, "route", respond)
        async with cluster:
            for zone_id in cluster.demand.zones:
                await cluster.demand.update(zone_id, "demand", DemandUpdate(expected_version=0,
                    observed_at=clock.now(), desired_available_vehicles=6 if zone_id == "cbd" else 0))
            for name in ("healthy", "healthy-2"):
                await cluster.vehicle(name)
            recommendation = await cluster.recommend()
            selected = recommendation["selected_vehicle_id"]
            rejected = await cluster.client.post("http://dispatch.test/api/v1/dispatches", json={
                "request_id": str(uuid4()), "recommendation_id": recommendation["recommendation_id"]})
            assert rejected.status_code == 409
            error = rejected.json()["error"]
            assert error["code"].startswith("CRDS_BENEFIT_CHANGED:")
            assert error["approval_decision"]["reason_code"] == reason
            assert error["approval_decision"].get("gain_vehicle_minutes") == gain
            assert not cluster.fleet.vehicles[selected]["claims"]
            assert not cluster.facilities.holds

    asyncio.run(scenario())
