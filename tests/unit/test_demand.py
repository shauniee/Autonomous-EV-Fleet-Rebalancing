import asyncio
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.demand import DemandUpdate, ChargingUpdate
from ev_fleet.demand.store import DemandStore, seed


def test_singapore_seeds_and_independent_versions(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        assert len((await store.snapshot())["zones"]) == 6
        await store.update("cbd", "demand", DemandUpdate(expected_version=0,
                           observed_at=clock.now(), desired_available_vehicles=12))
        zone = await store.update("cbd", "charging", ChargingUpdate(expected_version=0,
                                  observed_at=clock.now(), available_slots=2))
        assert zone["version"] == 3 and zone["demand_version"] == zone["charging_version"] == 1
        with pytest.raises(HTTPException) as caught:
            await store.update("cbd", "demand", DemandUpdate(expected_version=0,
                               observed_at=clock.now(), desired_available_vehicles=3))
        assert caught.value.status_code == 409
    asyncio.run(scenario())


def test_concurrent_demand_updates_only_one_wins(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        result = await asyncio.gather(*(store.update("cbd", "demand", DemandUpdate(
            expected_version=0, observed_at=clock.now(), desired_available_vehicles=count))
            for count in [4, 8]), return_exceptions=True)
        assert sum(isinstance(item, HTTPException) for item in result) == 1
        assert (await store.get("cbd"))["demand_version"] == 1
    asyncio.run(scenario())
