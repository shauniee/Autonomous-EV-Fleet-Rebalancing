import asyncio
import random
import httpx
from fastapi import HTTPException


async def get_json(client, url):
    # Reads only: one bounded retry. Writes do not use this helper.
    for attempt in range(2):
        try:
            response = await client.get(url)
            if response.status_code == 404:
                raise HTTPException(404, "TARGET_NOT_FOUND")
            response.raise_for_status()
            return response.json()
        except (httpx.TimeoutException, httpx.NetworkError, httpx.HTTPStatusError) as exc:
            retryable = not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code >= 500
            if attempt == 0 and retryable:
                await asyncio.sleep(random.uniform(.05, .15))
                continue
            code = 504 if isinstance(exc, httpx.TimeoutException) else 503
            raise HTTPException(code, "DEPENDENCY_TIMEOUT" if code == 504 else "DEPENDENCY_UNAVAILABLE") from exc
        except ValueError as exc:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc
