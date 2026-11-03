"""Simulation analytics: fail-closed inputs, read-only observations and honest CSV."""
import asyncio
from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

MODULE = Path(__file__).resolve().parents[2] / "simulation-console" / "backend" / "analytics.py"
spec = importlib.util.spec_from_file_location("sim_analytics", MODULE)
analytics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analytics)


def test_quantities_and_boundaries():
    assert analytics._millicores("200m") == 200
    assert analytics._millicores("1") == 1000
    assert analytics._millicores("250000u") == 250
    assert analytics._millicores("100000000n") == 100
    assert analytics._memory_mib("1024Ki") == 1
    assert analytics._memory_mib("2Gi") == 2048
    assert analytics._memory_mib("1048576") == 1
    with pytest.raises(ValueError):
        analytics._millicores("bad")
    with pytest.raises(ValueError):
        analytics._memory_mib("bad")


def test_presets_are_bounded_and_extra_inputs_forbidden():
    assert analytics.PRESETS["NORMAL"]["requests_per_second"] == 2
    assert analytics.PRESETS["EXTREME"]["requests_per_second"] == 200
    assert analytics.StartRequest(preset="HIGH", duration_seconds=90).duration_seconds == 90
    for payload in ({"preset": "EXTREME", "duration_seconds": 121},
                    {"preset": "NORMAL", "duration_seconds": 3},
                    {"preset": "UNBOUNDED"}, {"preset": "NORMAL", "target": "http://example.com"}):
        with pytest.raises(ValidationError):
            analytics.StartRequest(**payload)


def test_disabled_load_rejects_without_mutating_anything(monkeypatch):
    monkeypatch.delenv("ANALYTICS_LOAD_ENABLED", raising=False)
    async def verify():
        with pytest.raises(HTTPException) as error:
            await analytics.start_experiment(analytics.StartRequest(preset="NORMAL"))
        assert error.value.status_code == 503
        assert error.value.detail == "LOCAL_LOAD_TEST_DISABLED"
    asyncio.run(verify())


def test_read_only_hpa_and_pod_metrics(monkeypatch):
    paths = []
    async def fake_get(path):
        paths.append(path)
        if "horizontalpodautoscalers" in path:
            return {"spec": {"minReplicas": 1, "maxReplicas": 3,
                              "metrics": [{"type": "Resource",
                                "resource": {"name": "cpu", "target": {"averageUtilization": 60}}}]},
                    "status": {"currentReplicas": 2, "desiredReplicas": 3,
                               "currentMetrics": [{"type": "Resource",
                                 "resource": {"name": "cpu", "current": {"averageUtilization": 85}}}]}}
        if "metrics.k8s.io" in path:
            return {"items": [{"containers": [
                {"usage": {"cpu": "200m", "memory": "64Mi"}},
                {"usage": {"cpu": "50000000n", "memory": "1024Ki"}}]}]}
        return {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}]}},
                          {"status": {"conditions": [{"type": "Ready", "status": "False"}]}}]}
    monkeypatch.setattr(analytics, "_k8s_get", fake_get)
    result = asyncio.run(analytics._infrastructure())
    assert len(paths) == 3
    assert all("namespaces/ev-fleet" in path for path in paths)
    assert result["errors"] == {}
    assert result["hpa"]["current_cpu_pct"] == 85
    assert result["hpa"]["target_cpu_pct"] == 60
    assert result["hpa"]["desired_replicas"] == 3
    assert result["pods"] == {"total": 2, "ready": 1}
    assert result["usage"] == {"cpu_millicores": 250.0, "memory_mib": 65.0}


def test_partial_metrics_report_unknown_not_zero(monkeypatch):
    async def fake_get(path):
        if "metrics.k8s.io" in path:
            raise RuntimeError("metrics API missing")
        if "horizontalpodautoscalers" in path:
            return {"spec": {"minReplicas": 1, "maxReplicas": 3, "metrics": []},
                    "status": {"currentReplicas": 1, "desiredReplicas": 1}}
        return {"items": []}
    monkeypatch.setattr(analytics, "_k8s_get", fake_get)
    result = asyncio.run(analytics._infrastructure())
    assert result["hpa"]["current_cpu_pct"] is None
    assert result["usage"] is None
    assert "pod_metrics" in result["errors"]


def test_records_export_and_run_rate_stable_after_finish(monkeypatch, tmp_path):
    monkeypatch.setattr(analytics, "DATA_DIR", tmp_path)
    start = datetime.now(timezone.utc) - timedelta(seconds=20)
    end = start + timedelta(seconds=10)
    run = {
        "id": "abcdef123456", "preset": "HIGH", "simulated_evs": 50,
        "telemetry_hz": 1, "requests_per_second": 50, "duration_seconds": 10,
        "started_at": start.isoformat(), "finished_at": end.isoformat(),
        "state": "COMPLETED", "attempted": 100, "success": 98, "errors": 2,
        "latencies_ms": [10, 20, 30], "samples": [{
            "timestamp": end.isoformat(),
            "hpa": {"current_cpu_pct": None, "target_cpu_pct": 60,
                    "current_replicas": 2, "desired_replicas": 2},
            "pods": {"ready": 2, "total": 2},
            "usage": None, "attempted": 100, "success": 98, "errors": 2,
            "p95_latency_ms": 30,
        }],
    }
    analytics._save(run)
    result = analytics._run_snapshot(run)
    assert result["rps_observed"] == 10
    assert result["p95_latency_ms"] == 30
    assert analytics._summary(run)["peak_replicas"] == 2
    output = asyncio.run(analytics.export_csv(run["id"]))
    body = output.body.decode("utf-8")
    assert "run_id,preset,simulated_evs" in body
    assert "abcdef123456,HIGH,50" in body
    assert "60,2,2,2," in body
    with pytest.raises(HTTPException) as error:
        analytics._load("../../etc/passwd")
    assert error.value.status_code == 404


def test_synthetic_requests_are_get_only():
    class Client:
        def __init__(self):
            self.urls = []
        async def get(self, url, headers):
            self.urls.append(url)
            assert headers["Cache-Control"] == "no-cache"
            class Response:
                status_code = 200
            return Response()
    run = {"attempted": 0, "success": 0, "errors": 0, "latencies_ms": []}
    client = Client()
    asyncio.run(analytics._send(client, run, asyncio.Semaphore(1)))
    assert client.urls == [analytics.LOAD_URL]
    assert run["attempted"] == 1
    assert run["success"] == 1
    assert run["errors"] == 0
