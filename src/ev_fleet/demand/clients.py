import asyncio
import httpx
from pydantic import ValidationError
from ev_fleet.contracts.vehicles import FleetSnapshot


async def fleet_snapshot(client, settings):
    """One bounded read, no local fleet cache or fallback counts."""
    try:
        response = await asyncio.wait_for(client.get(settings.fleet_url + "/api/v1/vehicles"),
                                          timeout=settings.fleet_timeout_seconds)
        response.raise_for_status()
        return FleetSnapshot.model_validate(response.json()), None
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return None, "FLEET_TIMEOUT"
    except (httpx.TransportError, httpx.HTTPStatusError):
        return None, "FLEET_UNAVAILABLE"
    except (ValueError, ValidationError):
        return None, "FLEET_INVALID_RESPONSE"
