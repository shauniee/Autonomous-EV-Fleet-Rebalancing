"""Delayed-state study: each policy decides on a snapshot that is `lag` seconds old.

While the snapshot ages, the true fleet drifts: zone demand random-walks, idle EVs get rented and
rentals that end return to their zone. The chosen move is then executed on the drifted state and
replayed against out-of-sample futures sampled from it (the same model as decision_study).
Policies: nearest EV, CRDS without age futures, production CRDS (age futures from the demand
timestamp) and CRDS whose move is rechecked against the fresh state before execution, as Dispatch
does at approval. A move whose EV was rented meanwhile fails its Fleet claim and counts as no move.
Usage: python -m experiments.crds.staleness_study --trials 1000 --out results/crds-staleness-study
"""
import argparse
import json
import math
import random
import statistics
import subprocess
import time
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from ev_fleet.common.settings import CrdsSettings
from ev_fleet.contracts.crds import CrdsMove, CrdsMoveOutcome
from ev_fleet.rebalancing.crds import evaluate_moves
from ev_fleet.rebalancing.crds_futures import build_futures, parameters_for

from .decision_study import (CAPACITY_KWH, CHARGER_KW, CONSUMPTION, MARGIN, SAMPLE_NOW, SPEED_KPH, ZONES,
                             bootstrap, candidates, out_of_sample, random_state)

LAGS = (0, 30, 60, 90, 120)  # seconds; 120 s is the recommendation TTL
DEMAND_CHANGE_MINUTES = 10   # mean simulated minutes between +-1 demand changes in a zone
RENTAL_MINUTES = 120         # mean simulated minutes until an idle EV is rented
POLICIES = ("nearest", "crds_no_age", "crds", "crds_recheck")


def poisson(rng, mean):
    limit, k, p = math.exp(-mean), 0, rng.random()
    while p > limit:
        k += 1
        p *= rng.random()
    return k


def drift(rng, vehicles, desired, minutes):
    """True state `minutes` of simulated time after the snapshot."""
    truth = {z: max(0, d + sum(rng.choice((-1, 1)) for _ in range(poisson(rng, minutes / DEMAND_CHANGE_MINUTES))))
             for z, d in desired.items()}
    moved = []
    for v in vehicles:
        rental = v.active_rental
        if rental is not None:
            end = rental.expected_end_at - timedelta(minutes=minutes)
            if end <= SAMPLE_NOW:  # returned: idle in its return zone
                lat, lon = ZONES[rental.expected_return_zone_id]
                v = SimpleNamespace(**{**vars(v), "zone": rental.expected_return_zone_id, "rented": False,
                                       "position": (lat, lon), "active_rental": None})
            else:
                v = SimpleNamespace(**{**vars(v), "active_rental": SimpleNamespace(
                    expected_return_zone_id=rental.expected_return_zone_id, expected_end_at=end)})
        elif rng.random() < 1 - math.exp(-minutes / RENTAL_MINUTES):
            v = SimpleNamespace(**{**vars(v), "rented": True, "active_rental": SimpleNamespace(
                expected_return_zone_id=rng.choice(list(ZONES)),
                expected_end_at=SAMPLE_NOW + timedelta(minutes=rng.uniform(2, 25)))})
        moved.append(v)
    available = {z: sum(v.zone == z and not v.rented for v in moved) for z in ZONES}
    return moved, available, truth


def planning_inputs(vehicles, available, desired, observed_at):
    return SimpleNamespace(
        issues=[], evaluated_at=SAMPLE_NOW, traffic={}, available_by_zone=available,
        demand=SimpleNamespace(fixed_demand_zone_ids=[], zones=[SimpleNamespace(
            zone_id=z, demand=SimpleNamespace(desired_available_vehicles=desired[z], observed_at=observed_at))
            for z in ZONES]),
        fleet=SimpleNamespace(vehicles=vehicles), planning=SimpleNamespace(dispatches=[]),
        facilities=SimpleNamespace(facilities=[]))


def build_moves(rows, futures, target, settings):
    moves = []
    for v, d, arrival in rows:
        minutes = d / SPEED_KPH * 60 / settings.simulation_time_scale
        charge = ((80 - arrival) / 100 * CAPACITY_KWH / CHARGER_KW * 60 / settings.simulation_time_scale
                  if arrival < 35 else 0)
        moves.append(CrdsMove(vehicle_id=v.vehicle_id, facility_id=f"site-{target}", origin_zone_id=v.zone,
                              target_zone_id=target, outcomes={f.future_id: CrdsMoveOutcome(
                                  arrival_minutes=minutes + (settings.journey_delay_minutes
                                                             if f.future_id in ("combined", "delayed_inbound") else 0),
                                  availability_delay_minutes=charge, energy_required_kwh=MARGIN * d * CONSUMPTION)
                                  for f in futures}))
    return moves


def decide(rows, inputs, target, settings, params, expand_age):
    futures = build_futures(inputs, target, settings, expand_age=expand_age)
    return evaluate_moves(futures, build_moves(rows, futures, target, settings), params), len(futures)


def run(trials, seed, samples):
    settings = CrdsSettings()
    params = parameters_for(settings)
    rng, drift_rng, sample_rng = random.Random(seed), random.Random(seed + 3), random.Random(seed + 7)
    records = []
    for trial in range(trials):
        vehicles, available, desired, target = random_state(rng, SAMPLE_NOW)
        rows = candidates(vehicles, available, desired, target)
        if not rows:
            continue
        for lag in LAGS:
            minutes = lag * settings.simulation_time_scale / 60
            snapshot = planning_inputs(vehicles, available, desired, SAMPLE_NOW - timedelta(seconds=lag))
            aged, future_count = decide(rows, snapshot, target, settings, params, expand_age=True)
            no_age, _ = decide(rows, snapshot, target, settings, params, expand_age=False)
            nearest = min(rows, key=lambda r: (r[1], r[0].vehicle_id))[0].vehicle_id
            truth_vehicles, truth_available, truth_desired = drift(drift_rng, vehicles, desired, minutes)
            rented = {v.vehicle_id for v in truth_vehicles if v.rented}
            recheck = None
            if aged.selected_vehicle_id and aged.selected_vehicle_id not in rented:
                row = next(r for r in rows if r[0].vehicle_id == aged.selected_vehicle_id)
                fresh = planning_inputs(truth_vehicles, truth_available, truth_desired, SAMPLE_NOW)
                again, _ = decide([row], fresh, target, settings, params, expand_age=True)
                recheck = again.selected_vehicle_id
            chosen = {"nearest": nearest, "crds_no_age": no_age.selected_vehicle_id,
                      "crds": aged.selected_vehicle_id, "crds_recheck": recheck}
            executed = {p: None if vid in rented else vid for p, vid in chosen.items()}
            live = [r for r in rows if r[0].vehicle_id not in rented]  # rented EVs would leave origin stock at 0
            moves = build_moves(live, [SimpleNamespace(future_id="nominal")], target, settings)
            oos = out_of_sample(sample_rng, samples, truth_vehicles, truth_available, truth_desired,
                                moves, executed, params)
            records.append({"trial": trial, "lag_s": lag, "futures": future_count,
                            **{f"{p}_chosen": chosen[p] is not None for p in POLICIES},
                            **{f"{p}_claim_failed": chosen[p] is not None and executed[p] is None
                               for p in POLICIES},
                            "recheck_cancelled": aged.selected_vehicle_id is not None
                            and aged.selected_vehicle_id not in rented and recheck is None,
                            **{f"{p}_{k}": val for p, row in oos.items() for k, val in row.items()}})
    return records


def summarise(records, seed):
    rng = random.Random(seed + 1)
    out = {}
    for lag in LAGS:
        rows = [r for r in records if r["lag_s"] == lag]
        crds_moves = sum(r["crds_chosen"] and not r["crds_claim_failed"] for r in rows)
        block = {"states": len(rows), "futures_per_state": statistics.fmean(r["futures"] for r in rows),
                 "recheck_cancelled_share_of_crds_moves": sum(r["recheck_cancelled"] for r in rows) / max(1, crds_moves),
                 "policies": {}}
        for p in POLICIES:
            block["policies"][p] = {
                "move_share": sum(r[f"{p}_chosen"] and not r[f"{p}_claim_failed"] for r in rows) / len(rows),
                "claim_failed_share": sum(r[f"{p}_claim_failed"] for r in rows) / len(rows),
                "harm_share": statistics.fmean(r[f"{p}_harm"] for r in rows),
                "mean_gain": statistics.fmean(r[f"{p}_mean"] for r in rows),
                "mean_cvar10": statistics.fmean(r[f"{p}_cvar10"] for r in rows)}
        for a, b in (("crds", "crds_no_age"), ("crds_recheck", "crds"), ("crds_recheck", "nearest")):
            for key in ("harm", "cvar10"):
                diffs = [r[f"{a}_{key}"] - r[f"{b}_{key}"] for r in rows]
                block[f"{a}_minus_{b}_{key}"] = statistics.fmean(diffs)
                block[f"{a}_minus_{b}_{key}_ci95"] = bootstrap(diffs, rng)
        out[str(lag)] = block
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=1000)
    parser.add_argument("--seed", type=int, default=20261120)
    parser.add_argument("--samples", type=int, default=50)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    records = run(args.trials, args.seed, args.samples)
    summary = {"lags": summarise(records, args.seed), "runtime_seconds": time.perf_counter() - started,
               "model": {"time_scale": CrdsSettings().simulation_time_scale,
                         "demand_change_minutes": DEMAND_CHANGE_MINUTES, "rental_minutes": RENTAL_MINUTES}}
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    summary["manifest"] = {"commit": commit, "seed": args.seed, "trials": args.trials, "samples": args.samples}
    (args.out / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
