import asyncio
from datetime import timedelta
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.demand import DemandUpdate
from ev_fleet.demand.store import DemandStore, seed
from ev_fleet.demand.settings import DemandSettings
from ev_fleet.demand.simulation import DemandSimulator


def test_seed_reproduces_changing_bounded_sequences(clock):
    async def sequence(seed_value):
        store = DemandStore(clock)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings(simulation_enabled=True, random_seed=seed_value, min_demand=2, max_demand=7))
        values = []
        for _ in range(20):
            await simulator.tick()
            values.append([zone["demand"]["desired_available_vehicles"] for zone in (await store.snapshot())["zones"]])
        assert all(2 <= value <= 7 for tick in values for value in tick)
        assert len({tuple(tick) for tick in values}) > 1
        return values
    assert asyncio.run(sequence(42)) == asyncio.run(sequence(42))
    assert asyncio.run(sequence(42)) != asyncio.run(sequence(43))


def test_disabled_simulation_is_a_noop(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        before = await store.snapshot()
        simulator = DemandSimulator(store, DemandSettings())
        assert await simulator.tick() == 0
        assert await store.snapshot() == before
        assert (await simulator.status())["tick_count"] == 0
    asyncio.run(scenario())


def test_manual_hold_boundary_and_legacy_target_shape(clock):
    async def scenario():
        store = DemandStore(clock, manual_hold_seconds=60)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings(simulation_enabled=True, min_demand=2, max_demand=2))
        await simulator.tick()
        zone = await store.get("cbd")
        manual = await store.update("cbd", "demand", DemandUpdate(expected_version=zone["demand_version"],
            observed_at=clock.now(), desired_available_vehicles=100))
        assert "demand_source" not in manual
        clock.advance(59)
        await simulator.tick()
        assert await store.get("cbd") == manual
        assert (await simulator.status())["zones"]["cbd"]["source"] == "MANUAL"
        clock.advance(1)
        await simulator.tick()
        zone = await store.get("cbd")
        assert zone["demand"]["desired_available_vehicles"] == 2
        assert zone["demand_version"] == manual["demand_version"] + 1
        assert (await simulator.status())["zones"]["cbd"]["manual_hold_until"] is None
    asyncio.run(scenario())


def test_simulator_and_manual_updates_preserve_versions(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings(simulation_enabled=True))
        body = DemandUpdate(expected_version=0, observed_at=clock.now(), desired_available_vehicles=15)
        results = await asyncio.gather(store.update("cbd", "demand", body), simulator.tick(), return_exceptions=True)
        assert not isinstance(results[0], Exception)  # Manual wins first; tick honours its hold.
        assert (await store.get("cbd"))["demand_version"] == 1
        assert (await store.get("cbd"))["demand"]["desired_available_vehicles"] == 15
        other = DemandStore(clock)
        await seed(other)
        simulator = DemandSimulator(other, DemandSettings(simulation_enabled=True))
        results = await asyncio.gather(simulator.tick(), other.update("cbd", "demand", body), return_exceptions=True)
        assert isinstance(results[1], HTTPException) and results[1].status_code == 409
        assert (await other.get("cbd"))["demand_version"] == 1
        snapshot = await other.composition_snapshot()
        assert {zone["demand_version"] for zone in snapshot["zones"]} == {1}
    asyncio.run(scenario())


def test_tick_does_not_replace_future_manual_observation(clock):
    async def scenario():
        store = DemandStore(clock, manual_hold_seconds=0)
        await seed(store)
        await store.update("cbd", "demand", DemandUpdate(expected_version=0,
            observed_at=clock.now() + timedelta(seconds=4), desired_available_vehicles=99))
        simulator = DemandSimulator(store, DemandSettings(simulation_enabled=True))
        await simulator.tick()
        assert (await store.get("cbd"))["demand"]["desired_available_vehicles"] == 99
    asyncio.run(scenario())


def test_turning_simulation_off_preserves_existing_values(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        config = DemandSettings(simulation_enabled=True)
        simulator = DemandSimulator(store, config)
        await simulator.tick()
        before = await store.snapshot()
        config.simulation_enabled = False
        assert await simulator.tick() == 0
        assert await store.snapshot() == before
    asyncio.run(scenario())
