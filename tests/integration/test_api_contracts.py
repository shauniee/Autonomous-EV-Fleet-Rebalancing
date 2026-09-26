import asyncio
import httpx
import pytest
from fastapi.testclient import TestClient
from ev_fleet.fleet.app import create_app as fleet_app
from ev_fleet.demand.app import create_app as demand_app
from ev_fleet.rebalancing.app import create_app as rebalancing_app


def test_http_validation_and_health():
    with TestClient(fleet_app()) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 200
        response = client.post("/api/v1/vehicles", json={"vehicle_id": "bad"})
        assert response.status_code == 422
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]
        assert client.get("/api/v1/vehicles/unknown").status_code == 404


def test_hotspot_contract(clock):
    with TestClient(demand_app()) as client:
        assert len(client.get("/api/v1/zones").json()["zones"]) == 6
        response = client.put("/api/v1/zones/cbd/demand", json={"expected_version": 0,
                             "observed_at": clock.now().isoformat(), "desired_available_vehicles": 12})
        assert response.status_code == 200
        assert client.get("/api/v1/hotspots").json()["hotspots"][0]["zone_id"] == "cbd"
        assert client.get("/api/v1/hotspots?threshold=0").status_code == 422


@pytest.mark.parametrize("failure,status", [("timeout", 504), ("unavailable", 503), ("malformed", 503), ("missing", 404)])
def test_dependency_errors(failure, status):
    attempts = []
    def handler(request):
        attempts.append(request.url)
        if failure == "timeout": raise httpx.ReadTimeout("test", request=request)
        if failure == "unavailable": return httpx.Response(503)
        if failure == "missing": return httpx.Response(404)
        return httpx.Response(200, json={"unexpected": True})
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with TestClient(rebalancing_app(client=upstream)) as client:
        response = client.post("/api/v1/recommendations", json={"target_zone_id": "cbd"})
        assert response.status_code == status
        assert "error" in response.json()
    asyncio.run(upstream.aclose())
