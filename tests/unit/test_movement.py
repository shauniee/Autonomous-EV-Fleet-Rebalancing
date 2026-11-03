from uuid import uuid4
import pytest
from ev_fleet.contracts.common import utcnow
from ev_fleet.simulator.movement import Movement
from tests.routing_fixture import fixture_route


def command(origin=None):
    origin = origin or {"latitude": 1.35, "longitude": 103.8513}
    target = {"latitude": 1.283, "longitude": 103.8513}
    return {"command_id": str(uuid4()), "dispatch_id": str(uuid4()), "vehicle_id": "ev-1",
            "fleet_instance_id": str(uuid4()), "target_zone_id": "cbd",
            "target_position": target,
            "route": fixture_route(origin, target).model_dump(mode="json"),
            "issued_at": utcnow().isoformat(), "min_reserve_pct": 20,
            "route_factor": 1.3, "energy_margin_factor": 1.15}


def test_gradual_movement_energy_no_overshoot_and_deduplication():
    origin = {"latitude": 1.35, "longitude": 103.8513}
    moving = Movement(origin, 80)
    cmd = command()
    assert moving.accept(cmd)
    moving.advance(60, 30)
    assert moving.position == origin  # Wait for acknowledgement before moving.
    moving.start()
    moving.advance(60, 30)
    assert cmd["target_position"]["latitude"] < moving.position["latitude"] < origin["latitude"]
    assert moving.distance == .5
    assert moving.soc_pct == pytest.approx(80 - 100 * .5 * .18 / 60)
    assert moving.accept(cmd)  # Same command must not reset position or distance.
    assert moving.distance == .5
    moving.advance(100000, 30)
    assert moving.state == "ARRIVED" and moving.position == cmd["target_position"]
    assert moving.distance == pytest.approx(moving.total)
    soc = moving.soc_pct
    moving.advance(100000, 30)
    assert moving.soc_pct == soc
    moving.complete()
    moving.accept(cmd)
    assert moving.command is None and moving.state == "IDLE"


def test_insufficient_local_soc_and_active_command_conflict():
    moving = Movement({"latitude": 1.35, "longitude": 103.8513}, 20)
    cmd = command()
    assert not moving.accept(cmd)
    assert moving.reason == "INSUFFICIENT_LOCAL_SOC"
    moving.start()
    moving.advance(1000, 30)
    assert moving.soc_pct == 20
    with pytest.raises(ValueError):
        moving.accept(command())
    with pytest.raises(ValueError):
        moving.accept(dict(cmd, target_zone_id="woodlands"))


def test_zero_distance_arrival_and_battery_failure():
    cmd = command()
    moving = Movement(cmd["target_position"], 80)
    # Zero-distance journeys must still have route geometry whose origin
    # matches the physical EV position, not an off-road connector.
    cmd["route"] = fixture_route(cmd["target_position"], cmd["target_position"]).model_dump(mode="json")
    moving.accept(cmd)
    moving.start()
    moving.advance(1, 30)
    assert moving.state == "ARRIVED" and moving.soc_pct == 80
    moving = Movement({"latitude": 1.35, "longitude": 103.8513}, 80)
    moving.accept(command())
    moving.start()
    moving.soc_pct = .001  # Physical fault after acceptance.
    moving.advance(1000, 30)
    assert moving.state == "FAILED" and moving.soc_pct == 0


def test_charging_requires_allocated_position_and_stops_at_target():
    position = {"latitude": 1.283, "longitude": 103.8513}
    parking = {"facility_id": "lta-charging-demo", "position": position,
               "charging_allocated": True, "charging_power_kw": 12,
               "charging_target_soc_pct": 80}
    moving = Movement(position, 70)
    assert moving.park(parking)
    assert moving.telemetry()["availability"] == "UNAVAILABLE"
    moving.charge(1800)  # 6 kWh into a 60 kWh battery.
    assert moving.soc_pct == pytest.approx(80)
    assert moving.charging_state == "COMPLETE"
    assert moving.telemetry()["availability"] == "AVAILABLE"
    moving.charge(1800)
    assert moving.soc_pct == pytest.approx(80)
    elsewhere = Movement({"latitude": 1.35, "longitude": 103.85}, 70)
    assert not elsewhere.park(parking)
    elsewhere.charge(1800)
    assert elsewhere.soc_pct == 70
