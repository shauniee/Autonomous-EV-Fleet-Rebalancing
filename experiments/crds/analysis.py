"""Descriptive and seed-paired summaries; incomplete episodes stay visible."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from experiments.phase4.analysis import describe
from .recording import POLICIES


def analyze(paths, output):
    records = [json.loads(Path(path).read_text()) for path in paths]
    directory = Path(output)
    directory.mkdir(parents=True, exist_ok=False)
    groups = defaultdict(list)
    for record in records:
        groups[(record["case_id"], record["seed"])].append(record)
    conflicts, pairs = [], []
    for key, episodes in groups.items():
        if (len({e["policy"] for e in episodes}) != len(episodes)
                or len({(e["initial_state_hash"], e["workload_hash"], e["duration_seconds"],
                         e["transport"], tuple(sorted(e["zone_ids"]))) for e in episodes}) != 1):
            conflicts.append({"case_id": key[0], "seed": key[1], "reason": "duplicate policy or input/window/transport mismatch"})
            continue
        by_policy = {e["policy"]: e for e in episodes}
        for baseline in ("weighted", "crds-current"):
            a, b = by_policy.get("crds-delayed"), by_policy.get(baseline)
            if a and b and a["complete"] and b["complete"]:
                pairs.append({"case_id": key[0], "seed": key[1], "baseline": baseline,
                    "difference_vehicle_minutes": a["shortage_vehicle_minutes"] - b["shortage_vehicle_minutes"]})
    result = {"episode_count": len(records), "incomplete_count": sum(not r["complete"] for r in records),
        "pair_conflicts": conflicts, "paired_effects": pairs, "policies": {}, "paired_intervals": []}
    for policy in POLICIES:
        episodes = [r for r in records if r["policy"] == policy]
        values = [r["shortage_vehicle_minutes"] for r in episodes if r["complete"]]
        result["policies"][policy] = {"count": len(episodes), "complete_count": len(values),
            "shortage_vehicle_minutes": describe(values) if values else None,
            "error_count": sum(r["error_count"] for r in episodes)}
    for case in sorted({p["case_id"] for p in pairs}):
        for baseline in ("weighted", "crds-current"):
            values = [p["difference_vehicle_minutes"] for p in pairs
                      if p["case_id"] == case and p["baseline"] == baseline]
            result["paired_intervals"].append({"case_id": case, "baseline": baseline,
                "paired_count": len(values), "difference": describe(values) if values else None})
    (directory / "comparison.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("summaries", nargs="+", help="Episode summary.json paths")
    parser.add_argument("--output", required=True, help="New comparison directory")
    args = parser.parse_args()
    analyze(args.summaries, args.output)


if __name__ == "__main__":
    main()
