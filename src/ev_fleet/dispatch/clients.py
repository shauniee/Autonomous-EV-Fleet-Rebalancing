import httpx
from fastapi import HTTPException


async def request_json(client, method, url, body=None, model=None, *, timeout_seconds=None):
    """Single attempt. Coordinator owns retries and the stable mutation identities."""
    try:
        options = {"timeout": timeout_seconds} if timeout_seconds is not None else {}
        response = await client.request(method, url, json=body, **options)
        if response.status_code >= 400:
            try:
                code = response.json()["error"]["code"]
            except (ValueError, KeyError, TypeError):
                code = "DEPENDENCY_UNAVAILABLE"
            status = response.status_code if response.status_code in (404, 409, 422) else 503
            raise HTTPException(status, code)
        payload = response.json()
        if model:
            return model.model_validate(payload)
        return payload
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "DEPENDENCY_TIMEOUT") from exc
    except httpx.TransportError as exc:
        raise HTTPException(503, "DEPENDENCY_UNAVAILABLE") from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc
