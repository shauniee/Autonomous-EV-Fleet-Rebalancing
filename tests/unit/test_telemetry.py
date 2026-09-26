import asyncio
from datetime import timedelta
from uuid import uuid4
import pytest
from fastapi import HTTPException
from ev_fleet.fleet.store import FleetStore
from ev_fleet.contracts.vehicles import VehicleRegistration, TelemetryUpdate


async def registered(clock):
    store = FleetStore(clock=clock)
    await store.register(VehicleRegistration(vehicle_id="ev-1", battery_capacity_kwh=60, consumption_kwh_per_km=.18))
    session = await store.session("ev-1", uuid4())
    update = TelemetryUpdate(session_id=session["session_id"], sequence=1, observed_at=clock.now(),
                             position={"latitude": 1.3, "longitude": 103.8}, soc_pct=80)
    return store, update


def test_duplicate_does_not_refresh_and_boundaries(clock):
    async def scenario():
        store, update = await registered(clock)
        await store.telemetry("ev-1", update)
        clock.advance(15)
        result = await store.telemetry("ev-1", update)
        assert result["duplicate"] and result["vehicle"]["stale"]
        assert result["vehicle"]["connectivity"] == "ONLINE"
        clock.advance(15)
        assert (await store.get("ev-1"))["connectivity"] == "OFFLINE"
    asyncio.run(scenario())


def test_sequence_conflicts_and_session_replacement(clock):
    async def scenario():
        store, update = await registered(clock)
        await store.telemetry("ev-1", update)
        for changed in [update.model_copy(update={"soc_pct": 79}), update.model_copy(update={"sequence": 0})]:
            with pytest.raises(HTTPException) as caught:
                await store.telemetry("ev-1", changed)
            assert caught.value.status_code == 409
        request_id = uuid4()
        session = await store.session("ev-1", request_id)
        assert session == await store.session("ev-1", request_id)
        assert (await store.get("ev-1"))["stale"]
        with pytest.raises(HTTPException):
            await store.telemetry("ev-1", update)
    asyncio.run(scenario())


@pytest.mark.parametrize("offset", [-15, 6])
def test_old_and_future_observations(clock, offset):
    async def scenario():
        store, update = await registered(clock)
        update.observed_at += timedelta(seconds=offset)
        with pytest.raises(HTTPException) as caught:
            await store.telemetry("ev-1", update)
        assert caught.value.status_code == 422
    asyncio.run(scenario())


def test_concurrent_sequences_preserve_newest(clock):
    async def scenario():
        store, update = await registered(clock)
        results = await asyncio.gather(*(store.telemetry("ev-1", update.model_copy(update={"sequence": seq}))
                                         for seq in [5, 1, 9, 3, 10, 2]), return_exceptions=True)
        current = await store.get("ev-1")
        assert current["telemetry"]["sequence"] == 10
        accepted = sum(isinstance(result, dict) for result in results)
        assert current["state_version"] == 2 + accepted
    asyncio.run(scenario())
