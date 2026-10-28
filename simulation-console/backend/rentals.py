"""Local Kubernetes rental demo controls, deliberately scoped to one Deployment.

The console is a privileged demo-only operator interface: do not expose it publicly.
"""
from pathlib import Path
import os

import httpx
from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/api/probe/v1/rentals")
NAMESPACE = "ev-fleet"
SCALE_PATH = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/customer-rental-demo/scale"
JOB_PATH = f"/apis/batch/v1/namespaces/{NAMESPACE}/jobs/customer-rental-simulation"
TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")


async def cluster_request(method, path, payload=None):
    if os.getenv("RENTAL_K8S_CONTROL", "false").lower() != "true" or not TOKEN_PATH.is_file():
        raise HTTPException(503, "RENTAL_CONTROL_DISABLED")
    try:
        headers = {"Authorization": f"Bearer {TOKEN_PATH.read_text().strip()}"}
        async with httpx.AsyncClient(
            base_url="https://kubernetes.default.svc", verify=str(CA_PATH),
            timeout=4.0, follow_redirects=False,
        ) as client:
            response = await client.request(method, path, json=payload, headers=headers)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, OSError, ValueError) as exc:
        raise HTTPException(503, f"KUBERNETES_CONTROL_UNAVAILABLE:{type(exc).__name__}") from exc


def replica_count(scale, section):
    # Kubernetes Scale API can omit zero-value replica fields from JSON.
    # That means a paused Deployment may have spec={}, status={} instead
    # of explicitly containing "replicas": 0.
    data = scale.get(section, {} if section == "status" else None) if isinstance(scale, dict) else None
    if not isinstance(data, dict):
        raise HTTPException(503, "RENTAL_SCALE_RESPONSE_INVALID")
    count = data.get("replicas", 0)
    if type(count) is not int or count < 0:
        raise HTTPException(503, "RENTAL_SCALE_RESPONSE_INVALID")
    return count


@router.get("/status")
async def status():
    scale = await cluster_request("GET", SCALE_PATH)
    if scale is None:
        raise HTTPException(503, "RENTAL_DEPLOYMENT_NOT_INSTALLED")
    desired = replica_count(scale, "spec")
    actual = replica_count(scale, "status")
    return {"running": desired > 0, "desired_replicas": desired,
            "actual_replicas": actual}


@router.post("/start")
async def start():
    job = await cluster_request("GET", JOB_PATH)
    if job and job.get("status", {}).get("active", 0) > 0:
        raise HTTPException(409, "CONFLICTING_RENTAL_JOB_ACTIVE")
    scale = await cluster_request("GET", SCALE_PATH)
    if scale is None:
        raise HTTPException(503, "RENTAL_DEPLOYMENT_NOT_INSTALLED")
    if replica_count(scale, "spec") == 1:
        return {"running": True, "desired_replicas": 1}
    scale["spec"]["replicas"] = 1
    updated = await cluster_request("PUT", SCALE_PATH, scale)
    return {"running": True, "desired_replicas": replica_count(updated, "spec")}


@router.post("/stop")
async def stop(request: Request):
    # Fail closed: stopping the only owning simulator during a rental strands a
    # rented EV with no producer capable of recording its physical return.
    try:
        response = await request.app.state.client.get(
            request.app.state.service_urls["fleet"] + "/api/v1/vehicles"
        )
        response.raise_for_status()
        data = response.json()
        vehicles = data["vehicles"]
        if not isinstance(vehicles, list):
            raise ValueError("Invalid fleet response")
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise HTTPException(503, "FLEET_STATE_UNAVAILABLE") from exc
    customer_vehicles = [v for v in vehicles if isinstance(v, dict)
                         and v.get("vehicle_id", "").startswith("customer-ev-")]
    if any(v.get("active_rental") for v in customer_vehicles):
        raise HTTPException(409, "ACTIVE_CUSTOMER_RENTALS_MUST_FINISH")
    if any(v.get("active_claim") or v.get("effective_relocation_state") not in (None, "IDLE")
           for v in customer_vehicles):
        raise HTTPException(409, "ACTIVE_CUSTOMER_RELOCATIONS_MUST_FINISH")
    scale = await cluster_request("GET", SCALE_PATH)
    if scale is None:
        raise HTTPException(503, "RENTAL_DEPLOYMENT_NOT_INSTALLED")
    if replica_count(scale, "spec") == 0:
        return {"running": False, "desired_replicas": 0}
    scale["spec"]["replicas"] = 0
    updated = await cluster_request("PUT", SCALE_PATH, scale)
    return {"running": False, "desired_replicas": replica_count(updated, "spec")}
