import asyncio
from datetime import datetime, timezone
import time
from uuid import uuid4
import pytest
from fastapi import HTTPException
from tests.demand_heatmap_support import DemandCluster
from tests.integration.test_dispatch_relocation_flow import relocation_cluster


def test_concurrent_snapshots_and_demand_changes_preserve_dispatch_contract(clock):
    async def scenario():
        async with DemandCluster(clock) as cluster:
            await cluster.vehicle()
            await cluster.set_demand(value=4)
            old_recommendation = await cluster.recommend()
            await cluster.set_demand(value=5)
            with pytest.raises(HTTPException) as caught:
                await cluster.approve(old_recommendation)
            assert caught.value.detail == "TARGET_CHANGED"
            dispatch, _ = await cluster.approve()
            key = dispatch["dispatch_id"]
            for value in range(6, 16):
                await asyncio.gather(cluster.set_demand(value=value),
                    cluster.telemetry(state="RELOCATING", dispatch_id=key), cluster.heatmap())
                view = await cluster.heatmap()
                cbd = next(row for row in view["zones"] if row["zone_id"] == "cbd")
                assert cbd["rental_demand"] == value
                assert cbd["current_shortage"] == value and cbd["projected_shortage"] == value - 1
                assert cbd["inbound_relocating_evs"] == 1
                current = await cluster.dispatch.get(key)
                assert current["state"] == "IN_PROGRESS" and current["command"] == dispatch["command"]
    asyncio.run(scenario())


def test_real_http_demand_updates_during_relocation(relocation_cluster):
    client, (fleet, demand, rebalancing, dispatch), logs = relocation_cluster
    deadline = time.monotonic() + 10
    while True:
        view = client.get(demand + "/api/v1/heat-map").json()
        if sum(row["available_evs"] or 0 for row in view["zones"]) == 2:
            break
        assert time.monotonic() < deadline, view
        time.sleep(.05)

    def update(value):
        zone = client.get(demand + "/api/v1/zones/cbd").json()
        response = client.put(demand + "/api/v1/zones/cbd/demand", json={
            "expected_version": zone["demand_version"], "observed_at": datetime.now(timezone.utc).isoformat(),
            "desired_available_vehicles": value})
        assert response.status_code == 200, response.text

    update(4)
    # A real telemetry race is a legitimate 409; this caller gets a fresh recommendation.
    for attempt in range(5):
        rec = client.post(rebalancing + "/api/v1/recommendations", json={"target_zone_id": "cbd"}).json()
        response = client.post(dispatch + "/api/v1/dispatches", json={
            "request_id": str(uuid4()), "recommendation_id": rec["recommendation_id"]})
        if response.status_code == 202:
            break
        assert response.status_code == 409 and response.json()["error"]["code"] == "VEHICLE_VERSION_CONFLICT", response.text
    assert response.status_code == 202, response.text
    approved = response.json()
    status_url = dispatch + "/api/v1/dispatches/" + approved["dispatch_id"]
    changed = False
    deadline = time.monotonic() + 20
    while True:
        view = client.get(demand + "/api/v1/heat-map").json()
        cbd = next(row for row in view["zones"] if row["zone_id"] == "cbd")
        status = client.get(status_url).json()
        assert status["command"] == approved["command"]
        if cbd["inbound_relocating_evs"] == 1 and not changed:
            assert cbd["available_evs"] == 0 and cbd["current_shortage"] == 4
            assert cbd["projected_shortage"] == 3
            update(7)
            changed = True
        if changed and status["state"] == "SUCCEEDED" and cbd["available_evs"] == 1:
            assert cbd["rental_demand"] == 7
            assert cbd["current_shortage"] == 6 and cbd["inbound_relocating_evs"] == 0
            break
        assert time.monotonic() < deadline, (view, status)
        time.sleep(.05)
