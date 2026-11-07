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


async def plan(observations, clock, *, batch=False, **selection):
    def respond(request):
        if request.url.path == "/api/v1/route-estimates":
            body = json.loads(request.content)
            fleet = observations["/api/v1/vehicles"]
            vehicle = next(v for v in fleet["vehicles"] if v["vehicle_id"] == body["vehicle_id"])
            origin = vehicle["telemetry"]["position"]
            return httpx.Response(200, json={"vehicle_id": body["vehicle_id"],
                "facility_id": body["facility_id"], "fleet_instance_id": fleet["instance_id"],
                "control_version": 1, "origin": origin,
                "route": fixture_route(origin, BY_ID[body["facility_id"]].position.model_dump()).model_dump(mode="json")})
        return httpx.Response(200, json=observations[request.url.path])

    config = CrdsSettings(mode="ACTIVE", dispatch_url="http://dispatch.test")
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        result = await plan_one(client, Settings(), config, clock, {}, "cbd", **selection)
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
