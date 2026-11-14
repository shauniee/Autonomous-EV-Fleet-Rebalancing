"""The fast CRDS gain must equal the reference shortage model it optimises."""
import random

import pytest

from ev_fleet.contracts.crds import (
    CrdsFuture,
    CrdsMove,
    CrdsMoveOutcome,
    CrdsParameters,
    CrdsZoneTrajectory,
)
from ev_fleet.rebalancing.crds import evaluate_moves, shortage_by_zone

ZONES = ["east", "north", "west"]
FUTURES = ["f1", "f2", "f3"]


def _random_case(rng: random.Random):
    slot = rng.choice([1, 2, 5, 10])
    params = CrdsParameters(horizon_minutes=30, slot_minutes=slot,
                            priority_coverage_pct=rng.choice([0, 20, 50, 100]))
    origin = rng.choice([None, "same", "other"])
    target = rng.choice(ZONES)
    origin_zone = {None: None, "same": target,
                   "other": rng.choice([z for z in ZONES if z != target])}[origin]
    futures = [CrdsFuture(future_id=fid, zones={
        # The candidate EV sits in its origin zone in every slot, so stock there is >= 1.
        zone: CrdsZoneTrajectory(
            desired_stock=[rng.randint(0, 4) for _ in range(params.slot_count)],
            usable_stock=[rng.randint(1 if zone == origin_zone else 0, 5)
                          for _ in range(params.slot_count)])
        for zone in ZONES}) for fid in FUTURES]
    move = CrdsMove(vehicle_id="ev", facility_id="site", origin_zone_id=origin_zone,
                    target_zone_id=target, outcomes={fid: CrdsMoveOutcome(
                        arrival_minutes=rng.uniform(0, 1.1 * params.horizon_minutes),
                        availability_delay_minutes=rng.choice([0, 0, rng.uniform(0, 10)]),
                        energy_required_kwh=1) for fid in FUTURES})
    return params, futures, move


def _total(future, params, move=None):
    return sum(shortage_by_zone(future, params, move, weighted=True).values())


@pytest.mark.parametrize("seed", range(300))
def test_fast_gain_matches_reference_shortage_model(seed: int) -> None:
    params, futures, move = _random_case(random.Random(seed))

    gain = evaluate_moves(futures, [move], params).ranked_candidates[0]

    expected = {f.future_id: _total(f, params) - _total(f, params, move) for f in futures}
    assert gain.worst_case_gain_vehicle_minutes == pytest.approx(min(expected.values()), abs=1e-9)
    assert expected[gain.worst_case_future_id] == pytest.approx(
        gain.worst_case_gain_vehicle_minutes, abs=1e-9)
    # Evidence reports the unweighted per-zone change in that worst future.
    worst = next(f for f in futures if f.future_id == gain.worst_case_future_id)
    before = shortage_by_zone(worst, params)
    after = shortage_by_zone(worst, params, move)
    assert gain.shortage_changes_by_zone == pytest.approx(
        {zone: after[zone] - before[zone] for zone in ZONES}, abs=1e-9)


@pytest.mark.parametrize(("arrival_minutes", "gain", "decision"), [
    (29, 1, "HOLD"),
    (28, 2, "MOVE"),
    (28.5, 1, "HOLD"),  # a part-slot arrival only counts from the next whole slot
])
def test_gain_at_threshold_holds_and_above_it_moves(
        arrival_minutes: float, gain: int, decision: str) -> None:
    # One car short in every 1-minute slot: arriving at minute 29 relieves exactly 1 vehicle-minute.
    params = CrdsParameters(horizon_minutes=30, slot_minutes=1, minimum_gain_vehicle_minutes=1)
    future = CrdsFuture(future_id="f", zones={"east": CrdsZoneTrajectory(
        desired_stock=[1] * 30, usable_stock=[0] * 30)})
    move = CrdsMove(vehicle_id="ev", facility_id="site", origin_zone_id=None, target_zone_id="east",
                    outcomes={"f": CrdsMoveOutcome(arrival_minutes=arrival_minutes,
                                                   energy_required_kwh=1)})

    result = evaluate_moves([future], [move], params)

    assert result.decision == decision
    assert result.ranked_candidates[0].worst_case_gain_vehicle_minutes == gain
