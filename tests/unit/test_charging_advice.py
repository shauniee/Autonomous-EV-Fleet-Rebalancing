"""Safety and end-to-end API regression coverage for charger advice."""
import asyncio

import pytest
from pydantic import ValidationError

from ev_fleet.common.settings import Settings
from ev_fleet.contracts.recommendations import BatchRecommendationRequest
from ev_fleet.facilities.catalog import BY_ID
from ev_fleet.rebalancing.charging_advice import build_charging_advice
from tests.phase2_support import Cluster


def vehicle_at(site, now, soc=25):
    return {
        "vehicle_id": "ev-low", "state_version": 3,
        "battery_capacity_kwh": 60.0, "consumption_kwh_per_km": .18,
        "stale": False, "connectivity": "ONLINE",
        "active_rental": None, "active_claim": None, "active_charging_task": None,
        "telemetry": {
            "observed_at": now.isoformat(),
            "position": site.position.model_dump(mode="json"), "soc_pct": soc,
            "availability": "AVAILABLE", "relocation_state": "IDLE",
            "active_dispatch_id": None, "charging_state": "NOT_CHARGING",
        },
    }


def test_charger_advice_selects_nearest_reachable_with_real_bay(clock):
    near, far = BY_ID["mock-bishan-01"], BY_ID["mock-cbd-01"]
    v = vehicle_at(near, clock.now())
    result = build_charging_advice({"vehicles": [v]}, [far, near], Settings(), clock.now())
    row = result["vehicles"][0]
    assert result["trigger_soc_pct"] == 25
    assert row["status"] == "RECOMMENDED"
    assert row["charger"]["facility_id"] == near.facility_id
    assert row["charger"]["distance_km"] == 0
    assert row["charger"]["predicted_arrival_soc_pct"] == 25


def test_charger_advice_fails_closed_for_ineligible_or_unreachable(clock):
    site = BY_ID["mock-bishan-01"]
    v = vehicle_at(site, clock.now(), soc=19)
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert result["vehicles"][0]["status"] == "INELIGIBLE"
    assert "BELOW_OPERATIONAL_RESERVE" in result["vehicles"][0]["reasons"]

    v = vehicle_at(site, clock.now())
    v["active_rental"] = {"rental_id": "test"}
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert "RENTED" in result["vehicles"][0]["reasons"]

    v = vehicle_at(site, clock.now())
    v["stale"] = True
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert "STALE_TELEMETRY" in result["vehicles"][0]["reasons"]

    v = vehicle_at(site, clock.now())
    v["active_charging_task"] = {"task_id": "task"}
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert "CHARGING_TASK" in result["vehicles"][0]["reasons"]

    v = vehicle_at(site, clock.now())
    v["active_claim"] = {"dispatch_id": "dispatch"}
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert "BUSY" in result["vehicles"][0]["reasons"]

    unavailable = site.model_copy(update={"charging_available": 0})
    result = build_charging_advice({"vehicles": [vehicle_at(site, clock.now())]}, [unavailable], Settings(), clock.now())
    assert result["vehicles"][0]["status"] == "NO_SAFE_CHARGER"

    far = BY_ID["mock-woodlands-01"]
    result = build_charging_advice({"vehicles": [vehicle_at(site, clock.now())]}, [far], Settings(), clock.now())
    assert result["vehicles"][0]["status"] == "NO_SAFE_CHARGER"


def test_charger_advice_only_triggers_at_25_or_less(clock):
    site = BY_ID["mock-bishan-01"]
    v = vehicle_at(site, clock.now(), soc=25.01)
    result = build_charging_advice({"vehicles": [v]}, [site], Settings(), clock.now())
    assert result["vehicles"] == []


def test_exact_charger_filter_requires_explicit_single_ev_charge():
    request = BatchRecommendationRequest(target_zone_id="bishan", quantity=1, purpose="CHARGE",
        vehicle_id="ev-low", facility_id="mock-bishan-01")
    assert request.facility_id == "mock-bishan-01"
    for invalid in [
        {"quantity": 2, "vehicle_id": "ev-low", "facility_id": "mock-bishan-01"},
        {"quantity": 1, "vehicle_id": None, "facility_id": "mock-bishan-01"},
        {"quantity": 1, "vehicle_id": "ev-low", "facility_id": "mock-bishan-01", "purpose": "REBALANCE"},
    ]:
        with pytest.raises(ValidationError):
            BatchRecommendationRequest(target_zone_id="bishan", purpose="CHARGE", **invalid)


def test_advisory_to_exact_charge_batch_requires_dispatch_approval(clock):
    async def scenario():
        target = BY_ID["mock-bishan-01"]
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-low", soc=25)
            await cluster.telemetry("ev-low", soc=25, position=target.position.model_dump(mode="json"))
            before = (await cluster.fleet.snapshot())["snapshot_version"]
            response = await cluster.client.get("http://rebalancing.test/api/v1/charging-advice")
            assert response.status_code == 200, response.text
            item = next(v for v in response.json()["vehicles"] if v["vehicle_id"] == "ev-low")
            assert item["status"] == "RECOMMENDED", item
            assert item["charger"]["facility_id"] == target.facility_id
            assert (await cluster.fleet.snapshot())["snapshot_version"] == before
            assert not cluster.facilities.holds

            request = {"target_zone_id": "bishan", "purpose": "CHARGE",
                       "vehicle_id": "ev-low", "quantity": 1,
                       "facility_id": target.facility_id}
            proposed = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations", json=request)
            assert proposed.status_code == 201, proposed.text
            assert proposed.json()["recommended_count"] == 1
            assert proposed.json()["assignments"][0]["facility_id"] == target.facility_id
            assert proposed.json()["assignments"][0]["requires_charging"] is True
            assert not cluster.facilities.holds
            assert (await cluster.fleet.get("ev-low"))["active_claim"] is None

            request["facility_id"] = "mock-bishan-02"  # parking-only facility
            unavailable = await cluster.client.post(
                "http://rebalancing.test/api/v1/batch-recommendations", json=request)
            assert unavailable.status_code == 201
            assert unavailable.json()["recommended_count"] == 0
            assert not cluster.facilities.holds
    asyncio.run(scenario())
