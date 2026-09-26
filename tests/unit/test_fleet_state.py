import asyncio
from uuid import uuid4
import pytest
from fastapi import HTTPException
from ev_fleet.contracts.vehicles import VehicleRegistration
from ev_fleet.fleet.store import FleetStore


def test_registration_retry_and_no_telemetry(clock):
    async def scenario():
        store = FleetStore(clock=clock)
        registration = VehicleRegistration(vehicle_id="ev-1", battery_capacity_kwh=60, consumption_kwh_per_km=.18)
        first = await store.register(registration)
        assert first == await store.register(registration)
        assert first["stale"] and first["connectivity"] == "OFFLINE"
        with pytest.raises(HTTPException):
            await store.register(registration.model_copy(update={"battery_capacity_kwh": 70}))
        old_request = uuid4()
        await store.session("ev-1", old_request)
        await store.session("ev-1", uuid4())
        with pytest.raises(HTTPException):
            await store.session("ev-1", old_request)
    asyncio.run(scenario())
