# CSC3104 EV Fleet — Final Delivery Design (4–15 November 2026)

**Status:** Proposed design for group review; planning only. **Product code must not be changed or PRs merged until this design is approved.**

## Intent and motivation

Deliver a verifiably correct, demonstrable, cloud-native **simulation-only** EV fleet rebalancing system for CSC3104. The differentiator is distributed coordination under stale data, contention, uncertain outcomes, traffic disruption, and charging constraints, coupled with an explainable, human-approved energy-aware decision policy. Avoid claiming to control actual vehicles or that synthetic inputs are live operational ground truth.

**Course constraint:** The uploaded 2026–27 project brief requires containers and/or Kubernetes, a research article of around 3,000 words (maximum six pages, IEEE or ACM proceedings style), quantitative evaluation, a 10–15-minute recorded presentation, and explicit testing instructions. Final submission is **Sunday 15 November 2026 at 23:59**. Deliver the source/GitHub URL, `GROUPNN.pdf`, `GROUPNN_INSTRUCTIONS.pdf`, and a video/link. The course explicitly does **not** require a sophisticated GUI. The group number and member assignments are not available from the repository; replace `NN` during packaging.

## Decisions already made

- **User-selected approach:** full project, **stability first**, then operator UI, measured experiments, and academic delivery. This outranks feature expansion.
- **Alternative A (UI-first):** fast visual results, weak evidence and unresolved backend defects. Rejected as primary sequence.
- **Alternative B (feature-first/adaptive AI now):** higher ambition, high change and measurement risk before submission. Defer unless all gates pass early.
- **Alternative C (stability/evidence-first):** retain the existing six-zone system and evaluation harness, close correctness gaps, validate controlled deployment, compare existing algorithms, then polish/record. **Chosen.**
- New changes should be small, TDD-driven, isolated PRs. Do not mix frontend polish with claims, routing, persistence, and Redis safety changes.

## Evidence-based baseline (as at 4 November 2026)

- `main` contains services Fleet, Demand, Rebalancing, Dispatch, Facilities; 20 EV simulators, Redis traffic collector, PostgreSQL snapshots, the operator React/Leaflet dashboard, and the simulation console.
- PR #27 routing test-fixture repair and PR #28 Vitest worker-mode adjustment are **merged**.
- PR #29 `fix/audit-routing-performance-reliability` is **open and draft**, with changes to full-feed route assessment, optional OSRM alternatives, live closure overlays, spatial indexing, Redis publication and memory controls, dashboard dispatch-state messaging, and fail-closed persistence. Its implementation and new tests are not independently verified on the user's cluster.
- PR #30 `design/impeccable-fleet-dashboard-polish` is **open and draft**, with an operational pulse and UI polish, plus unit tests. It is also unverified.
- Last reported local Python full suite: **245 passed, 1 skipped** on a previously selected revision; that is **not** evidence that PR #29 passes.
- Last reported Windows Vitest run on merged PR #28: **five worker startup timeouts**, **zero tests executed**, after 300 seconds; changing forks to threads did not fix it.
- `.github/workflows/infrastructure-checks.yml` and `kubernetes-portability.yml` currently trigger on **manual `workflow_dispatch` only**; a mergeable PR or absent checks does not prove passing validation.
- `docs/strategy-evaluation.md` already defines a **10-scenario × 30-seed × 2-policy = 600-trial** in-process comparison using common eligibility and matched state hashes. A historic `results/strategy-evaluation/run-20261009-final/manifest.json` records `dirty: true`. Prefer a fresh clean-revision run and disclose limitations.
- The project's own `README.md` states the selection policy is 35% distance utility, 20% current SOC, 35% arrival reserve, and 10% charging suitability. Do not retrofit/adapt these weights in the primary comparison.

## Architecture and invariants to preserve

```text
EV simulators ─telemetry/session─> Fleet ─snapshots─┐
                                                   ├─> Demand (zone supply/shortage)
Facilities ─parking/charging allocations───────────┤
                                                   └─> Rebalancing (eligible, ranked advice)
LTA collector ─> Redis ─> Rebalancing route assessment ┘
OneMap/optional OSRM ─road geometry─> Dispatch (review + explicit approval)
Operator dashboard ─review/approve─> Dispatch ─claim─> Fleet
Dispatch ─command/ack/progress─> EV simulator ─telemetry─> Fleet
All service-owned durable snapshots ─> PostgreSQL
Local Simulation Console ─demand/load/rental controls + analytics
```

1. A recommendation must **never** itself dispatch an EV.
2. A single EV cannot hold two conflicting claims; parking allocations cannot exceed capacity.
3. Missing/old telemetry and uncertain command outcomes must **not** create available vehicles by default.
4. Routes must be explicit approved road geometries. OneMap remains primary, and a configured OSRM route must satisfy the same vehicle-road contract; no straight-line production fallback. **Critical demo prerequisite:** production \`ROUTING_PROVIDER=SIMULATED\` returns no road route and Dispatch currently rejects it with \`ONEMAP_ROUTE_REQUIRED\`; do not promise a live EV movement demo without valid OneMap routing. The existing offline test-only route fixtures can demonstrate the safety contract, but must never be represented as real road routing.
5. Live closures must not be masked by artificial scenarios. Traffic display samples are **not** the source for approval-time scoring.
6. PostgreSQL failure and ambiguous commits must fail closed without silently rolling back/overwriting durable ownership.
7. Redis is an ephemeral cache; stale, missing or erroneous feeds remain visibly unknown. Its liveness is separate from actual successful LTA collection.
8. No public exposure of privileged local simulation console; no tokens, personal records, or API keys in logs or report.
9. State-owning Fleet, Dispatch, Demand, Facilities and Rebalancing remain singleton writers unless a deliberate ownership redesign proves safe.
10. No test that cannot run is counted as passed. Distinguish unit, API, Compose, Kubernetes, browser, synthetic performance, and in-process research trials.

## Workstream and release design

### A. Stable, attributable baseline (release gate G0)

Use a clean local worktree and record exact Git revision, Python/Node versions, GitHub PR state, execution OS, Docker/Kubernetes versions, and external integration mode. Run narrow Python tests and full suite on `main`, verify dashboard build, run a **single** Vitest test, then compare Vitest on Windows outside OneDrive and GitHub Actions Linux. Diagnose worker processes, not just assertion timeout. Add a **lightweight PR-triggered** backend + frontend workflow after review, leaving costly Compose/PostgreSQL/K8s exercises manual when appropriate.

**Exit:** CI provides a trustworthy pass/fail signal for source commits. If Windows Vitest remains broken, record it as blocked while Linux CI actually runs the tests; browser E2E still requires explicit verification.

### B. Diagnose EV availability transitions (G1)

Fleet computes effective availability in `src/ev_fleet/fleet/store.py::view` from telemetry freshness, active claim/rental/charging task, and physical availability/relocation/charging state. The simulator reports telemetry about every 5 seconds, Fleet freshness becomes stale at 15 seconds and offline at 30 seconds; network retries and PostgreSQL serialisation could cross that boundary. Also, `MovingNode.refresh_parking` can set `parking_verified=False` on Facilities errors and the next telemetry becomes unavailable. The rental demo being off is **not** sufficient proof of availability.

**Observe first:** sample `/api/v1/vehicles/{id}` every 2–4 seconds alongside Fleet/simulator/Facilities logs and Pod restarts. Classify actual reason before changing intervals. Preserve stale=unavailable for safety.

**Exit:** 20 idle EVs have a documented state-transition trace under bounded normal load; intermittent legitimate loss of telemetry is distinguished from erroneous availability toggles. Any fix has a reproducing test and no double-claim regression.

### C. Validate and integrate PR #29 (G2)

Inspect its added tests plus code contracts; test OSRM selection/acceptance, real roadblocks during simulations, full-feed route assessment and index equivalence, freshness/unknown handling, Redis out-of-memory publication, PostgreSQL uncertain commits and restart reconciliation. Run the focused tests first, then full suite, Linux CI and disposable Compose/Kubernetes smoke. Review whether any `POST /traffic/assess-route` edge cases, endpoint security or resource bounds need attention. **Do not merge simply because GitHub shows mergeable.**

**Exit:** all planned invariants pass against the exact candidate commit; no unexplained deployment regressions or silent fallback to unsafe routing. Record a change-impact matrix.

### D. UI validation and PR #30 (G3)

Treat the user-facing simulation dashboard as an operator console, **not** autonomous AI control. Build the TypeScript/frontend, prove new operational counters do not turn unknown into zero, run meaningful browser flows in light/dark and small/wide viewports, verify keyboard/focus/contrast, and show stale/unknown/failed outcomes distinctly. After PR #29 is stable and merged, **rebase PR #30 onto updated main** and reconcile overlapping `dashboard/src/App.tsx` edits; rerun tests after resolving.

**Exit:** UI accurately mirrors service states; explicit approval cannot be implied by a 202 response; screenshot/video evidence exists.

### E. Quantitative evaluation (G4)

**Research question:** under identical eligibility and matched inputs, when and at what cost does the fixed energy-aware strategy improve relocation outcomes relative to nearest eligible EV?

Reuse `scripts/run_strategy_evaluation.py`, `scripts/analyze_strategy_results.py` and existing scenarios. Freeze policies and seeds before running. Record 10 scenarios, 30 seeds, two methods and exact source revision. Primary measures: rate of operationally poor outcomes (predefined failure/aborted/below 30% arrival SOC), arrival SOC, distance, completion, and service latency; stratify by scenario and preserve failures/missing values. Supplement with **separate** repeatable Docker/K8s stress runs (1 vs 20 EVs, 144k Speed Bands where available) measuring service availability, p50/p95 request latency, CPU, memory, retries and restarts. Never substitute dashboard GET-only HPA load for telemetry throughput or use in-process latency as network performance.

**Exit:** graphs and tables reproducible from raw files, comparison fair, environment and uncertainty documented; any improvement claim backed by evidence rather than intuition.

### F. Final academic delivery (G5)

Write around 3,000 words in **at most six pages** (IEEE/ACM), including abstract (≤200 words), motivation, contribution, system design, implementation/concurrency/fault handling, evaluation graphs and limitations, related work, conclusion, cited references. Do not claim first-of-kind autonomy; originality should be framed as the **specific integration and evaluated engineering tradeoffs** and supported by a short related-work comparison.

Deliver `GROUPNN.pdf`, `GROUPNN_INSTRUCTIONS.pdf`, source/repository, and a 10–15-minute video. Instructions must permit a marker to reproduce **credential-free startup, read-only fleet/heat-map views, recommendation-only workflow and explicit refusal of unroutable approvals** without private OneMap/LTA credentials. Full production-container vehicle movement currently requires a valid OneMap route/token; if the group cannot provision this, demonstrate the routed lifecycle separately through an explicitly labelled, isolated **test-only fixture** with no production safety bypass. State which steps depend on private external integrations and which tests were/weren't run. Preserve a clean submission tag and video/figure revision trace.

**Exit:** all four submission artefacts exist and have been cross-checked against the final committed source and the course brief before **15 November 2026 23:59**.

## Timeboxed timeline (planning estimates, not background promises)

| Date (2026) | Priority and expected evidence |
|---|---|
| 4–5 Nov | Freeze baseline, enable reliable CI, triage Vitest, sample EV availability reasons |
| 6–7 Nov | Correct/test candidate PR #29; disposable failure, routing and cluster smoke tests |
| 8 Nov | Review/rebase/validate PR #30; final operator flow and screenshots |
| 9–11 Nov | Freeze policies; rerun validated strategy comparison; collect separate deployment benchmarks; draft report |
| 12–13 Nov | Finish figures, limitations and related work; produce testing-instructions PDF; record demonstration |
| 14 Nov | Independent reproduction, package audit, final link/PDF/video playback checks |
| 15 Nov | Buffer for submission only; do not introduce risky new features |

This is a **priority sequence**, not a guarantee of completion; move directly to deliverables if a gate blocks further work.

## Definition of done and evidence states

- **PASS:** command executed on the named commit and platform; logs, exit code, and assertions/outputs retained.
- **FAIL:** command executed and failed; traceback, logs and impact retained.
- **BLOCKED:** could not execute; precise reason and alternative evidence retained; never masquerade as pass.
- **NOT RUN:** not attempted; assigned owner and decision deadline recorded.
- **NOT APPLICABLE:** reason explicitly documented.

A feature is `ready to merge` only when test and safety gates relevant to that feature pass. A course deliverable is `ready to submit` only if the source, report, instructions and video all exist and can be opened, and required limitations are stated.

## Exclusions and future work

- No LLM agents, predictive model training or adaptive-weights redesign before G0–G5. The existing fixed explainable score is defensible and can be contrasted with a baseline.
- No cloud/public production deployment or actual autonomous driving/rental payments.
- No destructive PostgreSQL/PVC recreation, blanket error suppression, or artificially inflated stale threshold to hide missed telemetry.
- No relabelling synthetic demand, OneMap matching or simulated outcomes as verified physical service performance.
- A genuine per-vehicle relational persistence redesign is a **separate post-submission** migration until necessity is measured.

## Review decisions before execution

- Confirm the project's intended claim of originality and the report's research question.
- Confirm which team member owns backend/cluster, UI, experiments, report and final submission.
- Decide by **6 November** whether a valid OneMap token and permissible network access will be available for the live routed demo. Without it, use the truthful credential-free limited demo plus labelled offline fixture lifecycle; no production straight-line routing fallback.
- Approve this design and associated **planning drafts** before any implementation changes.

## Linked task plans

- `docs/superpowers/plans/2026-11-04-stability-testing-and-pr-integration.md` — G0–G3.
- `docs/superpowers/plans/2026-11-04-evaluation-and-evidence.md` — G4.
- `docs/superpowers/plans/2026-11-04-course-submission.md` — G5.
