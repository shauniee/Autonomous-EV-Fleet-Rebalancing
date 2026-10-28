"""LTA contract fixtures; these are illustrative, not captured live API responses."""
import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from pydantic import SecretStr

from ev_fleet.contracts.facilities import ParkingHoldRequest
from ev_fleet.facilities.app import create_app
from ev_fleet.facilities.datamall import (
    DataMallClient, DataMallError, FacilitiesSettings,
    charging_sites, parking_sites, refresh_feed,
)
from ev_fleet.facilities.store import FacilitiesStore
from tests.phase2_support import Cluster
from ev_fleet.contracts.dispatch import BatchApproval
from tests.unit.test_persistence import MemoryRepository


def parking_row(identifier="A1", **overrides):
    return {"CarParkID": identifier, "Area": "Marina", "Development": "Example carpark",
            "Location": "1.283 103.851", "AvailableLots": 4, "LotType": "C",
            "Agency": "LTA", **overrides}


def charging_row():
    return {"name": "Example station", "address": "Example address", "locationId": "123456123456",
            "latitude": 1.283, "longtitude": 103.851, "status": "1",
            "chargingPoints": [
                {"id": "charger-1", "status": 1, "plugTypes": [
                    {"plugType": "CCS2", "evIds": [
                        {"evCpId": "R123456A-001", "status": 1},
                        {"evCpId": "R123456A-002", "status": "1"}]}]},
                {"id": "charger-2", "status": 100, "plugTypes": [{"plugType": "Type 2"}],
                 "evIds": [{"evCpId": "R123456B-001", "status": ""}]},
                {"id": "charger-3", "status": 0, "plugTypes": [{"plugType": "CCS2"}],
                 "evIds": [{"evCpId": "R123456C-001", "status": 0}]},
            ]}


def live_store(clock):
    return FacilitiesStore(clock=clock, settings=FacilitiesSettings(
        source="LTA_DATAMALL", account_key=SecretStr("fixture-key")))


def test_carparks_filter_cars_preserve_coordinates_and_unknown_totals(clock):
    records = [parking_row(), parking_row("bike", LotType="Y"),
               parking_row("missing", Location=""), parking_row("invalid", AvailableLots=-1),
               parking_row("outside", Location="0 0")]
    sites = parking_sites(records, clock.now(), 180)
    assert len(sites) == 1
    site = sites[0]
    assert site.facility_id == "lta-parking-LTA-A1"
    assert site.position.latitude == 1.283 and site.position.longitude == 103.851
    assert site.zone_id == "cbd"
    assert site.parking_capacity is None and site.charging_bays is None
    assert site.reported_parking_available == site.parking_available == 4
    assert site.availability_expires_at == clock.now() + timedelta(seconds=180)


def test_same_carpark_code_at_different_agencies_is_not_merged(clock):
    sites = parking_sites([parking_row(), parking_row(Agency="HDB")], clock.now(), 180)
    assert len({site.facility_id for site in sites}) == 2


def test_ev_connectors_are_not_double_counted_as_vehicle_bays(clock):
    site = charging_sites([charging_row()], clock.now(), 900)[0]
    assert site.facility_type == "CHARGING" and site.availability_basis == "CHARGERS"
    assert site.charging_bays == 3 and site.charging_available == 1
    assert site.parking_capacity is None and site.reported_parking_available is None
    assert site.reported_charging_available == 1
    assert site.connector_types == ["CCS2", "Type 2"]


def test_charger_aggregate_status_does_not_override_unknown_connector_status(clock):
    row = charging_row()
    for charger in row["chargingPoints"]:
        charger.pop("evIds", None)
        for plug in charger["plugTypes"]:
            plug.pop("evIds", None)
    assert charging_sites([row], clock.now(), 900)[0].charging_available == 0


def test_pagination_and_account_key_header():
    async def scenario():
        requests = []
        def respond(request):
            requests.append(request)
            offset = int(request.url.params["$skip"])
            return httpx.Response(200, json={"value": [parking_row(str(i))
                for i in range(offset, min(offset + 500, 501))]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            data = await DataMallClient(client, SecretStr("fixture-key")).parking()
        assert len(data) == 501
        assert [r.url.params["$skip"] for r in requests] == ["0", "500"]
        assert all(r.headers["AccountKey"] == "fixture-key" for r in requests)
    asyncio.run(scenario())


def test_batch_download_does_not_forward_account_key():
    async def scenario():
        requests = []
        def respond(request):
            requests.append(request)
            if request.url.host == "datamall2.mytransport.sg":
                assert request.url.path.endswith("/EVCBatch")
                return httpx.Response(200, json={"value": [{"Link":
                    "https://dm-traffic-flow-data.s3.ap-southeast-1.amazonaws.com/ev-batch/data.json?signature=secret"}]})
            return httpx.Response(200, json=[charging_row()])
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await DataMallClient(client, SecretStr("fixture-key")).charging()
        assert len(result) == 1 and len(requests) == 2
        assert requests[0].headers["AccountKey"] == "fixture-key"
        assert "AccountKey" not in requests[1].headers
    asyncio.run(scenario())


@pytest.mark.parametrize("link", ["http://example.com/key", "https://localhost/data", 
    "https://dm.s3.amazonaws.com.evil.test/data", "https://user:pass@dm.s3.amazonaws.com/data"])
def test_untrusted_download_links_are_rejected(link):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"value": [{"Link": link}]}))) as client:
            with pytest.raises(DataMallError, match="LTA_INVALID_DOWNLOAD_LINK"):
                await DataMallClient(client, SecretStr("fixture-key")).charging()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,code", [(401, "LTA_AUTH_FAILED"), (403, "LTA_AUTH_FAILED"),
                                        (429, "LTA_RATE_LIMITED"), (500, "LTA_REQUEST_FAILED")])
def test_upstream_errors_do_not_expose_key_or_response_body(status, code):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(status, text="fixture-key"))) as client:
            with pytest.raises(DataMallError) as error:
                await DataMallClient(client, SecretStr("fixture-key")).parking()
        assert str(error.value) == code and "fixture-key" not in str(error.value)
    asyncio.run(scenario())


def test_repeated_pagination_is_rejected():
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"value": [parking_row(str(i)) for i in range(500)]}))) as client:
            with pytest.raises(DataMallError, match="LTA_INVALID_PAGINATION"):
                await DataMallClient(client, SecretStr("fixture-key")).parking()
    asyncio.run(scenario())


def test_refresh_failure_retains_last_observation_then_excludes_stale_data(clock):
    async def scenario():
        store = live_store(clock)
        class Provider:
            fail = False
            async def parking(self):
                if self.fail:
                    raise DataMallError("LTA_AUTH_FAILED")
                return [parking_row()]
        provider = Provider()
        await refresh_feed(store, provider, "parking")
        before = await store.view()
        provider.fail = True
        clock.advance(10)
        await refresh_feed(store, provider, "parking")
        after = await store.view()
        assert after.facilities[0].availability_observed_at == before.facilities[0].availability_observed_at
        assert after.feeds[0].status == "ERROR"
        assert after.feeds[0].error_code == "LTA_AUTH_FAILED"
        clock.advance(170)
        stale = await store.view()
        assert stale.feeds[0].status == "STALE"
        assert stale.facilities[0].status == "UNKNOWN"
        assert stale.facilities[0].parking_available == 0
        assert stale.facilities[0].reported_parking_available == 4
        with pytest.raises(HTTPException, match="FACILITY_DATA_STALE"):
            site = stale.facilities[0]
            await store.reserve(site.facility_id, ParkingHoldRequest(
                dispatch_id=uuid4(), vehicle_id="ev-1", expected_zone_id=site.zone_id,
                expected_position=site.position))
    asyncio.run(scenario())


def test_live_updates_preserve_simulated_holds_and_never_report_negative_capacity(clock):
    async def scenario():
        store = live_store(clock)
        sites = parking_sites([parking_row()], clock.now(), 180)
        await store.replace_feed("parking", sites, clock.now())
        site = sites[0]
        receipt = await store.reserve(site.facility_id, ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-1", expected_zone_id=site.zone_id,
            expected_position=site.position))
        for available, expected in [(2, 1), (0, 0), (4, 3)]:
            await store.replace_feed("parking", parking_sites(
                [parking_row(AvailableLots=available)], clock.now(), 180), clock.now())
            assert (await store.get(site.facility_id)).parking_available == expected
            assert (await store.get_hold(receipt["dispatch_id"]))["status"] == "RESERVED"
    asyncio.run(scenario())


def test_missing_key_returns_explicit_empty_live_snapshot_without_mock_fallback(clock):
    async def scenario():
        store = FacilitiesStore(clock=clock, settings=FacilitiesSettings(source="LTA_DATAMALL"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(store)), base_url="http://test") as client:
            response = await client.get("/api/v1/facilities")
            snapshot = response.json()
            assert snapshot["data_source"] == "LTA_DATAMALL" and snapshot["facilities"] == []
            assert all(feed["status"] == "MISSING_KEY" for feed in snapshot["feeds"])
            assert "account_key" not in response.text
    asyncio.run(scenario())


@pytest.mark.parametrize("charging_fails", [False, True])
def test_app_lifecycle_starts_independent_feeds_and_cancels_pollers(clock, monkeypatch, charging_fails):
    class Provider:
        def __init__(self, client, account_key):
            pass
        async def parking(self):
            return [parking_row()]
        async def charging(self):
            if charging_fails:
                raise DataMallError("LTA_AUTH_FAILED")
            return [charging_row()]
    monkeypatch.setattr("ev_fleet.facilities.app.DataMallClient", Provider)
    async def scenario():
        app = create_app(live_store(clock))
        async with app.router.lifespan_context(app):
            for _ in range(100):
                snapshot = await app.state.store.view()
                if all(feed.status != "PENDING" for feed in snapshot.feeds):
                    break
                await asyncio.sleep(0)
            assert snapshot.feeds[0].status == "FRESH"
            assert snapshot.feeds[1].status == ("ERROR" if charging_fails else "FRESH")
            assert len(snapshot.facilities) == (1 if charging_fails else 2)
        assert all(task.done() for task in app.state.feed_tasks)
        assert app.state.datamall_client.is_closed
    asyncio.run(scenario())


def test_real_observations_and_holds_restore_without_resetting_freshness(clock):
    async def scenario():
        repository = MemoryRepository()
        original = live_store(clock)
        original.repository = repository
        await original.start()
        site = parking_sites([parking_row()], clock.now(), 180)[0]
        await original.replace_feed("parking", [site], clock.now())
        receipt = await original.reserve(site.facility_id, ParkingHoldRequest(
            dispatch_id=uuid4(), vehicle_id="ev-1", expected_zone_id=site.zone_id,
            expected_position=site.position))
        clock.advance(180)
        restored = live_store(clock)
        restored.repository = repository
        await restored.start()
        snapshot = await restored.view()
        assert snapshot.facilities[0].availability_observed_at == site.availability_observed_at
        assert snapshot.facilities[0].status == "UNKNOWN"
        assert snapshot.feeds[0].status == "STALE"
        assert (await restored.get_hold(receipt["dispatch_id"]))["status"] == "RESERVED"
    asyncio.run(scenario())


@pytest.mark.parametrize("feed,soc,identifier", [
    ("parking", 85, "lta-parking-LTA-A1"),
    ("charging", 30, "lta-charging-123456123456"),
])
def test_real_site_can_be_recommended_approved_and_reached(clock, feed, soc, identifier):
    async def scenario():
        async with Cluster(clock) as cluster:
            cluster.facilities.settings = FacilitiesSettings(source="LTA_DATAMALL", account_key=SecretStr("fixture-key"))
            cluster.facilities.clock = clock
            cluster.facilities.catalogue = {}
            cluster.facilities.feeds = live_store(clock).feeds
            convert = parking_sites if feed == "parking" else charging_sites
            records = [parking_row()] if feed == "parking" else [charging_row()]
            await cluster.facilities.replace_feed(feed, convert(records, clock.now(), 180), clock.now())
            await cluster.vehicle("ev-lta", soc=soc)
            proposal = await cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            assert proposal.status_code == 201, proposal.text
            data = proposal.json()
            assert data["recommended_count"] == 1
            assignment = data["assignments"][0]
            assert assignment["facility_id"] == identifier
            assert assignment["requires_charging"] is (feed == "charging")
            assert assignment["target_position"] == {"latitude": 1.283, "longitude": 103.851}
            result = await cluster.dispatch.approve_batch(BatchApproval(
                request_id=uuid4(), batch_recommendation_id=data["batch_recommendation_id"]))
            assert result["status"] == "APPROVED"
            dispatch_id = result["items"][0]["dispatch_id"]
            await cluster.telemetry(name="ev-lta", soc=soc - 5, state="ARRIVED",
                dispatch_id=dispatch_id, position=assignment["target_position"])
            arrived = await cluster.dispatch.get(dispatch_id)
            assert arrived["state"] == "SUCCEEDED"
            assert arrived["parking_status"] == "OCCUPIED"
            clock.advance(180)
            await cluster.telemetry(name="ev-lta", soc=soc - 5, position=assignment["target_position"])
            proposal = await cluster.client.post("http://rebalancing.test/api/v1/batch-recommendations",
                json={"target_zone_id": "cbd", "quantity": 1})
            assert proposal.json()["recommended_count"] == 0
    asyncio.run(scenario())
