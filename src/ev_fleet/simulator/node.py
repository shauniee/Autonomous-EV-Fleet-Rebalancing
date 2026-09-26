import asyncio
import logging
import random
from uuid import uuid4
import httpx
from ev_fleet.contracts.common import utcnow


async def run_node(fleet_url, vehicle_id, position, soc_pct, interval=5, updates=None,
                   dispatch_url=None, **simulation):
    """Stationary node. Registration/session retries are idempotent; telemetry is fresh."""
    if interval <= 0:
        raise ValueError("interval must be positive")
    if dispatch_url:
        if updates is not None:
            raise ValueError("--updates is only supported in stationary mode")
        from .commands import MovingNode
        await MovingNode(fleet_url, dispatch_url, vehicle_id, position, soc_pct,
                         telemetry_interval=interval, **simulation).run()
        return
    session = None
    session_request = str(uuid4())
    sequence = 0
    completed = 0
    attempt = 0
    async with httpx.AsyncClient(base_url=fleet_url, timeout=2) as client:
        while updates is None or completed < updates:
            try:
                if session is None:
                    response = await client.post("/api/v1/vehicles", json={
                        "vehicle_id": vehicle_id, "battery_capacity_kwh": 60,
                        "consumption_kwh_per_km": .18})
                    response.raise_for_status()
                    response = await client.post(f"/api/v1/vehicles/{vehicle_id}/telemetry-sessions",
                                                 json={"request_id": session_request})
                    response.raise_for_status()
                    session = response.json()["session_id"]
                    sequence = 0
                payload = {"session_id": session, "sequence": sequence,
                           "observed_at": utcnow().isoformat(), "position": position,
                           "soc_pct": soc_pct, "availability": "AVAILABLE", "relocation_state": "IDLE"}
                response = await client.post(f"/api/v1/vehicles/{vehicle_id}/telemetry", json=payload)
                # On ambiguous delivery, the next loop uses a higher sequence and fresh observation.
                sequence += 1
                if response.status_code == 404:
                    session = None
                    session_request = str(uuid4())
                response.raise_for_status()
                attempt = 0
                completed += 1
                logging.info("vehicle=%s telemetry=%s accepted", vehicle_id, payload["sequence"])
                if updates is None or completed < updates:
                    await asyncio.sleep(interval)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                # Never overwrite an ambiguously accepted sequence with a different payload.
                if isinstance(exc, httpx.TransportError):
                    sequence += 1
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    stale_observation = (exc.response.status_code == 422 and
                                         exc.response.json().get("error", {}).get("code") == "OBSERVATION_TIME_INVALID")
                    if exc.response.status_code != 404 and not stale_observation:
                        # A competing identity must not repeatedly steal the current session.
                        raise
                attempt += 1
                logging.warning("vehicle=%s communication failure: %s", vehicle_id, type(exc).__name__)
                await asyncio.sleep(min(10, .5 * 2 ** min(attempt, 5)) + random.uniform(0, .25))
