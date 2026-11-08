"""Simulation Console demand control validates every upstream outcome."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException


MODULE = Path(__file__).resolve().parents[2] / "simulation-console" / "backend" / "demand.py"
spec = importlib.util.spec_from_file_location("simulation_console_demand", MODULE)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def status_payload(*, enabled=False, running=False, tick_count=0):
    return {
        "enabled": enabled,
        "running": running,
        "tick_count": tick_count,
        "last_tick_at": None,
        "last_error": None,
        "configuration": {"update_interval_seconds": 30},
        "zones": {},
    }


async def request_with(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        client=client, service_urls={"demand": "http://demand.test"})))
    return request, client


def test_status_and_actions_return_validated_demand_state():
    seen = []

    def handler(request):
        seen.append((request.method, request.url.path))
        if request.method == "GET":
            return httpx.Response(200, json=status_payload())
        return httpx.Response(200, json=status_payload(
            enabled=request.url.path.endswith("/start"),
            running=request.url.path.endswith("/start"),
            tick_count=1,
        ))

    async def scenario():
        request, client = await request_with(handler)
        try:
            assert (await controller.simulation_status(request))["enabled"] is False
            started = await controller.control_simulation("start", request)
            assert started["enabled"] is True and started["tick_count"] == 1
            paused = await controller.control_simulation("pause", request)
            assert paused["enabled"] is False and paused["running"] is False
            with pytest.raises(HTTPException) as error:
                await controller.control_simulation("restart", request)
            assert error.value.status_code == 422
        finally:
            await client.aclose()

    asyncio.run(scenario())
    assert seen == [
        ("GET", "/api/v1/demand-simulation"),
        ("POST", "/api/v1/demand-simulation/start"),
        ("POST", "/api/v1/demand-simulation/pause"),
    ]


@pytest.mark.parametrize(
    "outcome,expected_status",
    [("http", 502), ("timeout", 503), ("invalid", 502)],
)
def test_control_failure_never_claims_success(outcome, expected_status):
    def handler(request):
        if outcome == "http":
            return httpx.Response(500, json={"error": "failed"})
        if outcome == "timeout":
            raise httpx.ReadTimeout("test timeout", request=request)
        return httpx.Response(200, json={"enabled": True})

    async def scenario():
        request, client = await request_with(handler)
        try:
            with pytest.raises(HTTPException) as error:
                await controller.control_simulation("start", request)
            assert error.value.status_code == expected_status
        finally:
            await client.aclose()

    asyncio.run(scenario())
