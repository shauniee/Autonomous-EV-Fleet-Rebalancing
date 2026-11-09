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
