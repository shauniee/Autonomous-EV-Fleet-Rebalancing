from datetime import datetime, timezone
from typing import Annotated

import httpx
from fastapi import APIRouter, HTTPException, Path, Request
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class DemandInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    desired_available_vehicles: int = Field(ge=0, le=10000)


class DemandValue(BaseModel):
    desired_available_vehicles: int = Field(strict=True, ge=0, le=10000)
    observed_at: str


class DemandZone(BaseModel):
    zone_id: str
    name: str
    instance_id: str
    demand_version: int = Field(strict=True, ge=0)
    demand: DemandValue | None


class DemandSimulationStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    enabled: bool
    running: bool
    tick_count: int = Field(ge=0)
    last_tick_at: str | None
    last_error: str | None
    configuration: dict
    zones: dict


router = APIRouter(prefix="/api/probe/v1")


async def _simulation_request(request: Request, method: str, path: str):
    url = request.app.state.service_urls["demand"] + "/api/v1/demand-simulation" + path
    try:
        response = await request.app.state.client.request(method, url)
        response.raise_for_status()
        return DemandSimulationStatus.model_validate(response.json()).model_dump(mode="json")
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, "DEMAND_SIMULATION_CONTROL_REJECTED") from exc
    except httpx.RequestError as exc:
        raise HTTPException(503, "DEMAND_SIMULATION_CONTROL_UNAVAILABLE") from exc
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(502, "DEMAND_SIMULATION_RESPONSE_INVALID") from exc


@router.get("/demand-simulation/status")
async def simulation_status(request: Request):
    return await _simulation_request(request, "GET", "")


@router.post("/demand-simulation/{action}")
async def control_simulation(action: str, request: Request):
    if action not in ("start", "pause"):
        raise HTTPException(422, "DEMAND_SIMULATION_ACTION_INVALID")
    return await _simulation_request(request, "POST", f"/{action}")


@router.put("/zones/{zone_id}/demand")
async def update_demand(
    zone_id: Annotated[str, Path(pattern=r"^[a-z0-9-]{1,64}$")],
    body: DemandInput,
    request: Request,
):
    url = request.app.state.service_urls["demand"] + f"/api/v1/zones/{zone_id}"
    write_attempted = False
    try:
        response = await request.app.state.client.get(url)
        response.raise_for_status()
        current = DemandZone.model_validate(response.json())
        if current.zone_id != zone_id:
            raise ValueError("Zone identity mismatch")
        payload = {
            "desired_available_vehicles": body.desired_available_vehicles,
            "expected_version": current.demand_version,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        # Never retry a mutation or silently refresh a conflicting version.
        write_attempted = True
        response = await request.app.state.client.put(url + "/demand", json=payload)
        response.raise_for_status()
        updated = DemandZone.model_validate(response.json())
        if (
            updated.zone_id != zone_id
            or updated.instance_id != current.instance_id
            or updated.demand_version != current.demand_version + 1
            or updated.demand is None
            or updated.demand.desired_available_vehicles != body.desired_available_vehicles
        ):
            raise ValueError("Demand update not confirmed")
        return {"zone": updated.model_dump()}
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        rejected = status in (404, 409, 422)
        message = {
            404: "Zone not found. Refresh the available zones.",
            409: "Demand changed during submission. Check its current value before reapplying.",
            422: "The Demand service rejected the observation. Check the input and service clock.",
        }.get(status, f"Demand service returned HTTP {status}.")
        raise HTTPException(
            status if rejected else 502,
            {"message": message, "outcome_uncertain": write_attempted and not rejected},
        ) from exc
    except httpx.RequestError as exc:
        raise HTTPException(
            503,
            {
                "message": "Could not reach the Demand service.",
                "outcome_uncertain": write_attempted,
            },
        ) from exc
    except (ValueError, TypeError, ValidationError) as exc:
        raise HTTPException(
            502,
            {
                "message": "Demand service returned an invalid or unconfirmed observation.",
                "outcome_uncertain": write_attempted,
            },
        ) from exc
