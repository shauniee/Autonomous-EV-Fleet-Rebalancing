import asyncio

import httpx
import pytest
from pydantic import ValidationError

from ev_fleet.contracts.facilities import Facility
from ev_fleet.facilities.app import create_app
from ev_fleet.facilities.catalog import CATALOGUE


def test_mock_catalogue_is_deterministic_and_capacity_safe():
    assert len(CATALOGUE) == 15
    assert len({site.facility_id for site in CATALOGUE}) == 15
    assert len({site.zone_id for site in CATALOGUE}) == 6
    assert any(site.status == "CLOSED" for site in CATALOGUE)
    assert any(site.charging_bays > 0 for site in CATALOGUE)
    for site in CATALOGUE:
        assert site.facility_id.startswith("mock-")
        assert site.source == "MOCK"
        assert 0 <= site.charging_available <= site.charging_bays <= site.parking_capacity
        assert 0 <= site.charging_available <= site.parking_available <= site.parking_capacity


def test_facilities_api_read_only_and_filterable():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app()), base_url="http://test") as client:
            result = await client.get("/api/v1/facilities")
            assert result.status_code == 200
            assert result.json()["data_source"] == "MOCK"
            assert len(result.json()["facilities"]) == 15
            cbd = await client.get("/api/v1/facilities", params={"zone_id": "cbd"})
            assert cbd.status_code == 200
            assert len(cbd.json()["facilities"]) == 3
            assert all(item["zone_id"] == "cbd" for item in cbd.json()["facilities"])
            single = await client.get("/api/v1/facilities/mock-cbd-01")
            assert single.status_code == 200 and single.json()["source"] == "MOCK"
            missing = await client.get("/api/v1/facilities/no-such-facility")
            assert missing.status_code == 404 and missing.json()["error"]["code"] == "FACILITY_NOT_FOUND"
            bad_zone = await client.get("/api/v1/facilities", params={"zone_id": "unknown"})
            assert bad_zone.status_code == 404 and bad_zone.json()["error"]["code"] == "ZONE_NOT_FOUND"
            assert (await client.post("/api/v1/facilities", json={})).status_code == 405
    asyncio.run(scenario())


def test_facility_rejects_double_counted_charging_capacity():
    payload = CATALOGUE[0].model_dump()
    payload["charging_available"] = payload["parking_available"] + 1
    with pytest.raises(ValidationError):
        Facility.model_validate(payload)
    payload = CATALOGUE[0].model_dump()
    payload["charging_bays"] = payload["parking_capacity"] + 1
    with pytest.raises(ValidationError):
        Facility.model_validate(payload)


def test_vacate_frees_occupied_bay_once_and_keeps_connected_cable():
    from uuid import uuid4
    from ev_fleet.contracts.facilities import ParkingHoldRequest
    from ev_fleet.facilities.store import FacilitiesStore

    async def occupy(store, site, vehicle_id):
        hold = ParkingHoldRequest(dispatch_id=uuid4(), vehicle_id=vehicle_id,
                                  expected_zone_id=site.zone_id, expected_position=site.position)
        await store.reserve(site.facility_id, hold)
        await store.change(hold.dispatch_id, "OCCUPIED")
        return hold.dispatch_id

    async def scenario():
        store = FacilitiesStore()
        app = create_app(store)
        site = next(s for s in CATALOGUE if s.facility_type == "PARKING"
                    and s.status == "OPEN" and s.parking_available > 1)
        before = (await store.get(site.facility_id)).parking_available
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            await occupy(store, site, "customer-ev-01")
            assert (await store.get(site.facility_id)).parking_available == before - 1
            vacated = await client.post("/api/v1/vehicles/customer-ev-01/parking/vacate")
            assert vacated.status_code == 200 and vacated.json()["parking"] is None
            assert (await store.get(site.facility_id)).parking_available == before
            version = store.version
            again = await client.post("/api/v1/vehicles/customer-ev-01/parking/vacate")
            assert again.status_code == 200 and store.version == version

            # A car still tethered to a charger has not left its bay.
            dispatch_id = await occupy(store, site, "customer-ev-02")
            store.holds[str(dispatch_id)]["cable"] = {"status": "CONNECTED"}
            tethered = await client.post("/api/v1/vehicles/customer-ev-02/parking/vacate")
            assert tethered.status_code == 409
            assert tethered.json()["error"]["code"] == "CHARGING_CABLE_CONNECTED"
            assert store.holds[str(dispatch_id)]["status"] == "OCCUPIED"

    asyncio.run(scenario())
