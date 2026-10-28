"""Kubernetes Scale JSON can omit zero-valued replica fields."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException


MODULE = Path(__file__).resolve().parents[2] / "simulation-console" / "backend" / "rentals.py"
spec = importlib.util.spec_from_file_location("rental_scale_controller", MODULE)
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


def test_replica_count_handles_omitted_zero_fields_and_rejects_bad_payloads():
    assert controller.replica_count({"spec": {}, "status": {}}, "spec") == 0
    assert controller.replica_count({"spec": {}, "status": {}}, "status") == 0
    assert controller.replica_count({"spec": {}}, "status") == 0
    assert controller.replica_count({"spec": {"replicas": 1}}, "spec") == 1
    for invalid in ({}, {"spec": None}, {"spec": {"replicas": "0"}},
                    {"spec": {"replicas": -1}}, {"spec": {"replicas": True}}):
        with pytest.raises(HTTPException) as error:
            controller.replica_count(invalid, "spec")
        assert error.value.status_code == 503
        assert error.value.detail == "RENTAL_SCALE_RESPONSE_INVALID"


def test_paused_rental_status_and_start_with_omitted_zero(monkeypatch):
    calls = []

    async def cluster_request(method, path, payload=None):
        calls.append((method, path, payload))
        if path == controller.JOB_PATH:
            return None
        if method == "GET":
            return {"spec": {}, "status": {}}
        assert method == "PUT"
        assert payload["spec"]["replicas"] == 1
        return {"spec": {"replicas": 1}, "status": {}}

    monkeypatch.setattr(controller, "cluster_request", cluster_request)

    async def verify():
        assert await controller.status() == {
            "running": False, "desired_replicas": 0, "actual_replicas": 0
        }
        assert await controller.start() == {"running": True, "desired_replicas": 1}

    asyncio.run(verify())
    assert any(c[0] == "PUT" and c[1] == controller.SCALE_PATH for c in calls)


def test_stop_handles_omitted_zero_in_put_response(monkeypatch):
    async def cluster_request(method, path, payload=None):
        if method == "GET":
            return {"spec": {"replicas": 1}, "status": {"replicas": 1}}
        assert method == "PUT"
        assert payload["spec"]["replicas"] == 0
        return {"spec": {}, "status": {}}

    class FakeFleetResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"vehicles": []}

    class FakeFleetClient:
        async def get(self, url):
            return FakeFleetResponse()

    monkeypatch.setattr(controller, "cluster_request", cluster_request)
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(client=FakeFleetClient(), service_urls={"fleet": "http://fleet"})
    ))
    assert asyncio.run(controller.stop(request)) == {"running": False, "desired_replicas": 0}
