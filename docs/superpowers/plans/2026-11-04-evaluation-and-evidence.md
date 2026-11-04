# EV Selection Evaluation and Reproducible Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a fair, reproducible comparison of the existing fixed energy-aware EV-selection policy with nearest eligible EV, plus an independent Kubernetes reliability/performance assessment, suitable for the CSC3104 research article.

**Architecture:** Reuse the current seeded in-process research harness for decision-policy comparison. Keep deployment stress measurements in a separate evidence lane because in-process API timing, synthetic movement and dashboard HTTP HPA loads do not measure the same thing as real process/network/vehicle telemetry performance. Preserve raw failed trials, incomplete pairs and source hashes.

**Tech Stack:** Python, pytest, existing `experiments/strategy_evaluation/`, `scripts/run_strategy_evaluation.py`, `scripts/analyze_strategy_results.py`, Docker Desktop Kubernetes, PowerShell, simulation-console analytics/CSV.

**Spec:** `docs/superpowers/specs/2026-11-04-csc3104-delivery-design.md`

## Global Constraints

- Do **not** modify the primary 0.35 distance / 0.20 current SOC / 0.35 arrival reserve / 0.10 charging-fit weights based on observed outcomes.
- Use paired seeds and identical eligibility rules for both comparison policies; compare like with like.
- Never run a load test while another group member has active rental, charging or relocation demonstrations.
- Preserve every failed or incomplete trial and record the denominator used in conclusions.
- Existing run under `results/strategy-evaluation/run-20261009-final` has a manifest with `dirty: true`; do not re-label it as a clean, current benchmark.
- Classify cached LTA vs synthetic demand/traffic, simulation clock vs wall clock, 1 vs 20 actual simulated pods, and GET-only HPA equivalent workloads distinctly.
- Evaluation evidence must be based on the frozen, tested code SHA from G0–G3, not an unmerged PR.

## Review Focus

1. **Seed-pair missing due to an experiment exception:** exclude only from paired estimate and report counts/failures, not zero (Task 3).
2. **Nearest EV low SOC under identical eligibility:** compare only eligible candidates; hard reserve remains unchanged (Task 1).
3. **One strategy fails an acknowledgement/timeout:** retain failure as an operational outcome; do not silently drop it (Task 3).
4. **Kubernetes CPU saturation during 20-EV run:** record performance degradation and restarts as evidence; do not assume success or automatically scale state writers (Task 4).
5. **HPA scales the static dashboard:** don't claim Fleet/Dispatch scalability from GET-only dashboard load (Task 5).

---

### Task 1: Freeze a clear research question and scenario protocol

**Files:**
- Review: `docs/strategy-evaluation.md`, `experiments/strategy_evaluation/scenarios.py`, `experiments/strategy_evaluation/runner.py`
- Create: `docs/evidence/2026-11-04/evaluation-protocol.md`
- Test: `tests/unit/test_evaluation_scenarios.py`, `tests/unit/test_evaluation_statistics.py`

**Interfaces:**
- Consumes: `generate_scenario(name: str, seed: int) -> ScenarioSpec`, its stable `state_hash()`, existing strategy implementations.
- Produces: preregistered protocol, exact frozen code/config/metrics and reproducibility metadata.

- [ ] **Step 1: State hypothesis before results.** Example: energy-aware selection reduces failures or arrivals below 30% SOC in low-reserve scenarios without excessive distance/latency cost; it might be worse on some cases.
- [ ] **Step 2: Fix operational definitions.** Primary: 'operationally poor' = failed/aborted request or successful arrival below 30% SOC, as existing docs define; reserve constraint remains 20%. Secondary: actual arrival SOC (only successful), distance, completion time, scoring request latency, shortage change.
- [ ] **Step 3: Freeze full matrix.** The existing `SCENARIO_NAMES` includes 10 scenarios; 30 matched seeds and 2 algorithms give 600 planned trials. Record all scenario names, seed range, time scale and code SHA before executing.
- [ ] **Step 4: Verify determinism.** `python -m pytest tests/unit/test_evaluation_scenarios.py tests/unit/test_evaluation_statistics.py -q -x`; write a failing test if a supposedly fixed seed yields different canonical hashes or if invalid SOC passes validation.
- [ ] **Step 5: Commit the protocol.** `git add docs/evidence/2026-11-04/evaluation-protocol.md && git commit -m "docs(research): preregister paired strategy comparison"`

**Acceptance:** research question, metrics, expected denominators and differences between simulated and measured quantities are documented without looking at the final result.

### Task 2: Smoke-test the harness under a clean, fixed commit

**Files:**
- Review/modify only if broken: `experiments/strategy_evaluation/cluster.py`, `runner.py`
- Test: `tests/integration/test_strategy_evaluation.py`
- Output: `results/strategy-evaluation/<new-smoke-run>/`

**Interfaces:**
- Consumes: documented CLI, `EvaluationCluster`, and isolated ASGI service instances per trial.
- Produces: complete, reproducible low-count smoke run.

- [ ] **Step 1: Check clean source state** `git status --short` and `git rev-parse HEAD`. If dirty, record exact diff hash or create a clean worktree; never mislabel a dirty run.
- [ ] **Step 2: Run existing research integration test.** `python -m pytest tests/integration/test_strategy_evaluation.py -q -x`; ensure Dispatch's route contract still works after PR #29, using offline test-only road routes, never invented production routes.
- [ ] **Step 3: Run a bounded smoke comparison.** `python scripts/run_strategy_evaluation.py --repetitions 1 --scenarios balanced_fleet nearest_low_soc --output results/strategy-evaluation/2026-11-04-smoke` (choose a unique unused directory).
- [ ] **Step 4: Inspect outputs.** Check manifest source SHA, `completion.json` planned/recorded counts, 4 raw trial records (2 scenarios × 1 seed × 2 policies), paired input hashes, and absence of infrastructure errors.
- [ ] **Step 5: If broken, reproduce by unit/integration test before fixing.** Re-run the specific test and clean smoke after minimal change.
- [ ] **Step 6: Commit harness fixes only if necessary; retain smoke logs as evidence.**

**Acceptance:** smoke completes with exact pair matching and honest infrastructure-error reporting.

### Task 3: Run the preregistered comparison and regenerate analysis

**Files:**
- Use: `scripts/run_strategy_evaluation.py`, `scripts/analyze_strategy_results.py`
- Review: `experiments/strategy_evaluation/analysis.py`
- Evidence: `results/strategy-evaluation/<new-final-run>/`, `docs/evidence/2026-11-04/evaluation-summary.md`

**Interfaces:**
- Consumes: Task 1 frozen protocol + Task 2 passing smoke.
- Produces: 600-trial raw records, reproducible paired tables, figures and uncertainty intervals.

- [ ] **Step 1: Record environment, code SHA, branch cleanliness and configuration** including Python version, CPU, memory, date, simulation speed, seed range and complete manifest.
- [ ] **Step 2: Execute 30-repetition comparison.** `python scripts/run_strategy_evaluation.py --repetitions 30 --output results/strategy-evaluation/2026-11-04-evidence` (unique unused directory). If 600 trials are impossible by the deadline, report a smaller predeclared sample as exploratory, not an equivalent full study.
- [ ] **Step 3: Verify completion.** Inspect `completion.json`, `raw/trials.jsonl` and `manifest.json`; planned 600 vs recorded and actual complete pairs. Never suppress harness exceptions or delete expected unsuccessful operations.
- [ ] **Step 4: Recompute graphs independently.** `python scripts/analyze_strategy_results.py results/strategy-evaluation/2026-11-04-evidence`. Check `tables/paired-effects.csv`, `tables/scenario-summary.csv`, `tables/failure-summary.csv` and SVG graphs.
- [ ] **Step 5: Write interpretation from observed results only.** Per-scenario differences first, equally weighted aggregate second, counts and intervals, effect direction, whether improvements cost extra distance/time, missing data and uncertainty.
- [ ] **Step 6: Preserve source-linked evidence.** Commit small evidence summary/manifest if policy permits; otherwise keep immutable raw outputs with hashes and reproducible command in the submission bundle.

**Acceptance:** every quantitative claim cites a figure/table generated from retained raw records; no evidence cherry-picking.

### Task 4: Measure process-level and Kubernetes reliability separately

**Files:**
- Review/use: `scripts/start-kubernetes.ps1`, `docs/kubernetes-local.md`, `src/ev_fleet/simulator/commands.py`, `k8s/simulators.yaml`, `src/ev_fleet/rebalancing/shared_traffic.py`
- Create: `docs/evidence/2026-11-04/k8s-metrics.csv`, `docs/evidence/2026-11-04/k8s-method.md`
- Test: validated safe-operator/Kubernetes smoke test from G2

**Interfaces:**
- Consumes: controlled local cluster and currently configured 20 EV simulators.
- Produces: repeatable service performance and stability observations, **not** a simulation-policy causal experiment.

- [ ] **Step 1: Write the trial matrix and safety preflight.** Example conditions: 1 idle EV, 20 idle EVs, and synthetic 144k-row traffic indexing under the same environment. No active dispatch or rental. Record Docker Desktop resource allocation, Pod versions, Windows background workload, traffic feed mode and exact SHA.
- [ ] **Step 2: Validate healthy baseline.** `kubectl -n ev-fleet get pods,deployments,statefulsets`; `kubectl -n ev-fleet top pods` (if metrics server exists); source feed status and Fleet telemetry recency. Record missing metrics as NULL rather than zero.
- [ ] **Step 3: Run repeated bounded observations.** At least five trials per meaningful comparison if practical; collect service request attempts/success/error, p50/p95 latency, throughput, CPU/memory, Pod restarts, max telemetry age and availability transitions. A **disposable** low-load control first.
- [ ] **Step 4: Inject one fault at a time in isolated environment.** Examples: pause network access to a test instance, reject a test Redis write, a disposable PostgreSQL connection interruption, unavailable OSRM. Keep production/saved database intact. Verify expected safe degradation/recovery.
- [ ] **Step 5: Compute observed comparisons.** Do not invent speed-ups; compare same request types, warm-up state, payload sizes and identical hardware. Write raw-sample table and method with limitations.
- [ ] **Step 6: Verify repeatability from instructions**, assign a team member to reproduce a sample, and commit method/evidence summary.

**Acceptance:** measurements distinguish cloud-native service operation from in-process ASGI timing and identify errors/restarts rather than concealing them.

### Task 5: Keep dashboard HPA measurements scoped correctly

**Files:**
- Review: `docs/simulation-analytics.md`, `simulation-console/backend/analytics.py`
- Evidence: `docs/evidence/2026-11-04/hpa-observations.md`

**Interfaces:**
- Consumes: local simulation-console performance analytics (read-only HTTP GET load) and Kubernetes HPA readings.
- Produces: an accurate, limited autoscaling figure or a documented unavailable result.

- [ ] **Step 1: Check `kubectl get hpa dashboard -n ev-fleet` and `kubectl top pods -n ev-fleet`.** Record whether Metrics Server reports CPU and whether replicas can be scheduled.
- [ ] **Step 2: Run a normal, high or extreme **GET-only** workload for 30–120 seconds under the existing bounded console controls; do not invent EVs or move vehicles.
- [ ] **Step 3: Export CSV and record baseline vs load CPU, current/desired replicas, HTTP errors, latency and observed scaling** (including no scaling).
- [ ] **Step 4: State the limitation in report.** Dashboard HPA load tests Nginx/HTTP scaling response; it does **not** prove Fleet telemetry, Dispatch coordination or horizontal scaling of state-owning services.
- [ ] **Step 5: Commit `hpa-observations.md` or record a BLOCKED reason if metrics are unavailable.**

**Acceptance:** HPA results are real observed values and claims are restricted to the measured component.

## Handoff to paper and demo

Freeze the final experimental code revision and attach an evidence index with command, platform, SHA and result state. Use the separately authored `2026-11-04-course-submission.md` plan to turn these results into article figures and the recorded demonstration. Defer any adaptive weighted-selection algorithm to future work if clean evidence and the course deliverables are not already complete.
