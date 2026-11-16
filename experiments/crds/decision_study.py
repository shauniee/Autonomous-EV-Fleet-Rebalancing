"""Synthetic in-process CRDS decision study (no services, no network, no credentials).

Random six-zone fleet states are scored with the production `build_futures` and
`evaluate_moves`. Every policy sees the same eligible candidates; we then ask what
each policy's chosen move does to fleet-wide weighted shortage in the declared futures.
Usage: python -m experiments.crds.decision_study --trials 2000 --out results/crds-decision-study
"""
import argparse
import json
import math
import random
import statistics
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from ev_fleet.common.settings import CrdsSettings
from ev_fleet.contracts.crds import CrdsFuture, CrdsMove, CrdsMoveOutcome, CrdsZoneTrajectory
from ev_fleet.demand.store import SINGAPORE_ZONES
from ev_fleet.rebalancing.crds import evaluate_moves
from ev_fleet.rebalancing.crds_futures import build_futures, parameters_for

CAPACITY_KWH, CONSUMPTION, SPEED_KPH, CHARGER_KW = 60, 0.18, 40, 50
ROUTE_FACTOR, MARGIN, RESERVE = 1.3, 1.15, 20
ZONES = {key: (lat, lon) for key, _, lat, lon in SINGAPORE_ZONES}


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def random_state(rng, now, fleet_size=20):
    while True:
        vehicles = []
        for i in range(fleet_size):
            zone = rng.choice(list(ZONES))
            lat, lon = ZONES[zone]
            rented = rng.random() < 0.15
            vehicles.append(SimpleNamespace(
                vehicle_id=f"ev-{i:02d}", zone=zone, rented=rented,
                position=(lat + rng.uniform(-.01, .01), lon + rng.uniform(-.01, .01)),
                telemetry=SimpleNamespace(soc_pct=rng.uniform(22, 95)),
                consumption_kwh_per_km=CONSUMPTION, battery_capacity_kwh=CAPACITY_KWH,
                active_rental=SimpleNamespace(expected_return_zone_id=rng.choice(list(ZONES)),
                                              expected_end_at=now + timedelta(minutes=rng.uniform(2, 25)))
                if rented else None))
        available = {z: sum(v.zone == z and not v.rented for v in vehicles) for z in ZONES}
        desired = {z: rng.randint(0, 5) for z in ZONES}
        short = {z: desired[z] - available[z] for z in ZONES}
        if max(short.values()) > 0:
            top = max(short.values())
            target = rng.choice([z for z in ZONES if short[z] == top])
            return vehicles, available, desired, target


def candidates(vehicles, available, desired, target):
    rows = []
    for v in vehicles:
        if v.rented or v.zone == target or (desired[v.zone] > 0 and available[v.zone] < 2):
            continue  # common eligibility: free, outside target, donor keeps one EV if it has demand
        d = ROUTE_FACTOR * haversine(v.position, ZONES[target])
        arrival = v.telemetry.soc_pct - 100 * MARGIN * d * CONSUMPTION / CAPACITY_KWH
        if arrival >= RESERVE:
            rows.append((v, d, arrival))
    return rows


def sample_future(rng, future_id, vehicles, available, desired, params):
    """One realised future from a stochastic model CRDS never sees.

    Unlike the declared profiles (fixed +-1 shift, +10 min returns, +5 min trips), returns are
    early or late by a random amount, can go missing or land in another zone, and every zone's
    demand moves independently and can step mid-horizon.
    """
    slots = params.slot_count
    stock = {z: [available[z]] * slots for z in ZONES}
    for v in vehicles:
        rental = v.active_rental
        if rental is None or v.telemetry.soc_pct < 30 or rng.random() < 0.10:
            continue  # same usability rule as production; 10% not back within the horizon
        zone = rental.expected_return_zone_id if rng.random() >= 0.20 else rng.choice(list(ZONES))
        minutes = max(params.slot_minutes, (rental.expected_end_at - SAMPLE_NOW).total_seconds() / 60
                      + rng.uniform(-5, 20))
        for slot in range(min(slots, math.ceil(minutes / params.slot_minutes)), slots):
            stock[zone][slot] += 1
    zones = {}
    for z in ZONES:
        base = max(0, desired[z] + rng.choices((-2, -1, 0, 1, 2), (.05, .2, .5, .2, .05))[0])
        path = [base] * slots
        if rng.random() < 0.3:  # demand steps by one car part-way through the horizon
            step, delta = rng.randrange(slots), rng.choice((-1, 1))
            path = path[:step] + [max(0, base + delta)] * (slots - step)
        zones[z] = CrdsZoneTrajectory(desired_stock=path, usable_stock=stock[z])
    return CrdsFuture(future_id=future_id, zones=zones)


def out_of_sample(rng, samples, vehicles, available, desired, moves, choice, params):
    """Realised gain of each policy's choice in each sampled future (HOLD counts as 0)."""
    gains = {p: [] for p in choice}
    for i in range(samples):
        future = sample_future(rng, f"s{i}", vehicles, available, desired, params)
        factor = rng.lognormvariate(0, 0.3)  # travel and charging time noise
        sampled = [m.model_copy(update={"outcomes": {future.future_id: CrdsMoveOutcome(
            arrival_minutes=m.outcomes["nominal"].arrival_minutes * factor,
            availability_delay_minutes=m.outcomes["nominal"].availability_delay_minutes * factor,
            energy_required_kwh=m.outcomes["nominal"].energy_required_kwh)}}) for m in moves]
        realised = {c.vehicle_id: c.worst_case_gain_vehicle_minutes
                    for c in evaluate_moves([future], sampled, params).ranked_candidates}
        for p, vid in choice.items():
            gains[p].append(0.0 if vid is None else realised[vid])
    worst = max(1, samples // 10)
    return {p: {"mean": statistics.fmean(g), "harm": sum(x < 0 for x in g) / samples,
                "cvar10": statistics.fmean(sorted(g)[:worst])} for p, g in gains.items()}


SAMPLE_NOW = datetime(2026, 11, 14, tzinfo=timezone.utc)


def run(trials, seed, samples=0):
    settings = CrdsSettings()
    params = parameters_for(settings)
    now = SAMPLE_NOW
    rng = random.Random(seed)
    sample_rng = random.Random(seed + 7)  # separate stream keeps the declared-futures results unchanged
    records = []
    for trial in range(trials):
        vehicles, available, desired, target = random_state(rng, now)
        rows = candidates(vehicles, available, desired, target)
        if not rows:
            records.append({"trial": trial, "candidates": 0})
            continue
        inputs = SimpleNamespace(
            issues=[], evaluated_at=now, traffic={}, available_by_zone=available,
            demand=SimpleNamespace(fixed_demand_zone_ids=list(ZONES), zones=[SimpleNamespace(
                zone_id=z, demand=SimpleNamespace(desired_available_vehicles=desired[z], observed_at=now))
                for z in ZONES]),
            fleet=SimpleNamespace(vehicles=vehicles), planning=SimpleNamespace(dispatches=[]),
            facilities=SimpleNamespace(facilities=[]))
        futures = build_futures(inputs, target, settings)
        moves, info = [], {}
        for v, d, arrival in rows:
            minutes = d / SPEED_KPH * 60 / settings.simulation_time_scale
            charge = ((80 - arrival) / 100 * CAPACITY_KWH / CHARGER_KW * 60 / settings.simulation_time_scale
                      if arrival < 35 else 0)
            moves.append(CrdsMove(vehicle_id=v.vehicle_id, facility_id=f"site-{target}",
                                  origin_zone_id=v.zone, target_zone_id=target, outcomes={
                f.future_id: CrdsMoveOutcome(
                    arrival_minutes=minutes + (settings.journey_delay_minutes
                                               if f.future_id in ("combined", "delayed_inbound") else 0),
                    availability_delay_minutes=charge, energy_required_kwh=MARGIN * d * CONSUMPTION)
                for f in futures}))
            info[v.vehicle_id] = (d, arrival, v.telemetry.soc_pct)
        robust = evaluate_moves(futures, moves, params)
        nominal = evaluate_moves([f for f in futures if f.future_id == "nominal"],
                                 [m.model_copy(update={"outcomes": {"nominal": m.outcomes["nominal"]}})
                                  for m in moves], params)
        gain = {c.vehicle_id: c for c in robust.ranked_candidates}
        nominal_gain = {c.vehicle_id: c.worst_case_gain_vehicle_minutes for c in nominal.ranked_candidates}
        nearest = min(info, key=lambda k: (info[k][0], k))
        weighted = max(info, key=lambda k: (
            .35 * (1 - min(info[k][0] / 50, 1)) + .20 * info[k][2] / 100
            + .35 * (info[k][1] - 20) / 80 + .10 * info[k][1] / 100, k))
        choice = {"nearest": nearest, "weighted": weighted,
                  "crds_nominal": nominal.selected_vehicle_id, "crds": robust.selected_vehicle_id}

        def outcome(vehicle_id):
            if vehicle_id is None:
                return {"move": False, "worst_gain": 0.0, "nominal_gain": 0.0, "donor_harm": 0.0, "km": 0.0}
            c = gain[vehicle_id]
            origin = next(m.origin_zone_id for m in moves if m.vehicle_id == vehicle_id)
            return {"move": True, "worst_gain": c.worst_case_gain_vehicle_minutes,
                    "nominal_gain": nominal_gain[vehicle_id],
                    "donor_harm": max(0.0, c.shortage_changes_by_zone[origin]), "km": info[vehicle_id][0]}

        record = {"trial": trial, "candidates": len(rows), "futures": len(futures),
                  "crds_decision": robust.decision,
                  **{f"{p}_{k}": val for p, vid in choice.items() for k, val in outcome(vid).items()}}
        if samples:
            oos = out_of_sample(sample_rng, samples, vehicles, available, desired, moves, choice, params)
            record.update({f"{p}_oos_{k}": val for p, row in oos.items() for k, val in row.items()})
        records.append(record)
    return records


def bootstrap(diffs, rng, n=2000):
    means = sorted(statistics.fmean(rng.choices(diffs, k=len(diffs))) for _ in range(n))
    return means[int(.025 * n)], means[int(.975 * n)]


def latency(seed, reps=30):
    rng = random.Random(seed)
    params = parameters_for(CrdsSettings())
    out = []
    for future_count in (6, 9):
        for n in (5, 10, 20, 40, 80, 160):
            zones = list(ZONES)
            futures = [CrdsFuture(future_id=f"f{i}", zones={z: CrdsZoneTrajectory(
                desired_stock=[rng.randint(0, 5)] * params.slot_count,
                usable_stock=[rng.randint(2, 6)] * params.slot_count) for z in zones})
                for i in range(future_count)]
            moves = [CrdsMove(vehicle_id=f"ev-{i}", facility_id="site", origin_zone_id=rng.choice(zones[1:]),
                              target_zone_id=zones[0], outcomes={f.future_id: CrdsMoveOutcome(
                                  arrival_minutes=rng.uniform(0, 10), energy_required_kwh=1) for f in futures})
                     for i in range(n)]
            samples = []
            for _ in range(reps):
                start = time.perf_counter()
                evaluate_moves(futures, moves, params)
                samples.append((time.perf_counter() - start) * 1000)
            samples.sort()
            out.append({"futures": future_count, "candidates": n,
                        "p50_ms": samples[len(samples) // 2], "p95_ms": samples[int(.95 * len(samples)) - 1]})
    return out


def summarise(records, seed):
    rng = random.Random(seed + 1)
    rows = [r for r in records if r["candidates"]]
    policies = ("nearest", "weighted", "crds_nominal", "crds")
    summary = {"trials": len(records), "with_candidates": len(rows),
               "crds_hold_share": sum(r["crds_decision"] == "HOLD" for r in rows) / len(rows), "policies": {}}
    for p in policies:
        moved = [r for r in rows if r[f"{p}_move"]]
        summary["policies"][p] = {
            "move_share": len(moved) / len(rows),
            "mean_worst_gain": statistics.fmean(r[f"{p}_worst_gain"] for r in rows),
            "harmful_share": sum(r[f"{p}_worst_gain"] < 0 for r in rows) / len(rows),
            "not_useful_share": sum(r[f"{p}_move"] and r[f"{p}_worst_gain"] <= 1 for r in rows) / len(rows),
            "donor_harm_share": sum(r[f"{p}_donor_harm"] > 0 for r in rows) / len(rows),
            "mean_km_when_moving": statistics.fmean(r[f"{p}_km"] for r in moved) if moved else 0.0}
    for base in ("nearest", "weighted", "crds_nominal"):
        diffs = [r["crds_worst_gain"] - r[f"{base}_worst_gain"] for r in rows]
        summary["policies"][base]["crds_minus_policy_worst_gain"] = statistics.fmean(diffs)
        summary["policies"][base]["crds_minus_policy_ci95"] = bootstrap(diffs, rng)
    if "crds_oos_mean" in rows[0]:
        oos = summary["out_of_sample"] = {}
        for p in policies:
            oos[p] = {"mean_gain": statistics.fmean(r[f"{p}_oos_mean"] for r in rows),
                      "harm_share": statistics.fmean(r[f"{p}_oos_harm"] for r in rows),
                      "mean_cvar10": statistics.fmean(r[f"{p}_oos_cvar10"] for r in rows),
                      "states_expected_harm": sum(r[f"{p}_oos_mean"] < 0 for r in rows) / len(rows)}
        for base in ("nearest", "weighted", "crds_nominal"):
            for key in ("mean", "cvar10"):
                diffs = [r[f"crds_oos_{key}"] - r[f"{base}_oos_{key}"] for r in rows]
                oos[base][f"crds_minus_policy_{key}"] = statistics.fmean(diffs)
                oos[base][f"crds_minus_policy_{key}_ci95"] = bootstrap(diffs, rng)
    return summary


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=2000)
    parser.add_argument("--seed", type=int, default=20261114)
    parser.add_argument("--samples", type=int, default=50,
                        help="out-of-sample futures per state (0 disables the check)")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    records = run(args.trials, args.seed, args.samples)
    summary = summarise(records, args.seed)
    summary["latency"] = latency(args.seed)
    summary["runtime_seconds"] = time.perf_counter() - started
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--", "src"], capture_output=True, text=True).stdout)
    summary["manifest"] = {"commit": commit, "src_dirty": dirty, "seed": args.seed, "trials": args.trials,
                           "samples": args.samples}
    (args.out / "trials.jsonl").write_text("".join(json.dumps(r) + "\n" for r in records))
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
