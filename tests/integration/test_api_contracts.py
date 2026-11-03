import asyncio
from contextlib import asynccontextmanager
import httpx
import pytest
from ev_fleet.fleet.app import create_app as fleet_app
from ev_fleet.demand.app import create_app as demand_app
from ev_fleet.rebalancing.app import create_app as rebalancing_app


@asynccontextmanager
async def app_client(app):
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            yield client


def test_http_validation_and_health():
    async def scenario():
        async with app_client(fleet_app()) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
            response = await client.post("/api/v1/vehicles", json={"vehicle_id": "bad"})
            assert response.status_code == 422
            assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
            assert (await client.get("/api/v1/vehicles/unknown")).status_code == 404

    asyncio.run(scenario())


def test_hotspot_contract(clock):
    async def scenario():
        async with app_client(demand_app()) as client:
            assert len((await client.get("/api/v1/zones")).json()["zones"]) == 6
            response = await client.put("/api/v1/zones/cbd/demand", json={
                "expected_version": 0,
                "observed_at": clock.now().isoformat(),
                "desired_available_vehicles": 12,
            })
            assert response.status_code == 200
            hotspots = (await client.get("/api/v1/hotspots")).json()["hotspots"]
            assert hotspots[0]["zone_id"] == "cbd"
            assert (await client.get("/api/v1/hotspots?threshold=0")).status_code == 422

    asyncio.run(scenario())


@pytest.mark.parametrize("failure,status", [("timeout", 504), ("unavailable", 503), ("malformed", 503), ("missing", 404)])
def test_dependency_errors(failure, status):
    async def scenario():
        attempts = []

        def handler(request):
            attempts.append(request.url)
            if failure == "timeout":
                raise httpx.ReadTimeout("test", request=request)
            if failure == "unavailable":
                return httpx.Response(503)
            if failure == "missing":
                return httpx.Response(404)
            return httpx.Response(200, json={"unexpected": True})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as upstream:
            async with app_client(rebalancing_app(client=upstream)) as client:
                response = await client.post(
                    "/api/v1/recommendations", json={"target_zone_id": "cbd"}
                )
                assert response.status_code == status
                assert "error" in response.json()

    asyncio.run(scenario())
