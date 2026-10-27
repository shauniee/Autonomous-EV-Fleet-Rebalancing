import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel, TypeAdapter, ValidationError


class Zone(BaseModel):
    zone_id: str
    name: str


class Vehicle(BaseModel):
    vehicle_id: str
    current_zone_id: str | None
    connectivity: str
    stale: bool
    effective_availability: str
    telemetry: dict | None


class Dispatch(BaseModel):
    dispatch_id: str
    vehicle_id: str | None
    state: str
    claim_held: bool | None
    command: dict | None


@asynccontextmanager
async def lifespan(app):
    app.state.service_urls = {
        name: os.getenv(f"{name.upper()}_URL", f"http://127.0.0.1:{8001 + i}").rstrip("/")
        for i, name in enumerate(("fleet", "demand", "rebalancing", "dispatch"))
    }
    async with httpx.AsyncClient(
        timeout=2,
        follow_redirects=False,
        limits=httpx.Limits(max_connections=10),
    ) as client:
        app.state.client = client
        yield


router = APIRouter(prefix="/api/probe/v1")


@router.get("/observations")
async def observations(request: Request):
    async def read(service, path, key=None, model=None):
        try:
            response = await request.app.state.client.get(
                request.app.state.service_urls[service] + path
            )
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Expected an object")
            if key:
                data = TypeAdapter(list[model]).validate_python(data[key])
                data = [row.model_dump() for row in data]
            elif data.get("status") not in ("ok", "ready"):
                raise ValueError("Invalid health response")
            return {
                "data": data,
                "observed_at": datetime.now(timezone.utc).isoformat(),
                "error": None,
            }
        except httpx.HTTPStatusError as exc:
            error = f"HTTP {exc.response.status_code}"
        except httpx.TimeoutException:
            error = "Request timed out"
        except httpx.RequestError:
            error = "Service unreachable"
        except (ValueError, KeyError, TypeError, ValidationError):
            error = "Invalid service response"
        return {"data": None, "observed_at": None, "error": error}

    names = list(request.app.state.service_urls)
    values = await asyncio.gather(
        *(read(name, "/health/ready") for name in names),
        read("demand", "/api/v1/zones", "zones", Zone),
        read("fleet", "/api/v1/vehicles", "vehicles", Vehicle),
        read("dispatch", "/api/v1/dispatches", "dispatches", Dispatch),
    )
    return {
        "services": dict(zip(names, values[:4])),
        "zones": values[4],
        "fleet": values[5],
        "dispatches": values[6],
    }
