"""Digital-twin acceptance: the edge simulator owns movement, Fleet owns history."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from ev_fleet.contracts.vehicles import TelemetryUpdate
from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.movement import Movement
from tests.routing_fixture import fixture_route
from tests.dispatch_support import Cluster


def test_adaptive_telemetry_frequency_and_synthetic_condition_fields():
    node = MovingNode("http://fleet", "http://dispatch", "ev-twin",
        {"latitude": 1.35, "longitude": 103.85}, 75)
    assert node.telemetry_delay() == 5
    node.movement.charging_state = "CHARGING"
    assert node.telemetry_delay() == 2
    node.movement.charging_state = "NOT_CHARGING"
    node.movement.state = "RELOCATING"
    assert node.telemetry_delay() == 1
    node.movement.state = "IDLE"

    model = Movement({"latitude": 1.35, "longitude": 103.85}, 75)
    reading = model.telemetry()
    assert reading["gps_quality"] == "SIMULATED"
    assert reading["speed_kph"] == 0
    assert reading["odometer_km"] == 0
    assert reading["fault_codes"] == []
    assert reading["battery_soh_pct"] == 98
    assert reading["battery_temp_c"] is not None
    assert reading["motor_temp_c"] is not None

    command = {"command_id": str(uuid4()), "dispatch_id": str(uuid4()),
               "target_position": {"latitude": 1.36, "longitude": 103.86},
               "route": fixture_route(
                   {"latitude": 1.35, "longitude": 103.85},
                   {"latitude": 1.36, "longitude": 103.86}).model_dump(mode="json"),
               "min_reserve_pct": 20, "route_factor": 1.3, "energy_margin_factor": 1.15}
    assert model.accept(command) is True
    model.start()
    model.advance(30, 30)
    moving = model.telemetry()
    assert 0 < moving["speed_kph"] <= 30
    assert moving["odometer_km"] > 0
    assert moving["power_kw"] > 0
    assert 0 <= moving["heading_deg"] < 360


def test_fleet_accepted_only_append_history_and_preserve_latest_snapshot(clock):
    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-twin", soc=80)
            await cluster.telemetry("ev-twin", soc=79)
            history = await cluster.client.get(
                "http://fleet.test/api/v1/vehicles/ev-twin/telemetry-history?limit=100")
            assert history.status_code == 200, history.text
            response = history.json()
            assert response["vehicle_id"] == "ev-twin"
            assert response["storage"] == "ephemeral"
            assert len(response["samples"]) == 2
            assert [x["sequence"] for x in response["samples"]] == [1, 2]
            assert response["samples"][0]["session_id"] == cluster.sessions["ev-twin"]
            assert response["samples"][1]["telemetry"]["soc_pct"] == 79
            assert (await cluster.fleet.get("ev-twin"))["telemetry"]["soc_pct"] == 79

            update = TelemetryUpdate(
                session_id=cluster.sessions["ev-twin"], sequence=2,
                observed_at=clock.now(),
                position={"latitude": 1.35, "longitude": 103.8513}, soc_pct=79)
            receipt = await cluster.fleet.telemetry("ev-twin", update)
            assert receipt["duplicate"] is True
            assert len((await cluster.fleet.telemetry_history("ev-twin"))["samples"]) == 2

            update = update.model_copy(update={"sequence": 1, "soc_pct": 30})
            with pytest.raises(HTTPException) as failed:
                await cluster.fleet.telemetry("ev-twin", update)
            assert failed.value.detail == "SEQUENCE_CONFLICT"
            assert len((await cluster.fleet.telemetry_history("ev-twin"))["samples"]) == 2
            assert (await cluster.fleet.get("ev-twin"))["telemetry"]["soc_pct"] == 79

            invalid = await cluster.client.get(
                "http://fleet.test/api/v1/vehicles/ev-twin/telemetry-history?limit=501")
            assert invalid.status_code == 422
            missing = await cluster.client.get(
                "http://fleet.test/api/v1/vehicles/unknown/telemetry-history")
            assert missing.status_code == 404
    asyncio.run(scenario())


def test_telemetry_model_rejects_invalid_digital_twin_measurements():
    args = {"session_id": str(uuid4()), "sequence": 0,
            "observed_at": "2026-10-28T11:00:00Z",
            "position": {"latitude": 1.35, "longitude": 103.85}, "soc_pct": 70}
    assert TelemetryUpdate(**args, speed_kph=0, odometer_km=0,
        gps_quality="SIMULATED", battery_soh_pct=99).sequence == 0
    for extra in [{"speed_kph": -1}, {"heading_deg": 360}, {"odometer_km": -1},
                  {"battery_temp_c": 200}, {"gps_quality": "GNSS_TRUSTED"}]:
        with pytest.raises(ValueError):
            TelemetryUpdate(**args, **extra)


def test_one_hertz_idle_telemetry_does_not_invalidate_safely_rechecked_control_claim(clock):
    from datetime import timedelta
    from ev_fleet.contracts.dispatch import ClaimRequest

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1", soc=80)
            before = await cluster.fleet.get("ev-1")
            origin = before["telemetry"]["position"]
            request = ClaimRequest(
                command_id=uuid4(), expected_instance_id=cluster.fleet.instance_id,
                expected_version=before["state_version"],
                expected_control_version=before["control_version"],
                route_origin=origin,
                target_zone_id="cbd",
                target_position={"latitude": 1.283, "longitude": 103.8513},
                approve_before=clock.now() + timedelta(seconds=60),
                min_reserve_pct=20, route_factor=1.3,
                energy_margin_factor=1.15)
            for _ in range(5):
                await cluster.telemetry("ev-1", soc=79)
            after = await cluster.fleet.get("ev-1")
            assert after["state_version"] > before["state_version"]
            assert after["control_version"] == before["control_version"]
            claim = await cluster.fleet.claim("ev-1", uuid4(), request)
            assert claim["status"] == "ACTIVE"
            assert (await cluster.fleet.get("ev-1"))["control_version"] > before["control_version"]

    asyncio.run(scenario())


def test_control_change_or_moved_route_origin_still_rejects_delayed_claim(clock):
    from datetime import timedelta
    from ev_fleet.contracts.dispatch import ClaimRequest

    async def scenario():
        async with Cluster(clock) as cluster:
            await cluster.vehicle("ev-1", soc=80)
            before = await cluster.fleet.get("ev-1")
            def request():
                return ClaimRequest(
                    command_id=uuid4(), expected_instance_id=cluster.fleet.instance_id,
                    expected_version=before["state_version"],
                    expected_control_version=before["control_version"],
                    route_origin=before["telemetry"]["position"],
                    target_zone_id="cbd",
                    target_position={"latitude": 1.283, "longitude": 103.8513},
                    approve_before=clock.now() + timedelta(seconds=60),
                    min_reserve_pct=20, route_factor=1.3,
                    energy_margin_factor=1.15)
            await cluster.telemetry("ev-1", soc=79,
                position={"latitude": 1.3500, "longitude": 103.855})
            with pytest.raises(HTTPException) as offroad:
                await cluster.fleet.claim("ev-1", uuid4(), request())
            assert offroad.value.detail == "ROUTE_ORIGIN_CHANGED"

            await cluster.telemetry("ev-1", soc=79, availability="UNAVAILABLE")
            with pytest.raises(HTTPException) as blocked:
                await cluster.fleet.claim("ev-1", uuid4(), request())
            assert blocked.value.detail == "VEHICLE_VERSION_CONFLICT"
    asyncio.run(scenario())


def test_durable_history_read_does_not_hold_fleet_writer_lock(clock):
    from ev_fleet.contracts.vehicles import VehicleRegistration
    from ev_fleet.fleet.store import FleetStore
    from tests.unit.test_persistence import MemoryRepository

    class SlowHistoryRepository(MemoryRepository):
        def __init__(self):
            super().__init__()
            self.reading = asyncio.Event()
            self.release = asyncio.Event()

        async def telemetry_history(self, vehicle_id, limit=100):
            self.reading.set()
            await self.release.wait()
            return []

    async def scenario():
        repository = SlowHistoryRepository()
        store = FleetStore(clock=clock, repository=repository)
        await store.start()
        await store.register(VehicleRegistration(
            vehicle_id="ev-1", battery_capacity_kwh=60, consumption_kwh_per_km=.18))
        history = asyncio.create_task(store.telemetry_history("ev-1"))
        await repository.reading.wait()
        # A write must not queue behind the in-flight database read.
        await asyncio.wait_for(store.register(VehicleRegistration(
            vehicle_id="ev-2", battery_capacity_kwh=60, consumption_kwh_per_km=.18)), .5)
        repository.release.set()
        assert (await history)["storage"] == "postgresql"

    asyncio.run(scenario())
