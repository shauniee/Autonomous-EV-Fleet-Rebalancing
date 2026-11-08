"""Isolated real-service trial cluster with deterministic simulated time."""
import asyncio
from contextlib import AsyncExitStack
from unittest.mock import patch
from tests.routing_fixture import fixture_driving_route
from datetime import datetime, timedelta, timezone
from time import perf_counter
from uuid import uuid4
import httpx
from ev_fleet.common.settings import CrdsSettings, DispatchSettings, Settings
from ev_fleet.contracts.demand import ChargingUpdate, DemandUpdate
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration
from ev_fleet.demand.app import create_app as demand_app
from ev_fleet.demand.settings import DemandSettings
from ev_fleet.demand.store import DemandStore, seed as seed_zones
from ev_fleet.dispatch.app import create_app as dispatch_app
from ev_fleet.fleet.app import create_app as fleet_app
from ev_fleet.fleet.store import FleetStore
from ev_fleet.rebalancing.app import create_app as rebalancing_app
from ev_fleet.simulator.movement import Movement
from .scenarios import ScenarioSpec


class ExperimentClock:
    def __init__(self):
        self.elapsed = 0.0
        self.origin = datetime(2026, 10, 1, tzinfo=timezone.utc)
        self.real_origin = perf_counter()

    def now(self):
        return self.origin + timedelta(seconds=self.elapsed)

    def monotonic(self):
        # Real time preserves meaningful recommendation latency; elapsed drives lifecycle timeouts.
        return self.elapsed + perf_counter() - self.real_origin

    def advance(self, seconds):
        if seconds < 0:
            raise ValueError("clock cannot move backwards")
        self.elapsed += seconds


class EvaluationCluster:
    def __init__(self, strategy):
        self.strategy = strategy
        self.clock = ExperimentClock()
        self.settings = DispatchSettings(
            fleet_url="http://fleet.test", demand_url="http://demand.test",
            rebalancing_url="http://rebalancing.test", stale_seconds=60,
            offline_seconds=120, recommendation_ttl_seconds=3600,
            ack_timeout_seconds=60, progress_timeout_seconds=120,
            reconcile_interval_seconds=1)
        self.demand_settings = DemandSettings(
            fleet_url=self.settings.fleet_url, demand_max_age_seconds=100000,
            fleet_snapshot_max_age_seconds=5, telemetry_max_age_seconds=60)
        self.fleet = FleetStore(self.settings, self.clock)
        self.demand = DemandStore(self.clock, manual_hold_seconds=100000)
        self.stack = AsyncExitStack()
        self.sessions = {}
        self.sequences = {}
        self.specs = {}

    async def __aenter__(self):
        await seed_zones(self.demand)
        # Experiment scenarios are offline and deterministic; approval still
        # exercises the production OneMap-only route validation and claims.
        self.stack.enter_context(patch(
            "ev_fleet.dispatch.service.driving_route", new=fixture_driving_route))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.route), timeout=5)
        await self.stack.enter_async_context(self.client)
        self.apps = {
            "fleet.test": fleet_app(self.fleet),
            "demand.test": demand_app(self.demand, seed_zones=False,
                                      settings=self.demand_settings, client=self.client),
            "rebalancing.test": rebalancing_app(
                Settings(fleet_url=self.settings.fleet_url, demand_url=self.settings.demand_url,
                         stale_seconds=self.settings.stale_seconds,
                         offline_seconds=self.settings.offline_seconds,
                         recommendation_ttl_seconds=self.settings.recommendation_ttl_seconds),
                self.client, self.clock, self.strategy,
                crds_settings=CrdsSettings(mode="DISABLED", dispatch_url="http://dispatch.test")),
            "dispatch.test": dispatch_app(self.settings, self.client, self.clock, background=False),
        }
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in self.apps.items()}
        for app in self.apps.values():
            await self.stack.enter_async_context(app.router.lifespan_context(app))
        return self

    async def __aexit__(self, *args):
        await self.stack.__aexit__(*args)

    async def route(self, request):
        return await self.transports[request.url.host].handle_async_request(request)

    async def register(self, spec):
        await self.fleet.register(VehicleRegistration(
            vehicle_id=spec.vehicle_id, battery_capacity_kwh=spec.battery_capacity_kwh,
            consumption_kwh_per_km=spec.consumption_kwh_per_km))
        self.sessions[spec.vehicle_id] = (await self.fleet.session(spec.vehicle_id, uuid4()))["session_id"]
        self.sequences[spec.vehicle_id] = 0
        self.specs[spec.vehicle_id] = spec
        await self.telemetry(spec.vehicle_id, spec.position, spec.soc_pct)

    async def telemetry(self, vehicle_id, position, soc_pct, relocation_state="IDLE",
                        dispatch_id=None, distance=None):
        sequence = self.sequences[vehicle_id]
        self.sequences[vehicle_id] += 1
        return await self.fleet.telemetry(vehicle_id, TelemetryUpdate(
            session_id=self.sessions[vehicle_id], sequence=sequence,
            observed_at=self.clock.now(), position=position, soc_pct=soc_pct,
            availability="UNAVAILABLE" if dispatch_id else "AVAILABLE",
            relocation_state=relocation_state, active_dispatch_id=dispatch_id,
            distance_travelled_km=distance))

    async def refresh_idle(self, selected=None):
        for vehicle_id, spec in self.specs.items():
            if vehicle_id != selected and spec.online:
                await self.telemetry(vehicle_id, spec.position, spec.soc_pct)

    async def set_demand(self, zone_id, value):
        current = await self.demand.get(zone_id)
        return await self.demand.update(zone_id, "demand", DemandUpdate(
            expected_version=current["demand_version"], observed_at=self.clock.now(),
            desired_available_vehicles=value))

    async def set_charging(self, zone_id, slots):
        current = await self.demand.get(zone_id)
        return await self.demand.update(zone_id, "charging", ChargingUpdate(
            expected_version=current["charging_version"], observed_at=self.clock.now(),
            available_slots=slots))

    async def setup(self, scenario: ScenarioSpec):
        # An offline vehicle really had telemetry, then aged out before the other EVs appeared.
        offline = [vehicle for vehicle in scenario.vehicles if not vehicle.online]
        online = [vehicle for vehicle in scenario.vehicles if vehicle.online]
        for vehicle in offline:
            await self.register(vehicle)
        if offline:
            self.clock.advance(self.settings.offline_seconds + 1)
        for vehicle in online:
            await self.register(vehicle)
        for zone_id, value in scenario.demands.items():
            await self.set_demand(zone_id, value)
        await self.set_charging(scenario.target_zone_id, scenario.charging_slots)

    async def heatmap_zone(self, zone_id):
        response = await self.client.get("http://demand.test/api/v1/heat-map")
        response.raise_for_status()
        return next(zone for zone in response.json()["zones"] if zone["zone_id"] == zone_id)

    async def recommend(self, target_zone_id):
        started = perf_counter()
        response = await self.client.post("http://rebalancing.test/api/v1/recommendations",
                                          json={"target_zone_id": target_zone_id})
        elapsed = (perf_counter() - started) * 1000
        response.raise_for_status()
        return response.json(), elapsed

    async def approve(self, recommendation):
        return await self.client.post("http://dispatch.test/api/v1/dispatches", json={
            "request_id": str(uuid4()), "recommendation_id": recommendation["recommendation_id"]})

    async def drive(self, approval, scenario, speed_kph, step_seconds):
        vehicle_id = approval["vehicle_id"]
        spec = self.specs[vehicle_id]
        command_response = await self.client.get(
            f"http://dispatch.test/api/v1/vehicles/{vehicle_id}/commands")
        command_response.raise_for_status()
        command = command_response.json()["commands"][0]
        movement = Movement(spec.position, spec.soc_pct, spec.battery_capacity_kwh,
                            spec.consumption_kwh_per_km)
        if not movement.accept(command):
            raise RuntimeError("dispatch revalidation accepted a command rejected by the same EV model")
        dispatch_id = approval["dispatch_id"]
        ack = await self.client.post(
            f"http://dispatch.test/api/v1/dispatches/{dispatch_id}/acknowledgements", json={
                "command_id": command["command_id"], "vehicle_id": vehicle_id,
                "accepted": True, "reason": None})
        ack.raise_for_status()
        movement.start()
        approved_at = self.clock.elapsed
        event_sequence = 0
        demand_changed = False
        disconnected = False
        transient_states = [ack.json()["state"]]

        for _ in range(250):
            self.clock.advance(step_seconds)
            movement.advance(step_seconds, speed_kph)
            fraction = 1 if not movement.total else movement.distance / movement.total

            if scenario.disconnect_selected and not disconnected and fraction >= .25:
                outage = self.settings.offline_seconds + 1
                self.clock.advance(outage)
                movement.advance(outage, speed_kph)
                await self.refresh_idle(selected=vehicle_id)
                status = await self.client.get(f"http://dispatch.test/api/v1/dispatches/{dispatch_id}")
                status.raise_for_status()
                transient_states.append(status.json()["state"])
                disconnected = True

            if scenario.demand_change is not None and not demand_changed and fraction >= .4:
                await self.set_demand(scenario.target_zone_id, scenario.demand_change)
                demand_changed = True

            await self.refresh_idle(selected=vehicle_id)
            await self.telemetry(vehicle_id, movement.position, movement.soc_pct,
                                 movement.state, dispatch_id, movement.distance)
            kind = {"RELOCATING": "PROGRESS", "ARRIVED": "ARRIVED", "FAILED": "FAILED"}[movement.state]
            event = await self.client.post(
                f"http://dispatch.test/api/v1/dispatches/{dispatch_id}/events", json={
                    "command_id": command["command_id"], "vehicle_id": vehicle_id,
                    "sequence": event_sequence, "observed_at": self.clock.now().isoformat(),
                    "kind": kind, "distance_travelled_km": movement.distance,
                    "reason": movement.reason})
            event_sequence += 1
            event.raise_for_status()
            status = event.json()
            transient_states.append(status["state"])
            if status["state"] in ("SUCCEEDED", "REJECTED", "FAILED"):
                if status["state"] == "SUCCEEDED":
                    movement.complete()
                    await self.telemetry(vehicle_id, movement.position, movement.soc_pct)
                return status, movement, self.clock.elapsed - approved_at, transient_states
        status = (await self.client.get(
            f"http://dispatch.test/api/v1/dispatches/{dispatch_id}")).json()
        return status, movement, self.clock.elapsed - approved_at, transient_states


async def execute_trial(scenario, strategy, config):
    async with EvaluationCluster(strategy) as cluster:
        await cluster.setup(scenario)
        before = await cluster.heatmap_zone(scenario.target_zone_id)
        recommendations = await asyncio.gather(*(
            cluster.recommend(scenario.target_zone_id)
            for _ in range(scenario.concurrent_requests)))
        recommendation_rows = [item[0] for item in recommendations]
        request_latencies = [item[1] for item in recommendations]
        approval_responses = await asyncio.gather(*(
            cluster.approve(recommendation) for recommendation in recommendation_rows))
        approval_outcomes = []
        approved = []
        for response in approval_responses:
            if response.status_code == 202:
                data = response.json()
                approved.append(data)
                approval_outcomes.append({"status": data["state"], "dispatch_id": data["dispatch_id"],
                                          "vehicle_id": data["vehicle_id"]})
            else:
                body = response.json()
                approval_outcomes.append({"status": "REJECTED",
                    "reason": body.get("error", {}).get("code", f"HTTP_{response.status_code}")})

        primary_recommendation = recommendation_rows[0]
        candidate = next((row for row in primary_recommendation["ranked_candidates"]
                          if row["vehicle_id"] == primary_recommendation["selected_vehicle_id"]), None)
        status = None
        movement = None
        completion_time = None
        transient_states = []
        if approved:
            status, movement, completion_time, transient_states = await cluster.drive(
                approved[0], scenario, config.speed_kph, config.step_seconds)
        after = await cluster.heatmap_zone(scenario.target_zone_id)
        succeeded = int(status is not None and status["state"] == "SUCCEEDED")
        failed_count = scenario.concurrent_requests - succeeded
        rejected_reasons = [outcome.get("reason") for outcome in approval_outcomes
                            if outcome["status"] == "REJECTED" and outcome.get("reason")]
        actual_arrival_soc = status.get("arrival_soc_pct") if status else None
        poor = failed_count > 0 or (actual_arrival_soc is not None and
                                    actual_arrival_soc < config.low_arrival_soc_pct)
        return {
            "trial_id": f"{scenario.name}-{scenario.seed}-{strategy.name}",
            "scenario": scenario.name, "seed": scenario.seed, "algorithm": strategy.name,
            "policy_version": strategy.policy_version, "initial_state_hash": scenario.state_hash(),
            "target_zone_id": scenario.target_zone_id,
            "request_count": scenario.concurrent_requests,
            "selected_vehicle_id": primary_recommendation["selected_vehicle_id"],
            "selected_vehicle_ids": [row["selected_vehicle_id"] for row in recommendation_rows],
            "eligible_vehicle_count": len(primary_recommendation["ranked_candidates"]),
            "excluded_vehicle_count": len(primary_recommendation["excluded_candidates"]),
            "estimated_relocation_distance_km": candidate["distance_km"] if candidate else None,
            "actual_relocation_distance_km": movement.distance if movement else None,
            "initial_soc_pct": candidate["current_soc_pct"] if candidate else None,
            "predicted_arrival_soc_pct": candidate["predicted_arrival_soc_pct"] if candidate else None,
            "actual_arrival_soc_pct": actual_arrival_soc,
            "arrival_soc_error_pct": (actual_arrival_soc - candidate["predicted_arrival_soc_pct"]
                                      if actual_arrival_soc is not None and candidate else None),
            "recommendation_latency_ms": primary_recommendation["decision_latency_ms"],
            "recommendation_request_latency_ms": sum(request_latencies) / len(request_latencies),
            "relocation_completion_time_s": completion_time if succeeded else None,
            "terminal_time_s": completion_time,
            "successful_relocations": succeeded,
            "successful_relocation_rate": succeeded / scenario.concurrent_requests,
            "low_initial_soc": bool(candidate and candidate["current_soc_pct"] < config.low_arrival_soc_pct),
            "predicted_low_arrival_soc": bool(candidate and candidate["predicted_arrival_soc_pct"] < config.low_arrival_soc_pct),
            "actual_low_arrival_soc": (actual_arrival_soc < config.low_arrival_soc_pct
                                       if actual_arrival_soc is not None else None),
            "failed_or_aborted_relocations": failed_count,
            "operationally_poor_outcome": poor,
            "final_dispatch_state": status["state"] if status else "REJECTED",
            "failure_reason": (status.get("failure_reason") if status and status.get("failure_reason") else
                               ",".join(rejected_reasons) if rejected_reasons else
                               "NO_APPROVED_DISPATCH" if not status else None),
            "transient_dispatch_states": transient_states,
            "approval_outcomes": approval_outcomes,
            "demand_before": before["rental_demand"], "demand_after": after["rental_demand"],
            "available_evs_before": before["available_evs"], "available_evs_after": after["available_evs"],
            "current_shortage_before": before["current_shortage"],
            "current_shortage_after": after["current_shortage"],
            "projected_shortage_before": before["projected_shortage"],
            "projected_shortage_after": after["projected_shortage"],
            "shortage_change": (after["current_shortage"] - before["current_shortage"]
                                if before["current_shortage"] is not None and after["current_shortage"] is not None else None),
            "charging_slots": scenario.charging_slots,
            "infrastructure_error": None,
        }
