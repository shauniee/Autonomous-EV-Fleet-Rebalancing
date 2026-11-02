import argparse
import asyncio
import csv
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
from .analysis import analyze
from .cluster import execute_trial
from .config import EvaluationConfig
from .scenarios import SCENARIO_NAMES, generate_scenario
from ev_fleet.rebalancing.strategies import ENERGY_AWARE, NEAREST_EV


def json_default(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError(type(value).__name__)


def flatten(row):
    return {key: json.dumps(value, sort_keys=True) if isinstance(value, (dict, list)) else value
            for key, value in row.items()}


def repository_state():
    def git(*arguments):
        result = subprocess.run(["git", *arguments], text=True, capture_output=True, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    digest = sha256()
    sources = [*sorted(Path("experiments/strategy_evaluation").glob("*.py")),
               Path("src/ev_fleet/rebalancing/policy.py"),
               Path("src/ev_fleet/rebalancing/strategies.py"),
               Path("src/ev_fleet/rebalancing/app.py")]
    for path in sources:
        digest.update(path.as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain")),
            "evaluation_source_sha256": digest.hexdigest(),
            "evaluation_sources": [path.as_posix() for path in sources]}


async def run(config, scenarios, output):
    output.mkdir(parents=True, exist_ok=False)
    raw = output / "raw"
    raw.mkdir()
    started = datetime.now(timezone.utc)
    manifest = {
        "evaluation_version": "1", "research_question": "Does energy-aware selection reduce operationally poor relocations while maintaining reasonable distance and response time?",
        "started_at": started.isoformat(), "config": config.as_dict(),
        "scenarios": list(scenarios), "algorithms": [ENERGY_AWARE.name, NEAREST_EV.name],
        "planned_scenario_trials": len(scenarios) * config.repetitions * 2,
        "repository": repository_state(),
        "environment": {"python": sys.version, "platform": platform.platform()},
        "weights_frozen": {"distance": .35, "current_soc": .20,
                           "arrival_reserve": .35, "charging_fit": .10},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    records = []
    jsonl = raw / "trials.jsonl"
    with jsonl.open("w", encoding="utf-8") as handle:
        for seed in range(config.seed_start, config.seed_start + config.repetitions):
            for scenario_name in scenarios:
                scenario = generate_scenario(scenario_name, seed)
                strategies = (ENERGY_AWARE, NEAREST_EV) if seed % 2 == 0 else (NEAREST_EV, ENERGY_AWARE)
                for strategy in strategies:
                    try:
                        record = await execute_trial(scenario, strategy, config)
                    except Exception as exc:  # Preserve the failed trial; completion will be marked incomplete.
                        record = {"trial_id": f"{scenario.name}-{scenario.seed}-{strategy.name}",
                            "scenario": scenario.name, "seed": scenario.seed,
                            "algorithm": strategy.name, "policy_version": strategy.policy_version,
                            "initial_state_hash": scenario.state_hash(),
                            "infrastructure_error": f"{type(exc).__name__}: {exc}"}
                    records.append(record)
                    handle.write(json.dumps(record, default=json_default, sort_keys=True) + "\n")
                    handle.flush()
                    print(f"[{len(records)}/{manifest['planned_scenario_trials']}] {record['trial_id']} "
                          f"{'ERROR' if record.get('infrastructure_error') else record.get('final_dispatch_state')}", flush=True)
    fields = sorted({key for row in records for key in row})
    with (raw / "trials.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fields)
        writer.writeheader()
        writer.writerows(flatten({field: row.get(field) for field in fields}) for row in records)

    analysis = analyze(jsonl, output, config.bootstrap_samples, config.analysis_seed)
    pair_hashes = {}
    for row in records:
        pair_hashes.setdefault((row["scenario"], row["seed"]), set()).add(row["initial_state_hash"])
    completion = {
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "planned_scenario_trials": manifest["planned_scenario_trials"],
        "recorded_scenario_trials": len(records),
        "paired_start_state_mismatches": sum(len(values) != 1 for values in pair_hashes.values()),
        "infrastructure_errors": analysis["infrastructure_errors"],
        "complete": (len(records) == manifest["planned_scenario_trials"] and
                     analysis["infrastructure_errors"] == 0 and
                     all(len(values) == 1 for values in pair_hashes.values())),
    }
    (output / "completion.json").write_text(json.dumps(completion, indent=2), encoding="utf-8")
    return completion


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the paired EV-selection strategy evaluation")
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--seed-start", type=int, default=0)
    parser.add_argument("--scenarios", nargs="*", choices=SCENARIO_NAMES, default=list(SCENARIO_NAMES))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be positive")
    output = args.output or Path("results/strategy-evaluation") / datetime.now().strftime("run-%Y%m%d-%H%M%S")
    config = EvaluationConfig(repetitions=args.repetitions, seed_start=args.seed_start)
    completion = asyncio.run(run(config, args.scenarios, output))
    print(json.dumps({"output": str(output), **completion}, indent=2))
    return 0 if completion["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
