import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
from uuid import uuid4, UUID
import os
from fastapi import Header, Query
from pydantic import BaseModel, Field
from ev_fleet.contracts.common import Position
import httpx
from fastapi import HTTPException
from ev_fleet.common.clock import Clock
from ev_fleet.common.logging import base_app
from ev_fleet.common.settings import Settings, CrdsSettings
from .crds_service import plan_one, plan_batch, recommendation_record
from .crds_futures import MODEL_VERSION, parameters_for
from ev_fleet.contracts.crds import CrdsRevalidationRequest, CrdsRevalidationResult
from ev_fleet.contracts.recommendations import RecommendationRequest, Recommendation, BatchRecommendationRequest, BatchRecommendation
from ev_fleet.contracts.vehicles import FleetSnapshot
from ev_fleet.contracts.demand import ZoneState
from ev_fleet.contracts.facilities import FacilitiesSnapshot
from .batch import rank_pairs
from .charging_advice import build_charging_advice
from .traffic import TrafficMonitor, assess_traffic, dashboard_traffic_snapshot, rank_cost
from .shared_traffic import RedisTrafficReader, redis_client
from .scenarios import TrafficScenarios, TrafficScenarioInput, allowed_control
from ev_fleet.contracts.dispatch import Dispatch
from .clients import get_json
from .strategies import ENERGY_AWARE
from .store import RecommendationStore
from .approved_intents import recommendation_for_revalidation
from .request_queue import Checkpoint, RecommendationQueue
from ev_fleet.common.persistence import SnapshotRepository
from ev_fleet.contracts.recommendations import (
    QueuedRecommendationRequest, RecommendationRequestPage, RecommendationRequestRecord,
    RecommendationRequestStatus,
)


class TrafficRouteQuery(BaseModel):
    """Internal read-only route assessment; no provider data or keys returned."""
    geometry: list[Position] = Field(min_length=2, max_length=10000)
    distance_km: float = Field(ge=0, le=300)
    duration_seconds: float = Field(ge=0, le=86400)


def create_app(settings=None, client=None, clock=None, strategy=ENERGY_AWARE, crds_settings=None):
    app = base_app("EV Rebalancing Service")
    config = settings or Settings.from_env()
    crds_config = crds_settings or CrdsSettings.from_env()
    clock = clock or Clock()
    store = RecommendationStore(clock, "rebalancing.recommendations")
    batch_store = RecommendationStore(clock, "rebalancing.batches")
    queue_repository = SnapshotRepository.from_env("rebalancing.recommendation_requests")
    queue_enabled = queue_repository.enabled and crds_config.mode != "DISABLED"
    queue = RecommendationQueue(clock, queue_repository,
                                capacity=crds_config.request_queue_capacity,
                                max_wait_seconds=crds_config.request_queue_max_wait_seconds)
    app.state.persistence = (store.repository, batch_store.repository)
    if queue_enabled:
        app.state.persistence += (queue_repository,)
    app.state.traffic_monitor = None
    app.state.traffic_scenarios = TrafficScenarios(clock)
    app.state.traffic_instance_id = str(uuid4())

    def traffic_snapshot():
        return {**app.state.traffic_scenarios.merged_snapshot(app.state.traffic_monitor.cached_snapshot()),
                "instance_id": app.state.traffic_instance_id}

    @asynccontextmanager
    async def lifespan(app):
        async with AsyncExitStack() as cleanup:
            for state in (store, batch_store):
                state.repository.enable_recovery_exit()
                cleanup.callback(state.repository.disable_recovery_exit)
                cleanup.push_async_callback(state.close)
                await state.start()
            if queue_enabled:
                queue_repository.enable_recovery_exit()
                cleanup.callback(queue_repository.disable_recovery_exit)
                cleanup.push_async_callback(queue_repository.close)
                await queue_repository.start()
                await queue.start()
            app.state.crds_route_slots = asyncio.Semaphore(4)
            app.state.crds_traffic_slots = asyncio.Semaphore(4)
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0), limits=httpx.Limits(max_connections=50)) as owned:
                app.state.client = client or owned
                # In Redis mode Rebalancing NEVER calls the LTA API. A separate,
                # single-replica collector owns all external traffic polling.
                # Legacy INLINE mode stays available for isolated unit tests.
                cache_mode = os.getenv("TRAFFIC_CACHE_BACKEND", "INLINE").upper()
                if cache_mode not in ("INLINE", "REDIS"):
                    raise RuntimeError("TRAFFIC_CACHE_BACKEND must be INLINE or REDIS")
                if cache_mode == "REDIS":
                    try:
                        shared = redis_client()
                    except ValueError:
                        shared = None  # Missing cache fails closed, not back to LTA.
                    monitor = RedisTrafficReader(
                        shared, source=os.getenv("TRAFFIC_SOURCE"), clock=clock.now)
                else:
                    monitor = TrafficMonitor(app.state.client, clock=clock.now)
                app.state.traffic_monitor = monitor
                traffic_task = (asyncio.create_task(monitor.refresh_loop())
                                if monitor.source == "LTA_DATAMALL" else None)
                queue_task = asyncio.create_task(run_queue()) if queue_enabled else None
                app.state.recommendation_queue_task = queue_task
                try:
                    yield
                finally:
                    queue.stop()
                    if queue_task is not None:
                        await asyncio.gather(queue_task, return_exceptions=True)
                    if traffic_task:
                        traffic_task.cancel()
                        await asyncio.gather(traffic_task, return_exceptions=True)
                    if isinstance(monitor, RedisTrafficReader):
                        await monitor.aclose()

    app.router.lifespan_context = lifespan

    def route_budget():
        return {"route_slots": app.state.crds_route_slots,
                "traffic_slots": app.state.crds_traffic_slots}

    @app.post("/api/v1/recommendations", status_code=201, response_model=Recommendation)
    async def recommend(body: RecommendationRequest):
        start = clock.monotonic()
        if crds_config.mode != "DISABLED":
            try:
                plan = await plan_one(app.state.client, config, crds_config, clock,
                                      traffic_snapshot(), body.target_zone_id, **route_budget())
            except asyncio.TimeoutError:
                raise HTTPException(504, "CRDS_INPUT_TIMEOUT") from None
            except ValueError:
                raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from None
            record = recommendation_record(plan, config, clock, body.target_zone_id, started_at=start)
            await store.put(record, config.recommendation_ttl_seconds)
            return record
        fleet, target = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.demand_url}/api/v1/zones/{body.target_zone_id}"))
        now = clock.now()
        traffic = traffic_snapshot()
        try:
            fleet = FleetSnapshot.model_validate(fleet).model_dump(mode="json")
            target = ZoneState.model_validate(target).model_dump(mode="json")
            if target["zone_id"] != body.target_zone_id:
                raise ValueError("target identity mismatch")
            eligible, excluded = strategy.rank(fleet, target, config, now)
            if traffic["source"] in ("LTA_DATAMALL", "SIMULATION"):
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
                    elif cost is not None and estimate["status"] == "FRESH":
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

    @app.get("/api/v1/traffic/scenarios")
    async def list_traffic_scenarios():
        return app.state.traffic_scenarios.list()

    @app.post("/api/v1/crds/revalidate", response_model=CrdsRevalidationResult)
    async def revalidate_crds(body: CrdsRevalidationRequest):
        record = await recommendation_for_revalidation(
            app.state.client, store, body, crds_config, clock)
        if (crds_config.mode != "ACTIVE" or record.crds is None
                or not record.crds.approval_allowed or record.crds.decision != "MOVE"):
            raise HTTPException(409, "CRDS_APPROVAL_DISABLED")
        if record.expires_at <= clock.now():
            raise HTTPException(409, "RECOMMENDATION_EXPIRED")
        if (record.crds.parameters != parameters_for(crds_config)
                or record.crds.scenario_model_version != MODEL_VERSION):
            raise HTTPException(409, "CRDS_POLICY_CHANGED")
        policy = Settings(**{**config.model_dump(), **record.policy_settings})
        try:
            plan = await plan_one(app.state.client, policy, crds_config, clock,
                traffic_snapshot(), record.target.zone_id, vehicle_id=record.selected_vehicle_id,
                facility_id=record.parking_facility_id, route_override=body.route,
                exclude_dispatch_id=str(body.dispatch_id),
                planning_mode=record.crds.planning_mode, **route_budget())
        except asyncio.TimeoutError:
            raise HTTPException(504, "CRDS_INPUT_TIMEOUT") from None
        except ValueError:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from None
        if (plan.inputs.planning.instance_id != body.expected_planning_instance_id
                or plan.inputs.planning.revision != body.expected_planning_revision):
            raise HTTPException(409, "CRDS_PLANNING_CHANGED")
        allowed = (plan.metadata.approval_allowed and
                   plan.evaluation.selected_vehicle_id == record.selected_vehicle_id and
                   plan.evaluation.selected_facility_id == record.parking_facility_id)
        return CrdsRevalidationResult(allowed=allowed, metadata=plan.metadata)

    @app.post("/api/v1/traffic/scenarios", status_code=201)
    async def create_traffic_scenario(
        body: TrafficScenarioInput,
        x_traffic_simulation_key: str | None = Header(default=None),
    ):
        allowed_control(x_traffic_simulation_key)
        # Only real road geometry emitted in an approved Dispatch command can
        # become a simulation segment. No arbitrary lines on the road map.
        url = os.getenv("DISPATCH_URL", "http://dispatch:8000").rstrip("/")
        try:
            raw = await get_json(app.state.client, f"{url}/api/v1/dispatches/{body.dispatch_id}")
            dispatch = Dispatch.model_validate(raw)
        except (ValueError, KeyError, TypeError) as exc:
            raise HTTPException(503, "DISPATCH_ROUTE_INVALID") from exc
        if (dispatch.command is None or dispatch.command.route is None
                or dispatch.state.value in ("REJECTED", "FAILED")
                or (dispatch.claim_held is not True and dispatch.state.value != "SUCCEEDED")):
            raise HTTPException(409, "APPROVED_ROAD_GEOMETRY_REQUIRED")
        return app.state.traffic_scenarios.create(body, dispatch.command.route)

    @app.delete("/api/v1/traffic/scenarios/{scenario_id}")
    async def delete_traffic_scenario(
        scenario_id: UUID, expected_version: int = Query(ge=0),
        x_traffic_simulation_key: str | None = Header(default=None),
    ):
        allowed_control(x_traffic_simulation_key)
        return app.state.traffic_scenarios.delete(str(scenario_id), expected_version)

    @app.delete("/api/v1/traffic/scenarios")
    async def clear_traffic_scenarios(
        expected_version: int = Query(ge=0),
        x_traffic_simulation_key: str | None = Header(default=None),
    ):
        allowed_control(x_traffic_simulation_key)
        return app.state.traffic_scenarios.clear(expected_version)

    @app.get("/api/v1/traffic/simulation-events")
    async def simulation_events():
        # The 20 moving EV simulators poll frequently and never need the
        # large cached LTA speed-band dataset used by the operator dashboard.
        return app.state.traffic_scenarios.simulation_snapshot()

    @app.post("/api/v1/traffic/assess-route")
    async def traffic_assess_route(body: TrafficRouteQuery):
        # Dispatch requests a result for the EXACT approved road geometry,
        # rather than fetching a sampled 800-link map feed and guessing locally.
        traffic = app.state.traffic_scenarios.merged_snapshot(
            app.state.traffic_monitor.cached_snapshot())
        return await asyncio.to_thread(
            assess_traffic, [p.model_dump() for p in body.geometry],
            body.distance_km, body.duration_seconds, traffic, clock.now())

    @app.get("/api/v1/traffic/cache-status")
    async def traffic_cache_status():
        snapshot = app.state.traffic_monitor.cached_snapshot()
        return {"source": snapshot["source"], "status": snapshot["status"],
                "generated_at": snapshot["generated_at"],
                "feeds": snapshot.get("feeds", {})}

    @app.get("/api/v1/traffic/conditions")
    async def traffic_conditions():
        # Each feed caches independently. Missing data is explicitly UNKNOWN.
        merged = app.state.traffic_scenarios.merged_snapshot(
            app.state.traffic_monitor.cached_snapshot())
        return dashboard_traffic_snapshot(merged)

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

    async def evaluate_request(body: QueuedRecommendationRequest | BatchRecommendationRequest) -> Checkpoint:
        start = clock.monotonic()
        try:
            plan = await plan_one(app.state.client, config, crds_config, clock, traffic_snapshot(),
                body.target_zone_id, vehicle_id=body.vehicle_id,
                vehicle_ids=body.vehicle_ids if isinstance(body, QueuedRecommendationRequest) else None,
                for_batch=True, planning_mode=body.planning_mode, **route_budget())
        except asyncio.TimeoutError:
            raise HTTPException(504, "CRDS_INPUT_TIMEOUT") from None
        except ValueError:
            raise HTTPException(503, "DEPENDENCY_INVALID_RESPONSE") from None
        children, metadata, stop_reason = await plan_batch(
            plan, body.quantity, app.state.client, crds_config, clock, policy=config)
        now = clock.now()
        expires = now + timedelta(seconds=config.recommendation_ttl_seconds)
        assignments = []
        records = []
        for child in children:
            record = recommendation_record(child, config, clock, body.target_zone_id, started_at=start)
            record["expires_at"] = expires.isoformat()
            records.append(record)
            gain = child.evaluation.ranked_candidates[0]
            site = child.sites[(gain.vehicle_id, gain.facility_id)]
            assessment = child.assessments[gain.vehicle_id]
            assignments.append({"recommendation_id": record["recommendation_id"],
                "vehicle_id": gain.vehicle_id, "facility_id": gain.facility_id, "facility_name": site.name,
                "target_position": site.position.model_dump(mode="json"),
                "requires_charging": record["requires_charging"], "distance_km": assessment["distance_km"],
                "predicted_arrival_soc_pct": assessment["predicted_arrival_soc_pct"], "score": None,
                "traffic_assessment": assessment["traffic_assessment"], "crds_gain": gain.model_dump(mode="json")})
        result = {"batch_recommendation_id": str(uuid4()), "target_zone_id": body.target_zone_id,
            "purpose": "REBALANCE", "requested_count": body.quantity, "recommended_count": len(assignments),
            "unfulfilled_count": body.quantity - len(assignments), "assignments": assignments,
            "created_at": now.isoformat(), "expires_at": expires.isoformat(),
            "crds": metadata.model_dump(mode="json"),
            "unfulfilled_reason": stop_reason if len(assignments) < body.quantity else None,
            "explanation": (f"{len(assignments)} of {body.quantity} operator-requested relocations are feasible. "
                "Coverage benefit is advisory; physical eligibility and capacity remain required."
                if body.planning_mode == "FULFIL_REQUEST" else
                f"CRDS recommends {len(assignments)} of {body.quantity} requested EVs."),
            "assumptions": ["Marginal gains recalculated after each move",
                ("Capacity-matched eligible facility pool" if body.planning_mode == "FULFIL_REQUEST"
                 else "Nearest eligible facility pool"),
                "Whole-plan gain evaluated directly; individual minima need not sum to it"]}
        return {"batch": result, "children": records}

    async def publish_crds_batch(checkpoint: Checkpoint) -> None:
        result = checkpoint["batch"]
        await store.put_many(checkpoint["children"])
        await batch_store.put_many([{**result, "recommendation_id": result["batch_recommendation_id"]}])

    async def run_queue():
        try:
            await queue.run(evaluate_request, publish_crds_batch)
        except Exception:
            logging.getLogger(__name__).exception("CRDS recommendation queue stopped")

    @app.post("/api/v1/recommendation-requests", status_code=202, response_model=RecommendationRequestRecord)
    async def submit_request(body: QueuedRecommendationRequest):
        if not queue_enabled:
            raise HTTPException(503, "RECOMMENDATION_QUEUE_DISABLED")
        if queue.stopping or app.state.recommendation_queue_task.done():
            raise HTTPException(503, "RECOMMENDATION_QUEUE_UNAVAILABLE")
        return await queue.submit(body)

    @app.get("/api/v1/recommendation-requests", response_model=RecommendationRequestPage)
    async def list_requests(status: RecommendationRequestStatus | None = None,
                            offset: int = Query(default=0, ge=0),
                            limit: int = Query(default=30, ge=1, le=100)):
        records, next_offset = await queue.page(status, offset, limit)
        return RecommendationRequestPage(enabled=queue_enabled, crds_mode=crds_config.mode,
                                         requests=records, next_offset=next_offset)

    @app.get("/api/v1/recommendation-requests/{request_id}", response_model=RecommendationRequestRecord)
    async def request_detail(request_id: UUID):
        record = await queue.get(str(request_id))
        if record.batch_recommendation_id and record.status in ("READY", "HOLD", "DEFERRED", "ADVISORY"):
            try:
                batch = await batch_store.get(str(record.batch_recommendation_id))
            except HTTPException as exc:
                if exc.status_code != 404:
                    raise
                return queue.view(record)
            batch.pop("recommendation_id")
            return record.model_copy(update={"batch": BatchRecommendation.model_validate(batch)})
        return record

    @app.post("/api/v1/batch-recommendations", status_code=201, response_model=BatchRecommendation)
    async def batch_recommend(body: BatchRecommendationRequest):
        if body.purpose == "REBALANCE" and crds_config.mode != "DISABLED":
            checkpoint = await evaluate_request(body)
            await publish_crds_batch(checkpoint)
            return checkpoint["batch"]
        fleet_raw, target_raw, sites_raw = await asyncio.gather(
            get_json(app.state.client, f"{config.fleet_url}/api/v1/vehicles"),
            get_json(app.state.client, f"{config.demand_url}/api/v1/zones/{body.target_zone_id}"),
            get_json(app.state.client, f"{config.facilities_url}/api/v1/facilities?zone_id={body.target_zone_id}"))
        now = clock.now()
        traffic = traffic_snapshot()
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
                                  "Traffic ranking evaluates a bounded baseline shortlist using approximate direct corridors; road route rechecked at approval",
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
