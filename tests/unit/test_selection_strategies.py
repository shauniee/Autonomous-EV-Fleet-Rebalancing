from ev_fleet.common.settings import Settings
from ev_fleet.rebalancing.strategies import ENERGY_AWARE, NEAREST_EV
from tests.unit.test_recommendations import target, vehicle


def test_strategies_share_eligibility_but_can_select_different_evs(clock):
    near = vehicle(clock, "near", 32, 1.35)
    far = vehicle(clock, "far", 90, 1.4052)
    snapshot = {"vehicles": [near, far]}
    energy, energy_excluded = ENERGY_AWARE.rank(snapshot, target(), Settings(), clock.now())
    nearest, nearest_excluded = NEAREST_EV.rank(snapshot, target(), Settings(), clock.now())
    assert energy_excluded == nearest_excluded == []
    assert energy[0]["vehicle_id"] == "far"
    assert nearest[0]["vehicle_id"] == "near"
    assert {row["vehicle_id"]: row["eligible"] for row in energy} == {
        row["vehicle_id"]: row["eligible"] for row in nearest}


def test_strategies_apply_identical_exclusions(clock):
    online = vehicle(clock, "online", 80, 1.35)
    offline = vehicle(clock, "offline", 100, 1.283)
    offline["connectivity"] = "OFFLINE"
    snapshot = {"vehicles": [offline, online]}
    energy, energy_excluded = ENERGY_AWARE.rank(snapshot, target(), Settings(), clock.now())
    nearest, nearest_excluded = NEAREST_EV.rank(snapshot, target(), Settings(), clock.now())
    assert [row["vehicle_id"] for row in energy] == [row["vehicle_id"] for row in nearest] == ["online"]
    assert energy_excluded == nearest_excluded
    assert energy_excluded[0]["exclusion_reasons"] == ["OFFLINE"]


def test_strategy_metadata_is_explicit_and_stable():
    assert ENERGY_AWARE.policy_version == "ev-aware-v1"
    assert NEAREST_EV.policy_version == "nearest-eligible-v1"
    assert ENERGY_AWARE.name != NEAREST_EV.name

