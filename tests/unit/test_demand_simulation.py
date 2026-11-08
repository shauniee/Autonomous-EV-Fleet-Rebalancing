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


def test_start_and_pause_control_one_simulation_loop(clock):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings(
            update_interval_seconds=.01, min_demand=2, max_demand=2))

        status = await simulator.status()
        assert status["enabled"] is False and status["running"] is False
        assert status["tick_count"] == 0

        first, second = await asyncio.gather(simulator.start(), simulator.start())
        assert first["enabled"] is True and second["enabled"] is True
        assert first["tick_count"] == second["tick_count"] == 1
        assert (await simulator.status())["running"] is True

        await simulator.pause()
        paused = await simulator.status()
        snapshot = await store.snapshot()
        assert paused["enabled"] is False and paused["running"] is False
        await asyncio.sleep(.03)
        assert await store.snapshot() == snapshot

        resumed = await simulator.start()
        assert resumed["tick_count"] == 2 and resumed["running"] is True
        await simulator.close()
        assert (await simulator.status())["running"] is False

    asyncio.run(scenario())


def test_pause_waits_for_an_in_progress_tick(clock, monkeypatch):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings(update_interval_seconds=.01))
        await simulator.start()
        entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def slow_tick(_simulator):
            entered.set()
            await release.wait()
            completed.set()
            return 0

        monkeypatch.setattr(store, "simulation_tick", slow_tick)
        await asyncio.wait_for(entered.wait(), 1)
        pause = asyncio.create_task(simulator.pause())
        await asyncio.sleep(0)
        assert not pause.done()
        release.set()
        await asyncio.wait_for(pause, 1)
        assert completed.is_set()
        assert (await simulator.status())["running"] is False

    asyncio.run(scenario())


def test_failed_initial_tick_leaves_simulation_paused(clock, monkeypatch):
    async def scenario():
        store = DemandStore(clock)
        await seed(store)
        simulator = DemandSimulator(store, DemandSettings())

        async def fail(_simulator):
            raise RuntimeError("persistence failed")

        monkeypatch.setattr(store, "simulation_tick", fail)
        with pytest.raises(RuntimeError, match="persistence failed"):
            await simulator.start()
        status = await simulator.status()
        assert status["enabled"] is False and status["running"] is False

    asyncio.run(scenario())
