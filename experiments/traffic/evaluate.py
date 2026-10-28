"""Offline, reproducible comparison; inputs are synthetic, not live LTA data.

Run: python -m experiments.traffic.evaluate
Output includes both policies across fixed, identical scenario fixtures.
"""
import json
from datetime import datetime, timezone
from ev_fleet.rebalancing.traffic import assess_traffic, rank_cost

NOW = datetime(2026, 10, 28, tzinfo=timezone.utc)
A = [{"latitude": 1.3, "longitude": 103.8},
     {"latitude": 1.3, "longitude": 103.82}]
B = [{"latitude": 1.3, "longitude": 103.8},
     {"latitude": 1.305, "longitude": 103.81},
     {"latitude": 1.3, "longitude": 103.82}]
CANDIDATES = [
    {"id": "short", "geometry": A, "distance_km": 4.0, "baseline_seconds": 600, "energy_kwh": .72},
    {"id": "long", "geometry": B, "distance_km": 4.6, "baseline_seconds": 780, "energy_kwh": .83},
]


def scenarios():
    band = [{"id": "slow", "road": "FIRST ROAD", "start": A[0],
             "end": A[1], "min_kph": 8, "max_kph": 14}]
    works = [{"id": "work", "road": "FIRST ROAD",
              "start_date": "2026-10-27", "end_date": "2026-10-29"}]
    closed = [{"id": "block", "type": "ROAD BLOCK",
               "position": {"latitude": 1.3, "longitude": 103.81}, "message": "Synthetic road block"}]
    return [
        ("normal", [], [], []),
        ("congestion", band, [], []),
        ("roadworks", band, [], works),
        ("roadblock", band, closed, []),
    ]


def evaluate():
    rows = []
    for name, speeds, incidents, roadworks in scenarios():
        observations = {"source": "LTA_DATAMALL", "status": "FRESH",
                        "speeds": speeds, "incidents": incidents, "roadworks": roadworks}
        results = []
        for candidate in CANDIDATES:
            assessment = assess_traffic(candidate["geometry"],
                candidate["distance_km"], candidate["baseline_seconds"], observations, NOW)
            results.append({**candidate, "assessment": assessment,
                            "traffic_cost": rank_cost(assessment, candidate["energy_kwh"])})
        baseline = min(results, key=lambda c: c["distance_km"])
        allowed = [r for r in results if r["traffic_cost"] is not None]
        traffic_choice = min(allowed, key=lambda c: (c["traffic_cost"], c["id"])) if allowed else None
        rows.append({"scenario": name, "baseline_choice": baseline["id"],
                     "traffic_choice": traffic_choice["id"] if traffic_choice else None,
                     "baseline_blocked": baseline["assessment"]["blocked"],
                     "traffic_blocked": traffic_choice["assessment"]["blocked"] if traffic_choice else None,
                     "baseline_eta_seconds": baseline["assessment"]["estimated_duration_seconds"],
                     "traffic_eta_seconds": traffic_choice["assessment"]["estimated_duration_seconds"] if traffic_choice else None,
                     "baseline_energy_kwh": baseline["energy_kwh"],
                     "traffic_energy_kwh": traffic_choice["energy_kwh"] if traffic_choice else None})
    return rows


if __name__ == "__main__":
    print(json.dumps({"kind": "SYNTHETIC_OFFLINE_COMPARISON",
        "note": "Not research evidence or live LTA/OneMap travel outcomes",
        "results": evaluate()}, indent=2))
