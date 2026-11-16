# EV-selection strategy evaluation and baseline comparison

This archived evaluation compares the legacy energy-aware EV selector
(`ev-aware-v1`) with nearest eligible EV. The study predates the default
[CRDS](crds.md) policy. Its results apply to the two tested selectors.

## Research question

Does energy-aware EV selection reduce operationally poor relocations, especially low-arrival-SOC outcomes, while maintaining reasonable relocation distance and response time compared with nearest-EV selection?

An operationally poor trial is declared before analysis as a failed/aborted request or a successful arrival below 30% SOC. The existing hard operational reserve remains 20%.

## Fair comparison

Both selectors call the same `assess()` function. Missing/stale/offline telemetry, unavailable or busy EVs, active claims, and predicted reserve violations therefore produce identical exclusions. Nearest EV orders eligible candidates by estimated distance then vehicle ID. Energy-aware uses the unchanged fixed weights: distance 0.35, current SOC 0.20, arrival reserve 0.35, and charging fit 0.10.

Every scenario/seed is generated once and used for both algorithms. Its canonical SHA-256 state hash is stored in both records and checked at completion. A new Fleet, Demand, Rebalancing, Dispatch, clock and set of stores is created for every algorithm trial; there is no reset endpoint or state reuse. Algorithm order alternates by seed.

The harness uses real service applications over in-process ASGI transport, real Fleet claims and Dispatch revalidation, and the existing `Movement` model. A controllable clock advances in 60-second simulation ticks, avoiding wall-clock waits. Recommendation request latency is measured with a real monotonic timer; relocation time is simulated time. These latency results are algorithm/process measurements, not deployed network benchmarks.

## Matrix

The full evaluation contains 10 scenarios, 30 matched seeds, and 2 algorithms: 600 scenario trials. The scenarios are balanced fleet, nearest low SOC, farther high SOC, limited charging, multiple eligible EVs, multiple hotspots, offline before selection, disconnect during relocation, changing demand, and concurrent requests.

Scenario parameters receive small deterministic position and SOC perturbations. Validation preserves the intended condition. The outage trial stops selected-EV communication beyond the offline threshold while physical movement continues; it must record `UNKNOWN` before recovery. The concurrency trial creates two recommendations from the same starting snapshot and approves both concurrently. Fleet must retain one active claim and reject the competing version.

Multiple-hotspot targets remain operator-defined. Neither selection algorithm chooses where demand should be served.

## Run

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\run_strategy_evaluation.py --repetitions 30
```

Use `--output results\strategy-evaluation\my-run` for a fixed output path. The runner refuses to overwrite an existing directory. A smaller non-evidentiary check can select scenarios and repetitions:

```powershell
.\.venv\Scripts\python.exe scripts\run_strategy_evaluation.py --repetitions 1 --scenarios balanced_fleet nearest_low_soc --output results\strategy-evaluation\smoke
```

Regenerate analysis without executing trials:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_strategy_results.py results\strategy-evaluation\<run-id>
```

## Outputs

Each run contains:

- `manifest.json`: preregistered settings, scenario list, frozen weights, source revision, exact evaluation-source SHA-256 and environment.
- `completion.json`: planned/recorded counts, state-hash mismatches and infrastructure errors.
- `raw/trials.jsonl`: complete nested records, written and flushed after every trial.
- `raw/trials.csv`: flattened machine-readable copy.
- `tables/scenario-summary.csv`: count, mean, median, standard deviation, p95 and confidence intervals.
- `tables/paired-effects.csv`: seed-paired energy-aware-minus-nearest effects, including an equally weighted aggregate.
- `tables/failure-summary.csv`: visible failed/aborted outcomes and reasons.
- `graphs/*.svg`: arrival SOC, distance, poor outcomes, completion time, latency and shortage change.
- `report.md`: result-focused comparison and limitations generated from raw JSONL.

Missing actual arrival SOC after a failed request remains null. It is never imputed or silently removed. Expected simulated relocation failures are outcome data. A harness exception is an `infrastructure_error`, remains in raw output, and makes `completion.json` incomplete. Paired analysis includes only complete pairs and exposes their counts.

Continuous metrics use descriptive summaries and deterministic percentile-bootstrap 95% confidence intervals. Rates include counts and Wilson intervals. Paired differences use matched scenario/seed inputs. Results are presented per scenario before the equally weighted aggregate. No weights are fitted, no scenarios are discarded after viewing outcomes, and no single trial is treated as evidence.

## Results

The saved run is `results/strategy-evaluation/run-20261009-final`: 600 of 600
trials recorded, no paired starting-state hash mismatches and no infrastructure
errors. Its manifest records `dirty: true`, meaning it ran on a working tree
with uncommitted changes. Its [report](../results/strategy-evaluation/run-20261009-final/report.md)
has the full tables and graphs.

Effects are energy-aware minus nearest EV, with scenarios weighted equally.

| Metric | Energy-aware | Nearest EV | Paired effect | 95% bootstrap CI |
|---|---:|---:|---:|---:|
| Operationally poor outcome | 0.1000 | 0.2000 | -0.1000 | [-0.1367, -0.0667] |
| Actual arrival SOC | 76.64% | 57.26% | +19.38 pp | [+16.83, +22.06] |
| Relocation distance | 13.44 km | 10.55 km | +2.89 km | [+2.50, +3.30] |
| Completion time | 1102.9 s | 880.7 s | +222.2 s | [+190.0, +253.4] |
| Recommendation latency | 12.41 ms | 12.45 ms | -0.04 ms | [-0.48, +0.42] |

Energy-aware selection produced no arrivals below 30% SOC; nearest EV produced
30. Each algorithm completed 300 of 330 dispatch requests; the other 30 were the
deliberately concurrent second claims, all refused with
`VEHICLE_VERSION_CONFLICT`. In this simulation the energy-aware policy reduced
poor relocations at the cost of longer trips, with no measurable latency
difference.

## Limitations

Locations, demand, charging availability and outages are synthetic. Distance uses approximate Singapore coordinates and a route factor rather than roads or traffic. Prediction and movement use related energy assumptions, so arrival-SOC error understates real-world uncertainty. Services run in memory and latency excludes deployed network/container behavior. The fleet is small, the six zones are coarse, and strategy evaluation evaluates fixed policy weights rather than optimizing them.
