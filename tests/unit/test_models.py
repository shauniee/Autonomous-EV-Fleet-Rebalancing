import pytest
from pydantic import ValidationError
from ev_fleet.common.settings import Settings
from ev_fleet.contracts.common import Position
from ev_fleet.contracts.vehicles import TelemetryUpdate, VehicleRegistration


@pytest.mark.parametrize("field,value", [("battery_capacity_kwh", 0), ("consumption_kwh_per_km", -1),
                                        ("battery_capacity_kwh", float("nan")), ("vehicle_id", "bad/id")])
def test_bad_registration(field, value):
    body = dict(vehicle_id="ev-1", battery_capacity_kwh=60, consumption_kwh_per_km=.18)
    body[field] = value
    with pytest.raises(ValidationError):
        VehicleRegistration(**body)


@pytest.mark.parametrize("latitude,longitude", [(0, 103.8), (1.3, 0), (float("inf"), 103.8)])
def test_singapore_envelope(latitude, longitude):
    with pytest.raises(ValidationError):
        Position(latitude=latitude, longitude=longitude)


@pytest.mark.parametrize("overrides", [{"soc_pct": 101}, {"soc_pct": -1},
                                      {"observed_at": "2026-09-26T00:00:00"}, {"sequence": -1}])
def test_bad_telemetry(overrides):
    body = dict(session_id="00000000-0000-0000-0000-000000000001", sequence=0,
                observed_at="2026-09-26T00:00:00Z", position={"latitude": 1.3, "longitude": 103.8}, soc_pct=80)
    with pytest.raises(ValidationError):
        TelemetryUpdate(**(body | overrides))


def test_threshold_order():
    with pytest.raises(ValidationError):
        Settings(stale_seconds=30, offline_seconds=15)
