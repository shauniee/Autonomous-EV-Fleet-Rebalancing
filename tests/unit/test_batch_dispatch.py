"""Contract, matching, parking capacity and batch dispatch tests."""
import asyncio
from datetime import timedelta
from uuid import uuid4, uuid5

import httpx
import pytest
from fastapi import HTTPException

from ev_fleet.contracts.dispatch import BatchApproval, DispatchApproval
from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.contracts.rentals import RentalStart
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.facilities.catalog import BY_ID
from tests.dispatch_support import Cluster


def test_atomic_mock_parking_holds_do_not_overallocate_and_release_is_idempotent():
    async def scenario():
        store = FacilitiesStore()
        facility = BY_ID["mock-cbd-03"]
        requests = [ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id=f"ev-{i}",
            expected_zone_id="cbd", expected_position=facility.position)
            for i in range(6)]
        outcome = await asyncio.gather(
            *(store.reserve(facility.facility_id, request) for request in requests),
            return_exceptions=True)
        accepted = [x for x in outcome if isinstance(x, dict)]
        rejected = [x for x in outcome if isinstance(x, HTTPException)]
        assert len(accepted) == 3 and len(rejected) == 3
        assert all(e.detail == "PARKING_FULL" for e in rejected)
        assert (await store.get(facility.facility_id)).parking_available == 0
        original = accepted[0]
        release = await store.change(original["dispatch_id"], "RELEASED")
        assert release == await store.change(original["dispatch_id"], "RELEASED")
        assert (await store.get(facility.facility_id)).parking_available == 1
        another = await store.reserve(facility.facility_id, requests[4])
        assert another["status"] == "RESERVED"
        with pytest.raises(HTTPException):
            await store.reserve(facility.facility_id, ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id=another["vehicle_id"],
                expected_zone_id="cbd", expected_position=facility.position))
        occupied = await store.change(another["dispatch_id"], "OCCUPIED")
        assert occupied["status"] == "OCCUPIED"
        assert (await store.get(facility.facility_id)).parking_available == 0
    asyncio.run(scenario())


def test_completed_vehicle_can_transfer_to_a_new_parking_facility():
    async def scenario():
        store = FacilitiesStore()
        origin = BY_ID["mock-cbd-01"]
        destination = BY_ID["mock-tampines-01"]
        first = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-repeat",
            expected_zone_id=origin.zone_id, expected_position=origin.position)
        second = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-repeat",
            expected_zone_id=destination.zone_id, expected_position=destination.position)

        original = await store.reserve(origin.facility_id, first)
        await store.change(original["dispatch_id"], "OCCUPIED")
        origin_after_arrival = await store.get(origin.facility_id)

        onward = await store.reserve(destination.facility_id, second)
        assert onward["status"] == "RESERVED"
        # The EV still occupies its origin until the onward trip succeeds.
        assert (await store.get(origin.facility_id)).parking_available == origin_after_arrival.parking_available
        with pytest.raises(HTTPException) as error:
            await store.reserve(destination.facility_id, ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id="ev-repeat",
                expected_zone_id=destination.zone_id, expected_position=destination.position))
        assert error.value.detail == "VEHICLE_ALREADY_ASSIGNED_PARKING"

        await store.change(onward["dispatch_id"], "OCCUPIED")
        assert (await store.get_hold(original["dispatch_id"]))["status"] == "RELEASED"
        assert (await store.get_hold(onward["dispatch_id"]))["status"] == "OCCUPIED"
        assert (await store.get(origin.facility_id)).parking_available == origin_after_arrival.parking_available + 1

    asyncio.run(scenario())


def test_failed_onward_relocation_keeps_the_original_bay_occupied():
    async def scenario():
        store = FacilitiesStore()
        origin = BY_ID["mock-cbd-01"]
        destination = BY_ID["mock-tampines-01"]
        first = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-repeat",
            expected_zone_id=origin.zone_id, expected_position=origin.position)
        second = ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-repeat",
            expected_zone_id=destination.zone_id, expected_position=destination.position)

        original = await store.reserve(origin.facility_id, first)
        await store.change(original["dispatch_id"], "OCCUPIED")
        onward = await store.reserve(destination.facility_id, second)
        await store.change(onward["dispatch_id"], "RELEASED")

        assert (await store.get_hold(original["dispatch_id"]))["status"] == "OCCUPIED"
        assert (await store.get_hold(onward["dispatch_id"]))["status"] == "RELEASED"

    asyncio.run(scenario())


def test_batch_recommendation_assigns_distinct_evs_and_parking_capacity(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            for name in ("ev-1", "ev-2", "ev-3"):
                await cluster.vehicle(name)
            request = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 5})
            assert request.status_code == 201, request.text
            batch = request.json()
            assert batch["requested_count"] == 5
            assert batch["recommended_count"] == 3
            assert batch["unfulfilled_count"] == 2
            ids = [row["vehicle_id"] for row in batch["assignments"]]
            assert len(ids) == len(set(ids))
            assert all(row["facility_id"].startswith("mock-cbd-") for row in batch["assignments"])
            assert all(0 <= row["score"] <= 1 for row in batch["assignments"])
            approved = await cluster.dispatch.approve_batch(
                BatchApproval(request_id=uuid4(), batch_recommendation_id=batch["batch_recommendation_id"]))
            assert approved["status"] == "APPROVED"
            assert len(approved["items"]) == 3
            claims = [(await cluster.fleet.get(name))["active_claim"] for name in ids]
            assert all(claim is not None for claim in claims)
            assert len({claim["dispatch_id"] for claim in claims}) == 3
            snapshot = await cluster.facilities.snapshot("cbd")
            for site in snapshot:
                holds = sum(h["facility_id"] == site.facility_id and
                            h["status"] == "RESERVED" for h in cluster.facilities.holds.values())
                assert site.parking_available == BY_ID[site.facility_id].parking_available - holds
    asyncio.run(scenario())


def test_batch_conflicting_vehicle_becomes_partial_and_releases_parking(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1")
            await cluster.vehicle("ev-2")
            res = await cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 2})
            batch = res.json()
            assert batch["recommended_count"] == 2
            second = batch["assignments"][1]["vehicle_id"]
            state = await cluster.fleet.get(second)
            await cluster.fleet.start_rental(second, RentalStart(
                rental_id=uuid4(), request_id=uuid4(), expected_version=state["state_version"],
                expected_end_at=clock.now() + timedelta(hours=1)))
            key = uuid4()
            approval = BatchApproval(request_id=key, batch_recommendation_id=batch["batch_recommendation_id"])
            result = await cluster.dispatch.approve_batch(approval)
            assert result["status"] == "PARTIAL"
            assert sum(row["state"] == "REJECTED" for row in result["items"]) == 1
            assert sum(row["state"] == "PENDING" for row in result["items"]) == 1
            assert result == await cluster.dispatch.approve_batch(approval)
            holds = list(cluster.facilities.holds.values())
            assert sum(h["status"] == "RESERVED" for h in holds) == 1
            assert len(holds) == 1
            other = await cluster.dispatch.get_batch(key)
            assert other == result
            admitted = next(row for row in result["items"] if row["state"] == "PENDING")
            child = cluster.dispatch.store.get(admitted["dispatch_id"])
            cluster.dispatch.transition(child, "UNKNOWN", "PROGRESS_TIMEOUT")
            after = await cluster.dispatch.get_batch(key)
            assert after["status"] == "PARTIAL"
            assert next(row for row in after["items"] if row["dispatch_id"] == admitted["dispatch_id"])[
                "approval_status"] == "APPROVED"
    asyncio.run(scenario())


def test_batch_arrival_commits_occupied_parking_capacity(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1")
            request = await cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            batch = request.json()
            allocation = batch["assignments"][0]
            approved = await cluster.dispatch.approve_batch(
                BatchApproval(request_id=uuid4(), batch_recommendation_id=batch["batch_recommendation_id"]))
            row = approved["items"][0]
            assert row["dispatch_id"]
            await cluster.telemetry(name="ev-1", soc=70, state="ARRIVED",
                dispatch_id=row["dispatch_id"], position=allocation["target_position"])
            result = await cluster.dispatch.get(row["dispatch_id"])
            assert result["state"] == "SUCCEEDED"
            assert result["parking_facility_id"] == allocation["facility_id"]
            assert result["parking_status"] == "OCCUPIED"
            receipt = await cluster.facilities.get_hold(row["dispatch_id"])
            assert receipt["status"] == "OCCUPIED"
    asyncio.run(scenario())


def test_closed_facility_and_invalid_quantity_rejected(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1")
            for value in (0, 21):
                response = await cluster.client.post(
                    "http://rebalancing.test/api/v1/batch-recommendations",
                    json={"target_zone_id": "cbd", "quantity": value})
                assert response.status_code == 422
            closed = BY_ID["mock-tampines-03"]
            with pytest.raises(HTTPException) as error:
                await cluster.facilities.reserve(closed.facility_id, ParkingHoldRequest(
                    dispatch_id=uuid4(), vehicle_id="ev-1",
                    expected_zone_id="tampines", expected_position=closed.position))
            assert error.value.detail == "PARKING_CLOSED"
    asyncio.run(scenario())


def test_competing_batches_cannot_double_claim_vehicles_or_parking(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            for vehicle in ("ev-1", "ev-2"):
                await cluster.vehicle(vehicle)
            recommendations = await asyncio.gather(*[
                cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                    json={"target_zone_id": "cbd", "quantity": 2})
                for _ in range(2)])
            assert all(r.status_code == 201 for r in recommendations)
            ids = [r.json()["batch_recommendation_id"] for r in recommendations]
            results = await asyncio.gather(*[
                cluster.dispatch.approve_batch(BatchApproval(request_id=uuid4(),
                    batch_recommendation_id=identifier)) for identifier in ids])
            assert sorted(r["status"] for r in results) == ["APPROVED", "REJECTED"]
            active = [h["receipt"] for h in cluster.facilities.holds.values()
                      if h["status"] in ("RESERVED", "OCCUPIED")]
            assert len(active) == 2
            assert len({hold["vehicle_id"] for hold in active}) == 2
            fleet = [await cluster.fleet.get(name) for name in ("ev-1", "ev-2")]
            assert sum(bool(vehicle["active_claim"]) for vehicle in fleet) == 2
    asyncio.run(scenario())


def test_charging_hold_requires_an_available_charger():
    async def scenario():
        store = FacilitiesStore()
        site = BY_ID["mock-cbd-03"]
        assert site.charging_available == 0
        with pytest.raises(HTTPException) as exc:
            await store.reserve(site.facility_id, ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id="ev-low-battery",
                expected_zone_id="cbd", expected_position=site.position,
                requires_charging=True))
        assert exc.value.detail == "CHARGING_FULL"
        assert (await store.get(site.facility_id)).parking_available == site.parking_available
    asyncio.run(scenario())

def test_close_unstarted_batch_item_blocks_late_child_retry(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            key, recommendation_id = uuid4(), uuid4()
            cluster.dispatch.batches[str(key)] = {
                "request_id": str(key), "batch_recommendation_id": str(uuid4()),
                "requested_count": 1, "recommended_count": 1,
                "items": [{"recommendation_id": str(recommendation_id), "vehicle_id": "ev-1",
                           "facility_id": "mock-cbd-01", "state": "PENDING"}],
            }
            path = f"http://dispatch.test/api/v1/batches/{key}/items/{recommendation_id}/close"
            closed = await cluster.client.post(path)
            assert closed.status_code == 200, closed.text
            assert closed.json()["status"] == "REJECTED"
            child = DispatchApproval(request_id=uuid5(key, str(recommendation_id)),
                                     recommendation_id=recommendation_id)
            result = await cluster.dispatch.approve(child)
            assert result["state"] == "REJECTED"
            assert result["command"] is None
            assert (await cluster.client.post(path)).json() == closed.json()

    asyncio.run(scenario())
