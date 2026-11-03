"""Bounded, local-only load experiments and read-only Kubernetes observability.

This module never writes Fleet, Demand, Dispatch or Kubernetes scaling objects.
Experiments generate HTTP GET requests to a fixed operator-dashboard URL only.
"""
import asyncio
import csv
from datetime import datetime, timezone
from io import StringIO
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

router = APIRouter(prefix="/api/probe/v1/analytics", tags=["analytics"])
NAMESPACE = "ev-fleet"
TOKEN = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
DATA_DIR = Path(os.getenv("ANALYTICS_DATA_DIR", "/tmp/ev-fleet-analytics"))
LOAD_URL = os.getenv("ANALYTICS_DASHBOARD_URL", "http://dashboard/")
PRESETS = {
    "NORMAL": {"simulated_evs": 10, "telemetry_hz": 0.2, "requests_per_second": 2},
    "HIGH": {"simulated_evs": 50, "telemetry_hz": 1.0, "requests_per_second": 50},
    "EXTREME": {"simulated_evs": 100, "telemetry_hz": 2.0, "requests_per_second": 200},
}
# A single Uvicorn worker and a single console replica are required by this local demo.
_active_run: dict | None = None
_active_task: asyncio.Task | None = None
_state_lock = asyncio.Lock()


def utcnow():
    return datetime.now(timezone.utc).isoformat()


class StartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    preset: Literal["NORMAL", "HIGH", "EXTREME"]
    duration_seconds: int = Field(default=60, ge=10, le=120)


def _file(run_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", run_id):
        raise HTTPException(404, "EXPERIMENT_NOT_FOUND")
    return DATA_DIR / f"{run_id}.json"


def _save(run: dict):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = _file(run["id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(run, separators=(",", ":")), encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        raise HTTPException(503, "ANALYTICS_STORAGE_UNAVAILABLE") from exc


def _load(run_id: str) -> dict:
    try:
        data = json.loads(_file(run_id).read_text(encoding="utf-8"))
        if data.get("id") != run_id:
            raise ValueError("Invalid experiment identity")
        return data
    except (OSError, ValueError, KeyError) as exc:
        raise HTTPException(404, "EXPERIMENT_NOT_FOUND") from exc


def _summary(run: dict) -> dict:
    result = {k: run.get(k) for k in
              ("id", "preset", "simulated_evs", "telemetry_hz", "requests_per_second",
               "duration_seconds", "started_at", "finished_at", "state",
               "attempted", "success", "errors")}
    samples = run.get("samples", [])
    cpus = [s["hpa"]["current_cpu_pct"] for s in samples
            if isinstance(s.get("hpa"), dict) and s["hpa"].get("current_cpu_pct") is not None]
    replicas = [s["hpa"]["current_replicas"] for s in samples
                if isinstance(s.get("hpa"), dict) and s["hpa"].get("current_replicas") is not None]
    result["peak_hpa_cpu_pct"] = max(cpus, default=None)
    result["peak_replicas"] = max(replicas, default=None)
    end = (datetime.fromisoformat(run["finished_at"]) if run.get("finished_at")
           else datetime.now(timezone.utc))
    seconds = max(0.001, (end - datetime.fromisoformat(run["started_at"])).total_seconds())
    result["mean_rps"] = round(run.get("attempted", 0) / seconds, 2)
    return result


def _percentile(values: list[float], proportion: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * proportion + 0.999999)))
    return round(ordered[index], 1)


def _millicores(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(n|u|m)?", value)
    if not m:
        raise ValueError("Invalid CPU quantity")
    number = float(m.group(1))
    return round(number * {"n": 0.000001, "u": 0.001, "m": 1, None: 1000}[m.group(2)], 3)


def _memory_mib(value: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(Ki|Mi|Gi|Ti)?", value)
    if not m:
        raise ValueError("Invalid memory quantity")
    number = float(m.group(1))
    return round(number * {None: 1 / 1048576, "Ki": 1 / 1024,
                           "Mi": 1, "Gi": 1024, "Ti": 1048576}[m.group(2)], 2)


async def _k8s_get(path: str):
    if os.getenv("ANALYTICS_K8S_READ", "false").lower() != "true" or not TOKEN.is_file():
        raise RuntimeError("Kubernetes metrics access is not enabled")
    try:
        async with httpx.AsyncClient(base_url="https://kubernetes.default.svc",
                                     verify=str(CA), timeout=3.0, follow_redirects=False) as client:
            response = await client.get(path, headers={
                "Authorization": f"Bearer {TOKEN.read_text(encoding='utf-8').strip()}"})
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise RuntimeError(f"Kubernetes API unavailable: {type(exc).__name__}") from exc


async def _infrastructure():
    paths = [
        f"/apis/autoscaling/v2/namespaces/{NAMESPACE}/horizontalpodautoscalers/dashboard",
        f"/api/v1/namespaces/{NAMESPACE}/pods?labelSelector=app.kubernetes.io%2Fname%3Ddashboard",
        f"/apis/metrics.k8s.io/v1beta1/namespaces/{NAMESPACE}/pods?labelSelector=app.kubernetes.io%2Fname%3Ddashboard",
    ]
    results = await asyncio.gather(*(_k8s_get(path) for path in paths), return_exceptions=True)
    errors = {}
    for i, name in enumerate(("hpa", "pods", "pod_metrics")):
        if isinstance(results[i], BaseException):
            errors[name] = str(results[i])
    hpa = None
    if "hpa" not in errors:
        raw = results[0]
        metrics = raw.get("status", {}).get("currentMetrics", [])
        cpu = next((x["resource"]["current"].get("averageUtilization")
                    for x in metrics if x.get("type") == "Resource"
                    and x.get("resource", {}).get("name") == "cpu"), None)
        spec_metric = next((x["resource"]["target"].get("averageUtilization")
                            for x in raw.get("spec", {}).get("metrics", [])
                            if x.get("type") == "Resource"
                            and x.get("resource", {}).get("name") == "cpu"), None)
        status = raw.get("status", {})
        hpa = {"current_cpu_pct": cpu, "target_cpu_pct": spec_metric,
               "min_replicas": raw.get("spec", {}).get("minReplicas"),
               "max_replicas": raw.get("spec", {}).get("maxReplicas"),
               "current_replicas": status.get("currentReplicas"),
               "desired_replicas": status.get("desiredReplicas"),
               "conditions": [{"type": x.get("type"), "status": x.get("status"),
                               "reason": x.get("reason")} for x in status.get("conditions", [])]}
    pods = None
    if "pods" not in errors:
        items = results[1].get("items", [])
        pods = {"total": len(items),
                "ready": sum(any(c.get("type") == "Ready" and c.get("status") == "True"
                                 for c in pod.get("status", {}).get("conditions", []))
                             for pod in items)}
    usage = None
    if "pod_metrics" not in errors:
        try:
            containers = [c for item in results[2].get("items", [])
                          for c in item.get("containers", [])]
            if not containers:
                raise ValueError("No measured dashboard containers")
            usage = {"cpu_millicores": round(sum(_millicores(c["usage"]["cpu"])
                                                 for c in containers), 2),
                     "memory_mib": round(sum(_memory_mib(c["usage"]["memory"])
                                            for c in containers), 2)}
        except (KeyError, TypeError, ValueError) as exc:
            errors["pod_metrics"] = f"Invalid metrics response: {type(exc).__name__}"
    return {"hpa": hpa, "pods": pods, "usage": usage, "errors": errors,
            "observed_at": utcnow()}


async def _send(client: httpx.AsyncClient, run: dict, semaphore: asyncio.Semaphore):
    async with semaphore:
        start = time.perf_counter()
        ok = False
        try:
            response = await client.get(LOAD_URL, headers={"Cache-Control": "no-cache"})
            ok = 200 <= response.status_code < 400
        except httpx.RequestError:
            pass
        elapsed = (time.perf_counter() - start) * 1000
        run["attempted"] += 1
        run["success" if ok else "errors"] += 1
        samples = run["latencies_ms"]
        samples.append(round(elapsed, 2))
        if len(samples) > 2000:
            del samples[:len(samples) - 2000]


async def _drive(run: dict):
    """Cap duration, rate and concurrency; never invoke mutating EV APIs."""
    pending: set[asyncio.Task] = set()
    try:
        async with httpx.AsyncClient(timeout=2.0, follow_redirects=False,
                                     limits=httpx.Limits(max_connections=16)) as client:
            semaphore = asyncio.Semaphore(12)
            start = time.monotonic()
            end = start + run["duration_seconds"]
            rate = run["requests_per_second"]
            sent = 0
            while time.monotonic() < end:
                # Fixed-rate scheduling avoids spawning all requests in a burst.
                due = start + sent / rate
                if due > time.monotonic():
                    await asyncio.sleep(min(due - time.monotonic(), 0.1))
                    continue
                task = asyncio.create_task(_send(client, run, semaphore))
                pending.add(task)
                task.add_done_callback(pending.discard)
                sent += 1
                # Do not let a stalled destination create unbounded queued tasks.
                if len(pending) >= 32:
                    await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    except asyncio.CancelledError:
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        run["state"] = "STOPPED"
        raise
    except Exception:
        run["state"] = "FAILED"
    finally:
        if run["state"] == "RUNNING":
            run["state"] = "COMPLETED"
        run["finished_at"] = utcnow()
        _save(run)


def _run_snapshot(run: dict | None):
    if not run:
        return None
    end = (datetime.fromisoformat(run["finished_at"]) if run["finished_at"]
           else datetime.now(timezone.utc))
    elapsed = max(0.001, (end -
                          datetime.fromisoformat(run["started_at"])).total_seconds())
    return {**_summary(run), "rps_observed": round(run["attempted"] / elapsed, 2),
            "p95_latency_ms": _percentile(run["latencies_ms"], 0.95),
            "samples": run["samples"][-100:]}


@router.post("/start", status_code=202)
async def start_experiment(body: StartRequest):
    global _active_run, _active_task
    if os.getenv("ANALYTICS_LOAD_ENABLED", "false").lower() != "true":
        raise HTTPException(503, "LOCAL_LOAD_TEST_DISABLED")
    async with _state_lock:
        if _active_task is not None and not _active_task.done():
            raise HTTPException(409, "EXPERIMENT_ALREADY_RUNNING")
        preset = PRESETS[body.preset]
        run = {**preset, "id": uuid4().hex[:12], "preset": body.preset,
               "duration_seconds": body.duration_seconds, "started_at": utcnow(),
               "finished_at": None, "state": "RUNNING", "attempted": 0,
               "success": 0, "errors": 0, "latencies_ms": [], "samples": []}
        _save(run)
        _active_run = run
        _active_task = asyncio.create_task(_drive(run))
        return _summary(run)


@router.post("/stop")
async def stop_experiment():
    async with _state_lock:
        if _active_task is None or _active_task.done():
            raise HTTPException(409, "NO_ACTIVE_EXPERIMENT")
        _active_task.cancel()
        task = _active_task
    try:
        await task
    except asyncio.CancelledError:
        pass
    return _summary(_active_run)


@router.get("/status")
async def status():
    infra = await _infrastructure()
    run = _active_run
    if run is not None:
        # A real observation only; never replace missing Kubernetes metrics with zeros.
        sample = {"timestamp": infra["observed_at"], "hpa": infra["hpa"],
                  "pods": infra["pods"], "usage": infra["usage"],
                  "attempted": run["attempted"], "success": run["success"],
                  "errors": run["errors"],
                  "p95_latency_ms": _percentile(run["latencies_ms"], 0.95)}
        # A completed experiment gets at most one final sample, not infinite idle points.
        last = run["samples"][-1] if run["samples"] else None
        needs_final = (run["finished_at"] is not None and
                       (last is None or last["timestamp"] < run["finished_at"]))
        if (run["state"] == "RUNNING" or needs_final) and (
                last is None or last["timestamp"] != sample["timestamp"]):
            run["samples"].append(sample)
            run["samples"] = run["samples"][-100:]
            _save(run)
    return {"infrastructure": infra, "experiment": _run_snapshot(run),
            "load_enabled": os.getenv("ANALYTICS_LOAD_ENABLED", "false").lower() == "true",
            "storage": "local-persistent-volume" if os.getenv("ANALYTICS_DATA_DIR") else "ephemeral"}


@router.get("/runs")
async def runs():
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        records = []
        for path in sorted(DATA_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime,
                           reverse=True)[:30]:
            try:
                run = _load(path.stem)
                if run["state"] == "RUNNING" and run["id"] != (_active_run or {}).get("id"):
                    run["state"] = "INTERRUPTED"
                records.append(_summary(run))
            except (HTTPException, KeyError, TypeError):
                continue
        return {"runs": records}
    except OSError as exc:
        raise HTTPException(503, "ANALYTICS_STORAGE_UNAVAILABLE") from exc


@router.get("/runs/{run_id}/csv")
async def export_csv(run_id: str):
    run = _load(run_id)
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["run_id", "preset", "simulated_evs", "telemetry_hz",
                     "target_rps", "timestamp_utc", "hpa_cpu_pct", "hpa_target_pct",
                     "hpa_current_replicas", "hpa_desired_replicas",
                     "ready_pods", "pod_cpu_m", "pod_memory_mib",
                     "requests_attempted", "requests_success", "requests_failed",
                     "p95_latency_ms"])
    for sample in run.get("samples", []):
        hpa, pods, usage = (sample.get(x) or {} for x in ("hpa", "pods", "usage"))
        writer.writerow([run["id"], run["preset"], run["simulated_evs"],
                         run["telemetry_hz"], run["requests_per_second"],
                         sample["timestamp"], hpa.get("current_cpu_pct"),
                         hpa.get("target_cpu_pct"), hpa.get("current_replicas"),
                         hpa.get("desired_replicas"), pods.get("ready"),
                         usage.get("cpu_millicores"), usage.get("memory_mib"),
                         sample.get("attempted"), sample.get("success"),
                         sample.get("errors"), sample.get("p95_latency_ms")])
    return Response(content=output.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="ev-analytics-{run_id}.csv"'})
