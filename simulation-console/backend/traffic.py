"""Local-only console proxy for simulation scenarios (no browser-held token)."""
import os
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Request, Query
from pydantic import BaseModel, ConfigDict, Field
from typing import Literal

router = APIRouter(prefix="/api/probe/v1/traffic")

class ScenarioRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dispatch_id: UUID
    segment_index: int = Field(ge=0, le=100000)
    kind: Literal["CONGESTION", "ROADWORKS", "ROAD_CLOSED"]
    speed_kph: float | None = Field(default=None, ge=3, le=80)
    duration_minutes: int = Field(ge=1, le=120)
    expected_version: int = Field(ge=0)

async def forward(request, method, path, payload=None, *, write=False):
    token = os.getenv("TRAFFIC_SIMULATION_CONTROL_KEY", "")
    if write and len(token) < 16:
        raise HTTPException(503, "TRAFFIC_SIMULATION_CONTROL_DISABLED")
    url = request.app.state.service_urls["rebalancing"] + "/api/v1/traffic/" + path
    attempted = False
    try:
        attempted = True
        response = await request.app.state.client.request(method, url, json=payload,
            headers={"X-Traffic-Simulation-Key": token} if write else None)
        if response.status_code in (403, 404, 409, 422):
            raise HTTPException(response.status_code, {
                "message": f"Traffic service rejected operation (HTTP {response.status_code}).",
                "outcome_uncertain": False})
        response.raise_for_status()
        return response.json()
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, {"message": f"Traffic upstream error HTTP {exc.response.status_code}",
                                  "outcome_uncertain": write and attempted}) from exc
    except (httpx.RequestError, ValueError, TypeError) as exc:
        raise HTTPException(503, {"message": "Traffic service unreachable or returned invalid data.",
                                  "outcome_uncertain": write and attempted}) from exc

@router.get("/scenarios")
async def list_scenarios(request: Request):
    return await forward(request, "GET", "scenarios")

@router.post("/scenarios", status_code=201)
async def create_scenario(request: Request, body: ScenarioRequest):
    return await forward(request, "POST", "scenarios", body.model_dump(mode="json"), write=True)

@router.delete("/scenarios/{scenario_id}")
async def delete_scenario(request: Request, scenario_id: UUID,
                          expected_version: int = Query(ge=0)):
    return await forward(request, "DELETE",
        f"scenarios/{scenario_id}?expected_version={expected_version}", write=True)

@router.delete("/scenarios")
async def clear_scenarios(request: Request, expected_version: int = Query(ge=0)):
    return await forward(request, "DELETE",
        f"scenarios?expected_version={expected_version}", write=True)
