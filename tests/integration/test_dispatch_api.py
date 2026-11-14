import asyncio
from uuid import uuid4
from tests.dispatch_support import Cluster


def test_approval_poll_ack_and_errors_through_http(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle()
            rec = await cluster.recommend()
            body = {"request_id": str(uuid4()), "recommendation_id": rec["recommendation_id"]}
            client = cluster.client
            response = await client.post("http://dispatch.test/api/v1/dispatches", json=body)
            assert response.status_code == 202, response.text
            dispatch = response.json()
            retry = await client.post("http://dispatch.test/api/v1/dispatches", json=body)
            assert retry.json()["dispatch_id"] == dispatch["dispatch_id"]
            poll = await client.get("http://dispatch.test/api/v1/vehicles/ev-1/commands")
            command = poll.json()["commands"][0]
            url = "http://dispatch.test/api/v1/dispatches/" + dispatch["dispatch_id"]
            ack = await client.post(url + "/acknowledgements", json={
                "command_id": command["command_id"], "vehicle_id": "ev-1", "accepted": True})
            assert ack.status_code == 200 and ack.json()["state"] == "ACKNOWLEDGED"
            mismatch = await client.post(url + "/acknowledgements", json={
                "command_id": str(uuid4()), "vehicle_id": "ev-1", "accepted": True})
            assert mismatch.status_code == 409
            assert mismatch.json()["error"]["request_id"] == mismatch.headers["X-Request-ID"]
            listing = await client.get("http://dispatch.test/api/v1/dispatches?vehicle_id=ev-1")
            assert len(listing.json()["dispatches"]) == 1
            assert (await client.get("http://dispatch.test/health/ready")).status_code == 200
    asyncio.run(scenario())


def test_route_setup_is_readable_but_never_exposes_tokens(clock, monkeypatch):
    async def scenario():
        monkeypatch.setenv("ROUTING_PROVIDER", "SIMULATED")
        monkeypatch.delenv("ONEMAP_ACCESS_TOKEN", raising=False)
        async with Cluster(clock) as cluster:
            response = await cluster.client.get("http://dispatch.test/api/v1/routing/status")
            assert response.status_code == 200
            assert response.json() == {
                "provider": "SIMULATED", "configured": False,
                "reason_code": "ONEMAP_ROUTE_REQUIRED",
            }

        monkeypatch.setenv("ROUTING_PROVIDER", "ONEMAP")
        monkeypatch.setenv("ONEMAP_ACCESS_TOKEN", "private-test-value")
        async with Cluster(clock) as cluster:
            response = await cluster.client.get("http://dispatch.test/api/v1/routing/status")
            assert response.status_code == 200
            assert response.json() == {
                "provider": "ONEMAP", "configured": True, "reason_code": None,
            }
            assert "private-test-value" not in response.text

    asyncio.run(scenario())


def test_offline_demo_routes_are_approved_and_labelled_but_simulated_is_not(clock, monkeypatch):
    from unittest.mock import patch
    from ev_fleet.dispatch.routing import driving_route
    from ev_fleet.simulator.movement import Movement

    async def scenario(provider):
        monkeypatch.setenv("ROUTING_PROVIDER", provider)
        monkeypatch.delenv("ONEMAP_ACCESS_TOKEN", raising=False)
        async with Cluster(clock) as cluster:
            # Use the real provider, not Cluster's test-only ONEMAP fixture.
            cluster.stack.enter_context(patch("ev_fleet.dispatch.service.driving_route", new=driving_route))
            await cluster.vehicle()
            rec = await cluster.recommend()
            body = {"request_id": str(uuid4()), "recommendation_id": rec["recommendation_id"]}
            response = await cluster.client.post("http://dispatch.test/api/v1/dispatches", json=body)
            assert response.status_code == 202, response.text
            if response.json()["state"] == "REJECTED":
                return response.json(), None
            status = (await cluster.client.get("http://dispatch.test/api/v1/routing/status")).json()
            assert status == {"provider": "OFFLINE_DEMO", "configured": True, "reason_code": "OFFLINE_DEMO_ROUTES"}
            poll = await cluster.client.get("http://dispatch.test/api/v1/vehicles/ev-1/commands")
            return response.json(), poll.json()["commands"][0]

    dispatch, command = asyncio.run(scenario("OFFLINE_DEMO"))
    assert command["route"]["source"] == "OFFLINE_DEMO"
    mover = Movement({"latitude": 1.35, "longitude": 103.8513}, 80)
    assert mover.accept(command) is True

    dispatch, command = asyncio.run(scenario("SIMULATED"))
    assert command is None and dispatch["failure_reason"] == "ONEMAP_ROUTE_REQUIRED"
