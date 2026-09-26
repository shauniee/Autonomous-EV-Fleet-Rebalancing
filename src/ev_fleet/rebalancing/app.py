import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
import httpx
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.logging import base_app
from ev_fleet.common.settings import Settings
from ev_fleet.contracts.recommendations import RecommendationRequest, Recommendation
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.contracts.demand import ZoneState
from .clients import get_json
from .policy import POLICY_VERSION, rank
from .store import RecommendationStore


def create_app(settings=None, client=None, clock=None):
    app = base_app("EV Rebalancing Service")
    config = settings or Settings.from_env()
    clock = clock or Clock()
    store = RecommendationStore(clock)

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), limits=httpx.Limits(max_connections=50)) as owned:
            app.state.client = client or owned
            yield

    app.router.lifespan_context = lifespan

    @app.post("/api/v1/recommendations", status_code=201, response_model=Recommendation)
    async def recommend(body: RecommendationRequest):
        start = clock.monotonic()
        fleet, target = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.demand_url}/api/v1/zones/{body.target_zone_id}"))
        now = clock.now()
        try:
            fleet = FleetSnapshot.model_validate(fleet).model_dump(mode="json")
            target = ZoneState.model_validate(target).model_dump(mode="json")
            if target["zone_id"] != body.target_zone_id:
                raise ValueError("target identity mismatch")
            eligible, excluded = rank(fleet, target, config, now)
            source = {key: fleet[key] for key in ("instance_id", "snapshot_version", "snapshot_at")}
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc
        winner = eligible[0] if eligible else None
        record = {"recommendation_id": str(uuid4()), "created_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=config.recommendation_ttl_seconds)).isoformat(),
                  "policy_version": POLICY_VERSION, "policy_settings": config.model_dump(exclude={"fleet_url", "demand_url"}),
                  "target": target, "fleet_snapshot": source,
                  "selected_vehicle_id": winner["vehicle_id"] if winner else None,
                  "ranked_candidates": eligible, "excluded_candidates": excluded,
                  "decision_latency_ms": (clock.monotonic() - start) * 1000,
                  "explanation": (f"{winner['vehicle_id']} has the highest eligible weighted score; "
                                  f"predicted arrival SOC is {winner['predicted_arrival_soc_pct']:.1f}%."
                                  if winner else "No EV satisfies all eligibility checks."),
                  "assumptions": ["Singapore simulated coordinates", "Estimated distance, not road routing",
                                  "Manual simulated charging data", "Advisory only; no reservation or dispatch"]}
        await store.put(record, config.recommendation_ttl_seconds)
        return record

    @app.get("/api/v1/recommendations/{recommendation_id}", response_model=Recommendation)
    async def recommendation(recommendation_id: str):
        return await store.get(recommendation_id)

    return app


app = create_app()
