import httpx
from ev_fleet.demand.app import create_app
from ev_fleet.demand.settings import DemandSettings
from ev_fleet.contracts.demand import DemandUpdate
from tests.dispatch_support import Cluster


class DemandCluster(Cluster):
    def __init__(self, clock, **overrides):
        super().__init__(clock)
        self.demand_config = DemandSettings(fleet_url="http://fleet.test", **overrides)

    async def __aenter__(self):
        await super().__aenter__()
        app = create_app(self.demand, settings=self.demand_config, client=self.client)
        self.apps["demand.test"] = app
        self.transports["demand.test"] = httpx.ASGITransport(app=app)
        await self.stack.enter_async_context(app.router.lifespan_context(app))
        return self

    async def set_demand(self, zone_id="cbd", value=10):
        zone = await self.demand.get(zone_id)
        return await self.demand.update(zone_id, "demand", DemandUpdate(expected_version=zone["demand_version"],
            observed_at=self.clock.now(), desired_available_vehicles=value))

    async def heatmap(self, path="/api/v1/heat-map"):
        response = await self.client.get("http://demand.test" + path)
        assert response.status_code == 200, response.text
        return response.json()
