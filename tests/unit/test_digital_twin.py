"""Digital-twin acceptance: the edge simulator owns movement, Fleet owns history."""
import asyncio
from uuid import uuid4

import pytest
from fastapi import HTTPException
from ev_fleet.contracts.vehicles import TelemetryUpdate
from ev_fleet.simulator.commands import MovingNode
from ev_fleet.simulator.movement import Movement
from tests.phase2_support import Cluster


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
