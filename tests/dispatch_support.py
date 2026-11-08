"""In-process HTTP cluster with injectable response loss, using the real service apps."""
from contextlib import AsyncExitStack
from unittest.mock import patch
from tests.routing_fixture import fixture_driving_route
from uuid import uuid4
import os
import httpx
from ev_fleet.common.settings import Settings, DispatchSettings, CrdsSettings
from ev_fleet.contracts.vehicles import VehicleRegistration, TelemetryUpdate
from ev_fleet.contracts.dispatch import DispatchApproval
from ev_fleet.fleet.store import FleetStore
from ev_fleet.fleet.app import create_app as fleet_app
from ev_fleet.demand.store import DemandStore, seed
from ev_fleet.demand.app import create_app as demand_app
from ev_fleet.rebalancing.app import create_app as rebalancing_app
from ev_fleet.dispatch.app import create_app as dispatch_app
from ev_fleet.facilities.store import FacilitiesStore
from ev_fleet.facilities.app import create_app as facilities_app


class Cluster:
    def __init__(self, clock, recommendation_ttl=10):
        self.clock = clock
        self.recommendation_ttl = recommendation_ttl
        self.config = DispatchSettings(fleet_url="http://fleet.test", demand_url="http://demand.test",
                                       rebalancing_url="http://rebalancing.test", facilities_url="http://facilities.test")
        self.fleet = FleetStore(self.config, clock)
        self.demand = DemandStore(clock)
        self.facilities = FacilitiesStore()
        self.failures = []
        self.stack = AsyncExitStack()
        self.sessions = {}
        self.sequences = {}

    async def __aenter__(self):
        await seed(self.demand)
        # Legacy lifecycle tests used SIMULATED routes, but production now
        # strictly requires ONEMAP. Supply an explicit, test-only road route.
        self.stack.enter_context(patch(
            "ev_fleet.dispatch.service.driving_route", new=fixture_driving_route))
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(self.route))
        await self.stack.enter_async_context(self.client)
        crds = (CrdsSettings.from_env() if "CRDS_MODE" in os.environ
                else CrdsSettings(mode="DISABLED", dispatch_url="http://dispatch.test"))
        self.apps = {"fleet.test": fleet_app(self.fleet), "demand.test": demand_app(self.demand),
            "rebalancing.test": rebalancing_app(Settings(fleet_url=self.config.fleet_url,
                demand_url=self.config.demand_url, facilities_url=self.config.facilities_url,
                recommendation_ttl_seconds=self.recommendation_ttl), self.client, self.clock,
                crds_settings=crds),
            "dispatch.test": dispatch_app(self.config, self.client, self.clock, background=False),
            "facilities.test": facilities_app(self.facilities)}
        self.transports = {host: httpx.ASGITransport(app=app) for host, app in self.apps.items()}
        for app in self.apps.values():
            await self.stack.enter_async_context(app.router.lifespan_context(app))
        self.dispatch = self.apps["dispatch.test"].state.service
        return self

    async def __aexit__(self, *args):
        await self.stack.__aexit__(*args)

    async def route(self, request):
        fault = next((item for item in self.failures if item["method"] == request.method and
                      item["path"] in str(request.url)), None)
        if fault and fault.get("once", True):
            self.failures.remove(fault)
        if fault and fault["action"] == "offline":
            raise httpx.ConnectError("simulated outage", request=request)
        response = await self.transports[request.url.host].handle_async_request(request)
        if fault and fault["action"] == "lose_response":
            raise httpx.ReadTimeout("response lost after commit", request=request)
        return response

    async def vehicle(self, name="ev-1", soc=80):
        await self.fleet.register(VehicleRegistration(vehicle_id=name, battery_capacity_kwh=60, consumption_kwh_per_km=.18))
        self.sessions[name] = (await self.fleet.session(name, uuid4()))["session_id"]
        self.sequences[name] = 0
        return await self.telemetry(name, soc=soc)

    async def telemetry(self, name="ev-1", soc=80, state="IDLE", dispatch_id=None,
                        position=None, availability=None):
        self.sequences[name] += 1
        update = TelemetryUpdate(session_id=self.sessions[name], sequence=self.sequences[name],
            observed_at=self.clock.now(), soc_pct=soc, relocation_state=state,
            availability=availability or ("UNAVAILABLE" if dispatch_id else "AVAILABLE"),
            active_dispatch_id=dispatch_id, position=position or {"latitude": 1.35, "longitude": 103.8513})
        return await self.fleet.telemetry(name, update)

    async def recommend(self):
        response = await self.client.post("http://rebalancing.test/api/v1/recommendations", json={"target_zone_id": "cbd"})
        assert response.status_code == 201, response.text
        return response.json()

    async def approve(self, recommendation=None):
        recommendation = recommendation or await self.recommend()
        body = DispatchApproval(request_id=uuid4(), recommendation_id=recommendation["recommendation_id"])
        return await self.dispatch.approve(body), body
