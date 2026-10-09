"""Analysis reads raw JSONL only; it never reaches back into live services."""
from collections import defaultdict
import csv
import json
import math
import random
import statistics
from pathlib import Path


CONTINUOUS = (
    "actual_relocation_distance_km", "initial_soc_pct", "predicted_arrival_soc_pct",
    "actual_arrival_soc_pct", "arrival_soc_error_pct", "relocation_completion_time_s",
    "terminal_time_s", "recommendation_latency_ms", "recommendation_request_latency_ms",
    "current_shortage_before", "current_shortage_after", "shortage_change",
)
RATES = ("successful_relocation_rate", "operationally_poor_outcome",
         "low_initial_soc", "predicted_low_arrival_soc", "actual_low_arrival_soc")


def percentile(values, quantile):
    ordered = sorted(values)
    if not ordered:
        return None
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def bootstrap_mean_ci(values, samples=2000, seed=4242):
    if not values:
        return None, None
    rng = random.Random(seed)
    means = [statistics.fmean(rng.choice(values) for _ in values) for _ in range(samples)]
    return percentile(means, .025), percentile(means, .975)


def wilson(successes, total, z=1.959963984540054):
    if not total:
        return None, None
    p = successes / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0, centre - margin), min(1, centre + margin)


def describe(values, bootstrap_samples=2000, seed=4242):
    values = [float(value) for value in values if value is not None]
    low, high = bootstrap_mean_ci(values, bootstrap_samples, seed)
    return {"n": len(values), "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "stddev": statistics.stdev(values) if len(values) > 1 else 0.0 if values else None,
            "p95": percentile(values, .95), "ci95_low": low, "ci95_high": high}


def load_records(path):
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_csv(path, rows):
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = list(rows[0]) + sorted({key for row in rows for key in row} - set(rows[0]))
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _bar_chart(path, title, rows, value_key, value_label):
    valid = [row for row in rows if row.get(value_key) is not None]
    bar_width = 54
    gap = 12
    width, height = max(900, 110 + len(valid) * (bar_width + gap)), 430
    values = [float(row[value_key]) for row in valid]
    minimum, maximum = min([0.0, *values]), max([0.0, *values])
    span = maximum - minimum or 1
    baseline = 350 - (0 - minimum) / span * 255
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
                '<rect width="100%" height="100%" fill="#f7faf8"/>',
                f'<text x="35" y="38" font-family="Arial" font-size="22" fill="#173f43">{title}</text>',
                f'<line x1="55" y1="{baseline:.2f}" x2="{width - 30}" y2="{baseline:.2f}" stroke="#78908d"/>']
    for index, row in enumerate(valid):
        x = 70 + index * (bar_width + gap)
        value_y = 350 - (float(row[value_key]) - minimum) / span * 255
        y = min(value_y, baseline)
        bar_height = max(1, abs(value_y - baseline))
        colour = "#1e7b6d" if row["algorithm"] == "energy_aware" else "#557b9b"
        label = f"{row['scenario'][:11]}\n{row['algorithm'].replace('_', ' ')}"
        elements.extend([
            f'<rect x="{x}" y="{y:.2f}" width="{bar_width}" height="{bar_height:.2f}" fill="{colour}" rx="3"/>',
            f'<text x="{x + bar_width / 2:.2f}" y="{y - 6:.2f}" text-anchor="middle" font-family="Arial" font-size="10">{float(row[value_key]):.2f}</text>',
            f'<text x="{x + bar_width / 2:.2f}" y="370" text-anchor="middle" font-family="Arial" font-size="8">{label.splitlines()[0]}</text>',
            f'<text x="{x + bar_width / 2:.2f}" y="383" text-anchor="middle" font-family="Arial" font-size="8">{label.splitlines()[1]}</text>',
        ])
    elements.extend([f'<text x="20" y="210" transform="rotate(-90 20 210)" font-family="Arial" font-size="12">{value_label}</text>',
                     '<text x="70" y="414" font-family="Arial" font-size="10" fill="#557">Green: energy-aware · Blue: nearest EV</text>', '</svg>'])
    path.write_text("\n".join(elements), encoding="utf-8")


def analyze(raw_path, output_dir, bootstrap_samples=2000, analysis_seed=4242):
    output = Path(output_dir)
    records = load_records(raw_path)
    usable = [row for row in records if not row.get("infrastructure_error")]
    grouped = defaultdict(list)
    for row in usable:
        grouped[(row["scenario"], row["algorithm"])].append(row)

    summaries = []
    for (scenario, algorithm), rows in sorted(grouped.items()):
        for metric in CONTINUOUS:
            summary = describe([row.get(metric) for row in rows], bootstrap_samples,
                               analysis_seed + sum(map(ord, scenario + algorithm + metric)))
            summaries.append({"scenario": scenario, "algorithm": algorithm,
                              "metric": metric, **summary})
        for metric in RATES:
            values = [row.get(metric) for row in rows if row.get(metric) is not None]
            if metric == "successful_relocation_rate":
                successes = sum(row["successful_relocations"] for row in rows)
                denominator = sum(row["request_count"] for row in rows)
            else:
                successes = sum(bool(value) for value in values)
                denominator = len(values)
            low, high = wilson(successes, denominator)
            summaries.append({"scenario": scenario, "algorithm": algorithm, "metric": metric,
                "n": len(values), "count": successes, "denominator": denominator,
                "mean": successes / denominator if denominator else None,
                "median": statistics.median(values) if values else None,
                "stddev": statistics.stdev(values) if len(values) > 1 else 0.0 if values else None,
                "p95": percentile(values, .95), "ci95_low": low, "ci95_high": high})
    write_csv(output / "tables" / "scenario-summary.csv", summaries)

    paired = []
    pairs = defaultdict(dict)
    for row in usable:
        pairs[(row["scenario"], row["seed"])][row["algorithm"]] = row
    for scenario in sorted({row["scenario"] for row in usable}):
        scenario_pairs = [value for (name, _), value in pairs.items() if name == scenario and
                          {"energy_aware", "nearest_ev"} <= set(value)]
        for metric in CONTINUOUS + RATES:
            differences = []
            for pair in scenario_pairs:
                energy, nearest = pair["energy_aware"].get(metric), pair["nearest_ev"].get(metric)
                if energy is not None and nearest is not None:
                    differences.append(float(energy) - float(nearest))
            summary = describe(differences, bootstrap_samples,
                               analysis_seed + sum(map(ord, scenario + metric)))
            paired.append({"scenario": scenario, "metric": metric,
                           "direction": "energy_aware_minus_nearest", **summary})
    complete_pairs = [value for value in pairs.values()
                      if {"energy_aware", "nearest_ev"} <= set(value)]
    for metric in CONTINUOUS + RATES:
        differences = []
        for pair in complete_pairs:
            energy, nearest = pair["energy_aware"].get(metric), pair["nearest_ev"].get(metric)
            if energy is not None and nearest is not None:
                differences.append(float(energy) - float(nearest))
        summary = describe(differences, bootstrap_samples, analysis_seed + sum(map(ord, metric)))
        paired.append({"scenario": "ALL_EQUAL_WEIGHT", "metric": metric,
                       "direction": "energy_aware_minus_nearest", **summary})
    write_csv(output / "tables" / "paired-effects.csv", paired)

    failures = []
    for (scenario, algorithm), rows in sorted(grouped.items()):
        reasons = defaultdict(int)
        for row in rows:
            if row.get("failed_or_aborted_relocations"):
                reasons[row.get("failure_reason") or "REQUEST_REJECTED"] += row["failed_or_aborted_relocations"]
        failures.extend({"scenario": scenario, "algorithm": algorithm, "reason": reason, "count": count}
                        for reason, count in sorted(reasons.items()))
    write_csv(output / "tables" / "failure-summary.csv", failures)

    graph_rows = []
    for row in summaries:
        if row["metric"] in ("actual_arrival_soc_pct", "actual_relocation_distance_km",
                             "operationally_poor_outcome", "relocation_completion_time_s",
                             "recommendation_request_latency_ms", "shortage_change"):
            graph_rows.append(row)
    graphs = output / "graphs"
    graphs.mkdir(parents=True, exist_ok=True)
    graph_specs = (
        ("arrival-soc.svg", "Actual arrival SOC by scenario", "actual_arrival_soc_pct", "SOC (%)"),
        ("relocation-distance.svg", "Actual relocation distance by scenario", "actual_relocation_distance_km", "Distance (km)"),
        ("poor-outcome-rate.svg", "Operationally poor outcome rate", "operationally_poor_outcome", "Rate"),
        ("completion-time.svg", "Successful relocation completion time", "relocation_completion_time_s", "Simulated seconds"),
        ("recommendation-latency.svg", "End-to-end recommendation latency", "recommendation_request_latency_ms", "Milliseconds"),
        ("shortage-change.svg", "Destination shortage change", "shortage_change", "After minus before"),
    )
    for filename, title, metric, label in graph_specs:
        _bar_chart(graphs / filename, title,
                   [row for row in graph_rows if row["metric"] == metric], "mean", label)

    overall = defaultdict(list)
    # Equal scenario weighting: first calculate each scenario mean, then average scenarios.
    for row in summaries:
        if row["metric"] in ("operationally_poor_outcome", "actual_arrival_soc_pct",
                             "actual_relocation_distance_km", "relocation_completion_time_s",
                             "recommendation_request_latency_ms") and row["mean"] is not None:
            overall[(row["algorithm"], row["metric"])].append(float(row["mean"]))
    overall = {key: statistics.fmean(values) for key, values in overall.items()}
    energy_poor = overall.get(("energy_aware", "operationally_poor_outcome"))
    nearest_poor = overall.get(("nearest_ev", "operationally_poor_outcome"))
    poor_effect = next((row for row in paired if row["scenario"] == "ALL_EQUAL_WEIGHT" and
                        row["metric"] == "operationally_poor_outcome"), None)
    if poor_effect and poor_effect["ci95_high"] is not None and poor_effect["ci95_high"] < 0:
        conclusion = "Energy-aware selection measurably reduced operationally poor outcomes in this simulation; the paired 95% confidence interval excluded zero."
    elif poor_effect and poor_effect["ci95_low"] is not None and poor_effect["ci95_low"] > 0:
        conclusion = "Energy-aware selection produced more operationally poor outcomes in this simulation; the paired 95% confidence interval excluded zero."
    else:
        conclusion = "This simulation did not establish a measurable difference in operationally poor outcomes; the paired 95% confidence interval included zero."
    report = ["# Phase 4 evaluation report", "", conclusion, "",
              "The primary paired effect is energy-aware minus nearest EV. Negative values favour energy-aware selection for poor-outcome rate, distance, and time.", "",
              "## Equal-scenario descriptive means", "",
              "| Metric | Energy-aware | Nearest EV |", "|---|---:|---:|"]
    for metric in ("operationally_poor_outcome", "actual_arrival_soc_pct",
                   "actual_relocation_distance_km", "relocation_completion_time_s",
                   "recommendation_request_latency_ms"):
        energy = overall.get(("energy_aware", metric))
        nearest = overall.get(("nearest_ev", metric))
        report.append(f"| {metric} | {energy:.4f} | {nearest:.4f} |" if energy is not None and nearest is not None
                      else f"| {metric} | n/a | n/a |")
    request_totals = {algorithm: {
        "successes": sum(row["successful_relocations"] for row in usable if row["algorithm"] == algorithm),
        "requests": sum(row["request_count"] for row in usable if row["algorithm"] == algorithm)}
        for algorithm in ("energy_aware", "nearest_ev")}
    report.extend(["", "## Request outcomes", "",
        "Concurrent trials contain two approval requests. Request-level success is therefore reported separately from the equal-scenario mean.", "",
        "| Algorithm | Successful requests | Total requests | Rate |", "|---|---:|---:|---:|"])
    for algorithm, counts in request_totals.items():
        report.append(f"| {algorithm} | {counts['successes']} | {counts['requests']} | {counts['successes'] / counts['requests']:.4f} |")
    report.extend(["", "## Interpretation limits", "",
        "- Six approximate Singapore simulation points; no road routing or traffic.",
        "- Synthetic demand, charging and scheduled communication outages.",
        "- Prediction and movement use related energy assumptions.",
        "- In-process service transport; latency is not deployed network performance.",
        "- Fixed policy weights were not tuned with these results.",
        "- Missing arrival values from failed trials are retained as null and failure rates are reported separately.", ""])
    (output / "report.md").write_text("\n".join(report), encoding="utf-8")
    return {"records": len(records), "usable_records": len(usable),
            "infrastructure_errors": len(records) - len(usable)}
