import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from uuid import uuid4
import httpx
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.logging import base_app
from ev_fleet.common.settings import Settings
from ev_fleet.contracts.recommendations import RecommendationRequest, Recommendation, BatchRecommendationRequest, BatchRecommendation
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.facilities import FacilitiesSnapshot
from .batch import rank_pairs
from .charging_advice import build_charging_advice
from .traffic import TrafficMonitor, assess_traffic, rank_cost
from .clients import get_json
from .strategies import ENERGY_AWARE
from .store import RecommendationStore


def create_app(settings=None, client=None, clock=None, strategy=ENERGY_AWARE):
    app = base_app("EV Rebalancing Service")
    config = settings or Settings.from_env()
    clock = clock or Clock()
    store = RecommendationStore(clock, "rebalancing.recommendations")
    batch_store = RecommendationStore(clock, "rebalancing.batches")
    app.state.persistence = (store.repository, batch_store.repository)
    app.state.traffic_monitor = None

    @asynccontextmanager
    async def lifespan(app):
        await store.start()
        await batch_store.start()
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), limits=httpx.Limits(max_connections=50)) as owned:
                app.state.client = client or owned
                app.state.traffic_monitor = TrafficMonitor(app.state.client, clock=clock.now)
                yield
        finally:
            await store.close()
            await batch_store.close()

    app.router.lifespan_context = lifespan

    @app.post("/api/v1/recommendations", status_code=201, response_model=Recommendation)
    async def recommend(body: RecommendationRequest):
        start = clock.monotonic()
        fleet, target = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.demand_url}/api/v1/zones/{body.target_zone_id}"))
        now = clock.now()
        traffic = await app.state.traffic_monitor.snapshot()
        try:
            fleet = FleetSnapshot.model_validate(fleet).model_dump(mode="json")
            target = ZoneState.model_validate(target).model_dump(mode="json")
            if target["zone_id"] != body.target_zone_id:
                raise ValueError("target identity mismatch")
            eligible, excluded = strategy.rank(fleet, target, config, now)
            if traffic["source"] == "LTA_DATAMALL":
                by_id = {vehicle["vehicle_id"]: vehicle for vehicle in fleet["vehicles"]}
                for candidate in eligible:
                    vehicle = by_id[candidate["vehicle_id"]]
                    corridor = [vehicle["telemetry"]["position"], target["position"]]
                    estimate = assess_traffic(corridor, candidate["distance_km"],
                        3600*candidate["distance_km"]/30, traffic, now)
                    candidate["traffic_assessment"] = estimate
                    cost = rank_cost(estimate, candidate["energy_required_kwh"])
                    if estimate["blocked"]:
                        candidate["eligible"] = False
                        candidate["exclusion_reasons"] = ["TRAFFIC_ROAD_BLOCK_ESTIMATE"]
                    elif cost is not None:
                        candidate["score"] = 1 - cost
                excluded.extend(candidate for candidate in eligible if not candidate["eligible"])
                eligible = [candidate for candidate in eligible if candidate["eligible"]]
                eligible.sort(key=lambda c: (-c["score"], c["vehicle_id"]))
            source = {key: fleet[key] for key in ("instance_id", "snapshot_version", "snapshot_at")}
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc
        winner = eligible[0] if eligible else None
        record = {"recommendation_id": str(uuid4()), "created_at": now.isoformat(),
                  "expires_at": (now + timedelta(seconds=config.recommendation_ttl_seconds)).isoformat(),
                  "policy_version": strategy.policy_version, "policy_settings": config.model_dump(exclude={"fleet_url", "demand_url", "facilities_url"}),
                  "target": target, "fleet_snapshot": source,
                  "selected_vehicle_id": winner["vehicle_id"] if winner else None,
                  "ranked_candidates": eligible, "excluded_candidates": excluded,
                  "decision_latency_ms": (clock.monotonic() - start) * 1000,
                  "explanation": (strategy.explanation(winner)
                                  if winner else "No EV satisfies all eligibility checks."),
                  "assumptions": ["Singapore simulated coordinates", "Estimated distance, not road routing",
                                  "Manual simulated charging data", "Advisory only; no reservation or dispatch"]}
        await store.put(record, config.recommendation_ttl_seconds)
        return record

    @app.get("/api/v1/traffic/conditions")
    async def traffic_conditions():
        # Each feed caches independently. Missing data is explicitly UNKNOWN.
        return await app.state.traffic_monitor.snapshot()

    @app.get("/api/v1/charging-advice")
    async def charging_advice():
        # Read-only advisory. Do not create recommendations/holds or alter state.
        fleet_raw, sites_raw = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.facilities_url}/api/v1/facilities"))
        now = clock.now()
        try:
            fleet = FleetSnapshot.model_validate(fleet_raw).model_dump(mode="json")
            sites = FacilitiesSnapshot.model_validate(sites_raw)
            return build_charging_advice(fleet, sites.facilities, config, now)
        except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc

    @app.post("/api/v1/batch-recommendations", status_code=201, response_model=BatchRecommendation)
    async def batch_recommend(body: BatchRecommendationRequest):
        fleet_raw, target_raw, sites_raw = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.demand_url}/api/v1/zones/{body.target_zone_id}"),
            get_json(app.state.client, f"{config.facilities_url}/api/v1/facilities?zone_id={body.target_zone_id}"))
        now = clock.now()
        traffic = await app.state.traffic_monitor.snapshot()
        try:
            fleet = FleetSnapshot.model_validate(fleet_raw).model_dump(mode="json")
            target = ZoneState.model_validate(target_raw).model_dump(mode="json")
            snapshot = FacilitiesSnapshot.model_validate(sites_raw)
            if target["zone_id"] != body.target_zone_id:
                raise ValueError("wrong demand zone")
            if any(s.zone_id != body.target_zone_id for s in snapshot.facilities):
                raise ValueError("wrong facility zone")
            proposed = rank_pairs(fleet, target, snapshot.facilities, config, now,
                                  body.quantity, body.purpose, body.vehicle_id, body.facility_id, traffic)
        except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from exc
        expires = now + timedelta(seconds=max(30, config.recommendation_ttl_seconds))
        ttl = (expires - now).total_seconds()
        ref = {key: fleet[key] for key in ("instance_id", "snapshot_version", "snapshot_at")}
        assignments = []
        for edge in proposed:
            rec_id = str(uuid4())
            child = {"recommendation_id": rec_id, "created_at": now.isoformat(),
                     "expires_at": expires.isoformat(), "policy_version": "parking-batch-v1",
                     "policy_settings": config.model_dump(exclude={"fleet_url", "demand_url", "facilities_url"}),
                     "target": target, "fleet_snapshot": ref,
                     "selected_vehicle_id": edge["vehicle_id"],
                     "parking_facility_id": edge["facility_id"],
                     "target_position": edge["target_position"],
                     "requires_charging": edge["requires_charging"],
                     "purpose": body.purpose,
                     "traffic_assessment": edge.get("traffic_assessment"),
                     "ranked_candidates": [edge["assessment"]], "excluded_candidates": [],
                     "decision_latency_ms": 0.0,
                     "explanation": "Facility-aware simulated relocation; revalidated on approval.",
                     "assumptions": ["Only local simulated capacity is held on approval",
                                     "Greedy matching heuristic", "LTA observations do not reserve real bays"]}
            await store.put(child, ttl)
            assignments.append({**{key: edge[key] for key in
                ("vehicle_id", "facility_id", "facility_name", "target_position",
                 "requires_charging", "distance_km", "predicted_arrival_soc_pct", "score")},
                "traffic_assessment": edge.get("traffic_assessment"),
                "recommendation_id": rec_id})
        identifier = str(uuid4())
        result = {"batch_recommendation_id": identifier,
                  "purpose": body.purpose,
                  "target_zone_id": body.target_zone_id, "requested_count": body.quantity,
                  "traffic_status": traffic["status"], "traffic_generated_at": traffic["generated_at"],
                  "recommended_count": len(assignments),
                  "unfulfilled_count": body.quantity - len(assignments),
                  "created_at": now.isoformat(), "expires_at": expires.isoformat(),
                  "assignments": assignments,
                  "explanation": f"Proposed {len(assignments)} of {body.quantity} EV-to-parking assignments (capacity-limited greedy heuristic).",
                  "assumptions": [f"Facility source: {snapshot.data_source}", "Only simulated allocations are reserved",
                                  "Traffic ranking uses approximate direct corridors; road route rechecked at approval",
                                  "No road routing or guaranteed real parking"]}
        await batch_store.put({**result, "recommendation_id": identifier}, ttl)
        return result

    @app.get("/api/v1/batch-recommendations/{batch_id}", response_model=BatchRecommendation)
    async def batch_recommendation(batch_id: str):
        record = await batch_store.get(batch_id)
        record.pop("recommendation_id", None)
        return record

    @app.get("/api/v1/recommendations/{recommendation_id}", response_model=Recommendation)
    async def recommendation(recommendation_id: str):
        return await store.get(recommendation_id)

    return app


app = create_app()
