"""Operator quantity fulfilment uses isolated services and fixture road routes."""
import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from ev_fleet.contracts.demand import DemandUpdate
from ev_fleet.contracts.dispatch import BatchApproval
from tests.dispatch_support import Cluster


@asynccontextmanager
async def operator_cluster(clock, monkeypatch, *, count=6, capacity=12, target_demand=1):
    monkeypatch.setenv("CRDS_MODE", "ACTIVE")
    monkeypatch.setenv("DISPATCH_URL", "http://dispatch.test")
    async with Cluster(clock, recommendation_ttl=120) as cluster:
        site = cluster.facilities.catalogue["mock-cbd-03"]
        cluster.facilities.catalogue = {
            site.facility_id: site.model_copy(update={
                "parking_capacity": capacity, "parking_available": capacity,
                "charging_bays": 0, "charging_available": 0,
            })
        }
        for zone_id in cluster.demand.zones:
            zone = await cluster.demand.get(zone_id)
            await cluster.demand.update(zone_id, "demand", DemandUpdate(
                expected_version=zone["demand_version"], observed_at=clock.now(),
                desired_available_vehicles=(10 if zone_id == "bishan" else
                    target_demand if zone_id == "cbd" else 1)))
        for index in range(count):
            await cluster.vehicle(f"operator-{index}", soc=90)
        yield cluster


async def request_batch(cluster, *, quantity=6, mode="FULFIL_REQUEST"):
    response = await cluster.client.post(
        "http://rebalancing.test/api/v1/batch-recommendations",
        json={"target_zone_id": "cbd", "quantity": quantity,
              "planning_mode": mode})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("target_demand", [1, 12])
def test_required_six_non_beneficial_moves_can_be_reviewed_and_approved(
        clock, monkeypatch, target_demand):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, target_demand=target_demand) as cluster:
            batch = await request_batch(cluster)
            assert batch["recommended_count"] == 6, batch
            assert batch["crds"]["planning_mode"] == "FULFIL_REQUEST"
            assert batch["crds"]["coverage_tradeoff"] is True
            assert batch["crds"]["worst_case_gain_vehicle_minutes"] < 0
            assert batch["crds"]["approval_allowed"] is True
            assert not cluster.facilities.holds
            for index in range(6):
                assert (await cluster.fleet.get(f"operator-{index}"))["active_claim"] is None
            approval = BatchApproval(request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"])
            result = await cluster.dispatch.approve_batch(approval)
            assert result["status"] == "APPROVED", result
            assert len(result["items"]) == 6
            assert await cluster.dispatch.approve_batch(approval) == result
    asyncio.run(scenario())


def test_advisory_mode_still_declines_non_beneficial_moves(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch) as cluster:
            batch = await request_batch(cluster, mode="OPTIMIZE_COVERAGE")
            assert batch["recommended_count"] == 0
            assert batch["crds"]["approval_allowed"] is False
    asyncio.run(scenario())


def test_required_batch_stops_at_available_parking_capacity(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, capacity=2) as cluster:
            batch = await request_batch(cluster)
            assert batch["recommended_count"] == 2, batch
            assert batch["unfulfilled_count"] == 4
            assert "DESTINATION_CAPACITY_LIMIT" in batch["crds"]["source_references"][
                "decision_evidence"]["batch_stop_reasons"]
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["battery", "offline", "expiry"])
def test_required_quantity_does_not_override_vehicle_or_expiry_checks(clock, monkeypatch, change):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=2) as cluster:
            batch = await request_batch(cluster, quantity=2)
            assert batch["recommended_count"] == 2, batch
            vehicle = batch["assignments"][0]["vehicle_id"]
            if change == "battery":
                await cluster.telemetry(vehicle, soc=1)
            elif change == "offline":
                clock.advance(31)
                other = batch["assignments"][1]["vehicle_id"]
                await cluster.telemetry(other, soc=90)
            else:
                clock.advance(121)
            approval = BatchApproval(request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"])
            response = await cluster.client.post(
                "http://dispatch.test/api/v1/batches", json=approval.model_dump(mode="json"))
            if change == "expiry":
                assert response.status_code in (404, 409), response.text
            else:
                assert response.status_code == 202, response.text
                assert response.json()["job_status"] == "QUEUED"
                assert (await cluster.fleet.get(vehicle))["active_claim"] is None
                await cluster.dispatch.queue_once()
                outcome = await cluster.client.get(
                    f"http://dispatch.test/api/v1/batches/{approval.request_id}")
                assert outcome.status_code == 200, outcome.text
                item = next(i for i in outcome.json()["items"] if i["vehicle_id"] == vehicle)
                assert item["approval_status"] == "REJECTED", item
            assert (await cluster.fleet.get(vehicle))["active_claim"] is None
    asyncio.run(scenario())


def test_required_quantity_never_selects_unverified_routes(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=2) as cluster:
            cluster.failures.append({
                "method": "POST", "path": "/api/v1/route-estimates",
                "action": "offline", "once": False,
            })
            batch = await request_batch(cluster, quantity=2)
            assert batch["recommended_count"] == 0
            assert batch["crds"]["decision"] == "DEFER"
            assert batch["crds"]["approval_allowed"] is False
            assert not cluster.facilities.holds
    asyncio.run(scenario())


def test_required_approval_still_rechecks_parking_capacity(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=2) as cluster:
            batch = await request_batch(cluster, quantity=2)
            assert batch["recommended_count"] == 2
            site_id = batch["assignments"][0]["facility_id"]
            site = cluster.facilities.catalogue[site_id]
            cluster.facilities.catalogue[site_id] = site.model_copy(
                update={"parking_available": 0})
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"]))
            assert all(i["approval_status"] == "REJECTED" for i in result["items"])
            assert not cluster.facilities.holds
    asyncio.run(scenario())


def test_required_approval_keeps_intent_when_demand_changes(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=2, target_demand=12) as cluster:
            batch = await request_batch(cluster, quantity=2)
            assert batch["recommended_count"] == 2
            zone = await cluster.demand.get("cbd")
            await cluster.demand.update("cbd", "demand", DemandUpdate(
                expected_version=zone["demand_version"], observed_at=clock.now(),
                desired_available_vehicles=0))
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"]))
            assert result["status"] == "APPROVED", result
    asyncio.run(scenario())


def test_required_quantity_uses_other_destinations_when_nearest_fills(clock, monkeypatch):
    from ev_fleet.facilities.catalog import BY_ID

    async def scenario():
        async with operator_cluster(clock, monkeypatch, capacity=1) as cluster:
            alternative = BY_ID["mock-cbd-01"].model_copy(update={
                "parking_capacity": 6, "parking_available": 6,
                "charging_bays": 0, "charging_available": 0,
            })
            cluster.facilities.catalogue[alternative.facility_id] = alternative
            batch = await request_batch(cluster)
            assert batch["recommended_count"] == 6, batch
            sites = [a["facility_id"] for a in batch["assignments"]]
            assert sites.count("mock-cbd-03") <= 1
            assert sites.count(alternative.facility_id) >= 5
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(),
                batch_recommendation_id=batch["batch_recommendation_id"]))
            assert result["status"] == "APPROVED", result
    asyncio.run(scenario())


def test_required_inbound_quantity_does_not_count_evs_already_in_target(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, target_demand=12) as cluster:
            for index in range(3):
                name = f"already-cbd-{index}"
                await cluster.vehicle(name, soc=90)
                await cluster.telemetry(name, soc=90, position={
                    "latitude": 1.283, "longitude": 103.8513,
                })
            batch = await request_batch(cluster)
            assert batch["recommended_count"] == 6, batch
            assert all(a["vehicle_id"].startswith("operator-") for a in batch["assignments"])
            rows = batch["crds"]["source_references"]["decision_evidence"]["vehicles"]
            assert all(rows[f"already-cbd-{i}"]["reason"] == "ALREADY_IN_TARGET_ZONE"
                       for i in range(3))
    asyncio.run(scenario())


def test_required_matching_preserves_chargers_for_evs_that_need_them(clock, monkeypatch):
    from ev_fleet.facilities.catalog import BY_ID

    async def scenario():
        async with operator_cluster(clock, monkeypatch, count=0) as cluster:
            charging = BY_ID["mock-cbd-03"].model_copy(update={
                "parking_capacity": 1, "parking_available": 1,
                "charging_bays": 1, "charging_available": 1,
                "charging_power_kw": 22, "connector_types": ["CCS2"],
            })
            parking = BY_ID["mock-cbd-01"].model_copy(update={
                "parking_capacity": 1, "parking_available": 1,
                "charging_bays": 0, "charging_available": 0,
            })
            cluster.facilities.catalogue = {
                charging.facility_id: charging, parking.facility_id: parking,
            }
            await cluster.vehicle("flexible", soc=90)
            await cluster.vehicle("needs-charger", soc=30)
            batch = await request_batch(cluster, quantity=2)
            assert batch["recommended_count"] == 2, batch
            chosen = {a["vehicle_id"]: a for a in batch["assignments"]}
            assert chosen["needs-charger"]["facility_id"] == charging.facility_id
            assert chosen["needs-charger"]["requires_charging"] is True
            assert chosen["flexible"]["facility_id"] == parking.facility_id
    asyncio.run(scenario())


def test_required_tradeoff_warning_survives_positive_whole_plan_gain(clock, monkeypatch):
    async def scenario():
        async with operator_cluster(clock, monkeypatch, target_demand=3) as cluster:
            zone = await cluster.demand.get("bishan")
            await cluster.demand.update("bishan", "demand", DemandUpdate(
                expected_version=zone["demand_version"], observed_at=clock.now(),
                desired_available_vehicles=0))
            batch = await request_batch(cluster)
            assert batch["recommended_count"] == 6, batch
            assert batch["crds"]["worst_case_gain_vehicle_minutes"] > 0
            assert any(a["crds_gain"]["worst_case_gain_vehicle_minutes"] <= 1
                       for a in batch["assignments"])
            assert batch["crds"]["coverage_tradeoff"] is True
    asyncio.run(scenario())
