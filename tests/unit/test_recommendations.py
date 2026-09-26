import pytest
from ev_fleet.common.settings import Settings
from ev_fleet.rebalancing.policy import assess, rank
from ev_fleet.rebalancing.metrics import distance_km


def vehicle(clock, name="near", soc=30, latitude=1.283):
    return {"vehicle_id": name, "state_version": 2, "connectivity": "ONLINE", "stale": False,
            "battery_capacity_kwh": 60, "consumption_kwh_per_km": .18,
            "telemetry": {"observed_at": clock.now().isoformat(), "soc_pct": soc,
                          "position": {"latitude": latitude, "longitude": 103.8513},
                          "availability": "AVAILABLE", "relocation_state": "IDLE"}}


def target():
    return {"position": {"latitude": 1.283, "longitude": 103.8513}, "charging": None}


def test_farther_ev_can_win(clock):
    near, far = vehicle(clock), vehicle(clock, "far", 90, 1.35)
    ranked, excluded = rank({"vehicles": [near, far]}, target(), Settings(), clock.now())
    assert not excluded
    assert ranked[0]["vehicle_id"] == "far"
    assert ranked[0]["distance_km"] > ranked[1]["distance_km"]


@pytest.mark.parametrize("change,reason", [("offline", "OFFLINE"), ("stale", "STALE_TELEMETRY"),
                                          ("unavailable", "UNAVAILABLE"), ("busy", "BUSY"),
                                          ("low", "BELOW_OPERATIONAL_RESERVE"), ("missing", "NO_TELEMETRY")])
def test_exclusions(clock, change, reason):
    row = vehicle(clock)
    if change == "offline": row["connectivity"] = "OFFLINE"
    if change == "stale": row["stale"] = True
    if change == "unavailable": row["telemetry"]["availability"] = "UNAVAILABLE"
    if change == "busy": row["telemetry"]["relocation_state"] = "RELOCATING"
    if change == "low": row["telemetry"]["soc_pct"] = 19
    if change == "missing": row["telemetry"] = None
    result = assess(row, target(), Settings(), clock.now())
    assert not result["eligible"] and reason in result["exclusion_reasons"]


def test_reserve_boundary_and_tie(clock):
    a, b = vehicle(clock, "a", 20), vehicle(clock, "b", 20)
    ranked, excluded = rank({"vehicles": [b, a]}, target(), Settings(), clock.now())
    assert not excluded and ranked[0]["vehicle_id"] == "a"
    assert ranked[0]["predicted_arrival_soc_pct"] == 20
    assert ranked[0]["energy_required_kwh"] == 0


def test_distance_energy_and_charging(clock):
    row = vehicle(clock, soc=80, latitude=1.35)
    config = Settings()
    destination = target()
    plain = assess(row, destination, config, clock.now())
    assert 7 < distance_km(row["telemetry"]["position"], destination["position"]) < 8
    assert plain["energy_required_kwh"] == pytest.approx(plain["distance_km"] * .18 * 1.15)
    destination["charging"] = {"available_slots": 1, "observed_at": clock.now().isoformat()}
    charged = assess(row, destination, config, clock.now())
    assert charged["score"] > plain["score"]
    assert charged["utilities"]["charging_fit"] == 1
    clock.advance(3600)
    row["telemetry"]["observed_at"] = clock.now().isoformat()
    assert assess(row, destination, config, clock.now())["charging_assessment"] == "UNKNOWN_OR_STALE"


def test_rfc3339_z_timestamps(clock):
    row = vehicle(clock)
    row["telemetry"]["observed_at"] = clock.now().isoformat().replace("+00:00", "Z")
    destination = target()
    destination["charging"] = {"available_slots": 1, "observed_at": row["telemetry"]["observed_at"]}
    result = assess(row, destination, Settings(), clock.now())
    assert result["eligible"] and result["charging_assessment"] == "AVAILABLE"
