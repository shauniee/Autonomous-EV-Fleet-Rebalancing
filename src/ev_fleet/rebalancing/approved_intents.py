"""Read immutable queued authorization from its authoritative Dispatch owner."""
from datetime import datetime

from fastapi import HTTPException

from ev_fleet.contracts.recommendations import Recommendation
from .clients import get_json


async def recommendation_for_revalidation(client, store, body, settings, clock):
    if body.approved_batch_request_id is None:
        return Recommendation.model_validate(await store.get(str(body.recommendation_id)))
    batch_id = str(body.approved_batch_request_id)
    raw = await get_json(client, f"{settings.dispatch_url}/api/v1/batches/{batch_id}"
                         f"/items/{body.recommendation_id}/intent")
    try:
        if (raw["batch_request_id"] != batch_id
                or raw["dispatch_id"] != str(body.dispatch_id)
                or raw["recommendation_id"] != str(body.recommendation_id)):
            raise HTTPException(503, "APPROVED_INTENT_IDENTITY_MISMATCH")
        expires = datetime.fromisoformat(raw["expires_at"])
        recommendation = Recommendation.model_validate(raw["recommendation"])
        if (str(recommendation.recommendation_id) != str(body.recommendation_id)
                or recommendation.expires_at != expires
                or recommendation.selected_vehicle_id is None
                or recommendation.parking_facility_id is None):
            raise HTTPException(503, "APPROVED_INTENT_IDENTITY_MISMATCH")
        if expires <= clock.now():
            raise HTTPException(409, "APPROVED_INTENT_EXPIRED")
        return recommendation
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(503, "APPROVED_INTENT_INVALID") from exc
