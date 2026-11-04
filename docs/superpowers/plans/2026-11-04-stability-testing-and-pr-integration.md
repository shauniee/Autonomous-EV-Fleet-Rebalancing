# Stability, Testing and PR Integration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish trustworthy automated verification, diagnose intermittent EV unavailability, then safely integrate backend PR #29 and UI PR #30 without violating dispatch, route or state-ownership invariants.

**Architecture:** Keep the existing single-writer HTTP microservices and explicit approval model. Test critical state changes at pure unit, HTTP, persistence, Compose and Kubernetes levels separately. Treat PR #29 as a candidate backend release; layer PR #30 on updated main only after backend acceptance.

**Tech Stack:** Python >=3.10; FastAPI/Pydantic v2/httpx/asyncpg/Redis; pytest; React 19/TypeScript/Vite/Vitest 4/Playwright; PowerShell; Docker Compose; Docker Desktop Kubernetes; GitHub Actions (Python 3.12, Node 22).

**Spec:** `docs/superpowers/specs/2026-11-04-csc3104-delivery-design.md`

## Global Constraints

- Submission due **15 November 2026 23:59**. Research, demo and packaging take precedence over speculative feature work.
- No real vehicles, public simulation-console exposure, or use of private credentials in tests/logs.
- Human approval is separate from recommendation, and a claim is single-owner and idempotent.
- Missing/ambiguous/expired observations are not an all-clear.
- Refuse cluster redeploys with active claims, rentals, relocations or charging.
- Preserve PostgreSQL PVC; fault injection only on a disposable test DB.
- Source branch and exact commit must be recorded for every PASS/FAIL/BLOCKED claim.
- Do not merge PR #29 or PR #30 until the applicable release gate passes.
- Use Windows `npm.cmd` and `.venv\Scripts\python.exe`; Linux CI uses Node 22 and Python 3.12.

## Review Focus

1. **Telemetry arrives late but node remains ONLINE:** EV must become stale/unavailable at the configured threshold, then recover only after valid fresh telemetry (Task 3).
2. **Facilities lookup fails while an idle EV polls:** availability cannot oscillate without an observable reason; a disconnected Facilities feed must not be treated as verified safe parking (Task 3).
3. **OneMap route is blocked or unavailable with OSRM configured:** OSRM route must be vetted by the complete traffic dataset and accepted only with explicit road geometry (Task 4).
4. **Redis publishes a blob during OOM or data expiry:** metadata must never become a falsely fresh pointer to absent or invalid payload, and status must expose the error (Task 5).
5. **PostgreSQL loses a commit response during a Fleet claim:** service must fail closed and reconcile from durable state; a second claim cannot be granted (Task 6).

---

### Task 1: Freeze a reproducible baseline and PR dependency map (G0)

**Files:**
- Create: `docs/evidence/2026-11-04/baseline.md`
- Review: `README.md`, `scripts/test-local.ps1`, `docs/kubernetes-local.md`
- Inputs: PR #29 and PR #30 (draft), merged PR #27 and #28

**Interfaces:**
- Consumes: existing repo and the documented safe startup script.
- Produces: a single evidence matrix that every later task references.

- [ ] **Step 1: Record environment and repository identity.** Run `git status -sb`, `git rev-parse HEAD`, `git branch --show-current`, `node --version`, `python --version`, `docker version`, and `kubectl config current-context`; redact secrets and account details.
- [ ] **Step 2: Record current checks without interpreting absent checks as green.** Capture PR #29/#30 head SHAs, draft status, files changed, GitHub checks and whether they were actually executed.
- [ ] **Step 3: Collect prior failures.** In `baseline.md`, distinguish 245/1 Python results on historical revision from unverified PR #29 and five Vitest worker timeouts before test collection.
- [ ] **Step 4: Establish test classification.** Table: check, exact commit, platform, command, result PASS/FAIL/BLOCKED/NOT RUN, evidence path.
- [ ] **Step 5: Commit documentation only.** `git add docs/evidence/2026-11-04/baseline.md && git commit -m "docs: freeze release baseline and evidence provenance"`

**Acceptance:** the baseline never implies that any unexecuted test passed.

### Task 2: Make fast PR tests give reliable signals (G0)

**Files:**
- Modify: `.github/workflows/infrastructure-checks.yml`
- Review: `.github/workflows/kubernetes-portability.yml`
- Test: existing `tests/`, `dashboard/tests/`, `simulation-console/frontend/`

**Interfaces:**
- Consumes: `workflow_dispatch` workflow and existing `backend`, `frontend`, `compose-smoke`, `telemetry-postgres` jobs.
- Produces: PR-triggered cheap backend/frontend checks and retained manual expensive validation.

- [ ] **Step 1: Add a failing check of the existing gap.** Document that no `pull_request` event exists, so a new PR can report mergeable with zero checks.
- [ ] **Step 2: Change workflow triggers.** Preserve `workflow_dispatch`; enable `pull_request` with minimal `permissions: contents: read` for backend and frontend jobs. Ensure expensive database/Compose jobs run only under manual trigger or an explicitly chosen safe condition. Keep Linux Python 3.12 and Node 22.
- [ ] **Step 3: Verify workflow syntax and triggers.** Open a **docs-only test PR**; observe backend/frontend checks being scheduled and run on the exact head SHA. A YAML parse alone is not enough.
- [ ] **Step 4: Verify failure detection.** A deliberately failing temporary assertion on an isolated throwaway branch must mark a check failed; revert before merge.
- [ ] **Step 5: Commit.** `git add .github/workflows/infrastructure-checks.yml && git commit -m "ci: execute fast backend and frontend checks on pull requests"`

**Acceptance:** a PR displays the executed result of both fast jobs; no service credentials or live cluster are required.

### Task 3: Diagnose and test AVAILABLE/UNAVAILABLE oscillation (G1)

**Files:**
- Modify conditionally: `src/ev_fleet/fleet/store.py`, `src/ev_fleet/simulator/commands.py`
- Test: `tests/unit/test_fleet.py`, `tests/unit/test_moving_simulator.py` **if present**; otherwise create `tests/unit/test_availability_transitions.py`.
- Create: `docs/evidence/2026-11-04/availability-diagnosis.md`

**Interfaces:**
- Consumes: `FleetStore.view(record) -> dict`; `MovingNode.refresh_parking() -> None`; `MovingNode.send_telemetry() -> None`.
- Produces: reason-labelled diagnostic evidence and only the minimal confirmed bug fix.

- [ ] **Step 1: Gather controlled observations.** Sample a named idle EV every 2–4 seconds for 2–5 minutes: `effective_availability`, `stale`, `telemetry_age_seconds`, raw `telemetry.availability`, `connectivity`, `active_claim`, `active_rental`, `active_charging_task`, relocation/charging state. Correlate with `kubectl -n ev-fleet logs deployment/fleet`, the named EV Pod and Facilities errors/restarts. Do not mutate EV state.
- [ ] **Step 2: Classify the first observed transition.** Decide telemetry staleness, lost session, parking refresh failure, claim/rental/charging, or UI polling artifact; write it with timestamps and evidence. Do not infer a cause from the Rental Demo toggle.
- [ ] **Step 3: Write a failing regression test for that exact cause.** For stale telemetry, advance a fake clock over `settings.stale_seconds` and assert UNAVAILABLE even if connectivity remains ONLINE, then fresh telemetry recovers. For Facilities failure, test that a failed parking GET causes an explicit unverified/unavailable state and a subsequent genuine success restores it; assert no phantom claim.
- [ ] **Step 4: Run the failing test.** `python -m pytest tests/unit/test_availability_transitions.py -q -x` (or chosen existing module); record its specific failing assertion before code change.
- [ ] **Step 5: Implement the smallest correction.** Only if evidence establishes a bug: improve retry/backoff and status observability without increasing freshness thresholds to mask loss; if no bug exists, keep current safety behaviour and document expected transitions.
- [ ] **Step 6: Rerun and test contention.** Targeted test, `python -m pytest tests/unit/test_dispatch_lifecycle.py tests/unit/test_batch_dispatch.py -q`, and a controlled idle 20-EV observation.
- [ ] **Step 7: Commit.** `git add src/ev_fleet tests/unit docs/evidence/2026-11-04/availability-diagnosis.md && git commit -m "fix(fleet): diagnose and guard availability transitions"` (documentation-only if transition was correct).

**Acceptance:** every observed transition has an attributable reason; fresh/no-claim EVs do not oscillate under an otherwise healthy idle run. Stale EVs remain excluded.

### Task 4: Review and test PR #29 route safety before merge (G2)

**Files:**
- Review/modify in PR #29: `src/ev_fleet/dispatch/service.py`, `src/ev_fleet/dispatch/traffic_routes.py`, `src/ev_fleet/rebalancing/app.py`, `src/ev_fleet/rebalancing/traffic.py`, `src/ev_fleet/simulator/movement.py`
- Tests: `tests/unit/test_traffic_routing.py`, `tests/unit/test_traffic_scenarios.py`, `tests/unit/test_route_traffic_api.py`, `tests/unit/test_routing_contract_regression.py`

**Interfaces:**
- Consumes: `POST /api/v1/traffic/assess-route` with geometry/distance/duration; `choose_traffic_route(..., assess_route=...)`.
- Produces: road-only route approval and live-closure regression evidence.

- [ ] **Step 1: Run PR #29 tests on its actual head.** `git fetch origin && git switch --track origin/fix/audit-routing-performance-reliability`, then `python -m pytest tests/unit/test_traffic_routing.py tests/unit/test_traffic_scenarios.py tests/unit/test_route_traffic_api.py tests/unit/test_routing_contract_regression.py -q -x`. The old main-branch PASS is not sufficient.
- [ ] **Step 2: Add a failing boundary test if absent.** A blocked primary + safe configured OSRM route selects an explicit OSRM geometry; no OSRM service yields a safe refusal; malformed/synthetic/empty route is rejected. Test invalid geometry and unavailable assessment as explicit failures, not fallback to clear.
- [ ] **Step 3: Add mixed-scenario closure test.** A simulated congestion event and a coincident **fresh actual ROAD BLOCK** must make primary blocked; unrelated route remains eligible, and source labels remain distinct.
- [ ] **Step 4: Implement fixes only for failing behaviour.** Preserve OneMap-first and `Movement.accept` route contract, validate route geometry/assessment shape and record meaningful rejection reasons.
- [ ] **Step 5: Verify.** Rerun focused tests and full `python -m pytest -q`; then run the existing real-HTTP/Compose routing smoke on disposable fixtures.
- [ ] **Step 6: Commit to PR #29 and record exact SHA.** Do not merge yet.

**Acceptance:** no missing road route can be approved or executed, and real closures cannot be erased by a simulation overlay. **Preflight:** `ROUTING_PROVIDER=SIMULATED` deliberately produces no authoritative road geometry; the live route/EV acceptance test is BLOCKED without OneMap credentials or a properly isolated test-only fixture.

### Task 5: Review PR #29 traffic-index and Redis consistency (G2)

**Files:**
- Review/modify in PR #29: `src/ev_fleet/rebalancing/shared_traffic.py`, `src/ev_fleet/rebalancing/traffic.py`, `k8s/data-services.yaml`, `compose.yaml`
- Tests: `tests/unit/test_shared_traffic_cache.py`, `tests/unit/test_traffic_routing.py`
- Evidence: `docs/evidence/2026-11-04/traffic-benchmarks.md`

**Interfaces:**
- Consumes: `build_speed_index(rows)`; `assess_traffic(geometry, distance_km, duration_seconds, traffic, when=None)`; `GET /api/v1/traffic/cache-status`.
- Produces: exact matching semantics with bounded request time and no metadata corruption.

- [ ] **Step 1: Write a failing equivalence test for spatial edge cases.** Compare indexed and full-scan assessments near a grid boundary, a multi-segment zigzag, a long segment, empty geometry rejection and coincident endpoints. Include synthetic 144k-row performance input separately from semantic fixtures.
- [ ] **Step 2: Write failing failure-injection tests.** Redis payload write OOM must not update the metadata pointer; metadata loss, expiry and malformed payload must be UNKNOWN/ERROR instead of FRESH; unchanged digest reuses index.
- [ ] **Step 3: Run tests against PR #29.** `python -m pytest tests/unit/test_shared_traffic_cache.py tests/unit/test_traffic_routing.py -q -x`. Record failure cases.
- [ ] **Step 4: Correct discovered defects.** No change of road-matching threshold, cache freshness or exception masking without an explicit invariant test.
- [ ] **Step 5: Benchmark with and without index.** Three or more repeated idle-host runs of full-feed route assessment; record p50/p95 latency, index construction time, memory and output equality, without promising a target before measurement.
- [ ] **Step 6: Verify manifests.** `docker compose config`, `kubectl kustomize .\k8s`; inspect Redis noeviction capacity and memory margin.
- [ ] **Step 7: Commit PR #29 corrections and evidence.**

**Acceptance:** same decisions as the previous full scan; cache issues are explicit; resource use is measured rather than assumed.

### Task 6: Verify uncertain PostgreSQL commit safety (G2)

**Files:**
- Review/modify in PR #29: `src/ev_fleet/common/persistence.py`, `src/ev_fleet/common/logging.py`, `src/ev_fleet/dispatch/service.py`, `src/ev_fleet/fleet/store.py`
- Tests: `tests/unit/test_persistence_fail_closed.py`, `tests/integration/test_telemetry_postgres.py`, `tests/unit/test_dispatch_lifecycle.py`

**Interfaces:**
- Consumes: `SnapshotRepository.save(payload, *, telemetry_sample=None)` and `SnapshotRepository.ready/status`.
- Produces: explicit write uncertainty handling, preserved durable claim ownership, recoverable readiness.

- [ ] **Step 1: Run a failure-injection test against PR #29.** Simulate network loss after PostgreSQL executes a transaction, cancellation mid-save and repeated calls after `faulted`. Expect HTTP 503 on state endpoints while liveness remains available.
- [ ] **Step 2: Verify startup/restart semantics on disposable PostgreSQL.** Check durable snapshots are loaded before readiness; uncertain write does not trigger a blind rewrite on shutdown; a retry with the same dispatch request ID does not double-claim.
- [ ] **Step 3: Run integration tests.** `python -m pytest tests/unit/test_persistence_fail_closed.py tests/unit/test_dispatch_lifecycle.py -q -x`; execute `tests/integration/test_telemetry_postgres.py` only against disposable `TELEMETRY_TEST_DATABASE_URL`.
- [ ] **Step 4: Implement minimal fixes if any test fails.** No deletion of real PVC or rollback of an ambiguous committed claim.
- [ ] **Step 5: Record reconciliation trace and commit to PR #29.**

**Acceptance:** on ambiguous commit, no new claim is granted using uncertain in-memory state; normal operation resumes only after known durable reload/reconciliation.

### Task 7: Controlled integration acceptance and PR #29 decision (G2)

**Files:**
- Test existing: `scripts/test-local.ps1`, `scripts/start-kubernetes.ps1`, `compose.yaml`, `k8s/`
- Create: `docs/evidence/2026-11-04/pr29-acceptance.md`

**Interfaces:**
- Consumes: Tasks 3–6 and the exact PR #29 head SHA.
- Produces: merge decision supported by test and real process/cluster evidence.

- [ ] **Step 1: Run full Python suite** on exact PR revision; record exit code and warning count, not just successful lines.
- [ ] **Step 2: Run frontend type/build as relevant** because PR #29 edits `dashboard/src/App.tsx`; `npm.cmd --prefix dashboard run build`.
- [ ] **Step 3: Run disposable Compose smoke** using documented `docker compose --profile relocation up --build -d --wait` and cleanup. First check provider configuration: with `ROUTING_PROVIDER=SIMULATED`, production Dispatch must fail closed (`ONEMAP_ROUTE_REQUIRED`), so do **not** assert that EV movement succeeds. Exercise complete approval/ack/arrival only with a valid OneMap setup; alternatively use an explicitly isolated test-only route fixture and label it accordingly.
- [ ] **Step 4: Run safe local Kubernetes smoke** only when zero active rentals, relocations and charging tasks. Inspect Pod events/logs/restarts, 20-EV idle telemetry, Redis feed status, PostgreSQL readiness and operational flags.
- [ ] **Step 5: Check explicit block/recovery scenarios.** Unknown traffic, redis down, Fleet communication failure, duplicate approval, parking full. Never infer correctness from green liveness alone.
- [ ] **Step 6: Record PASS/FAIL/BLOCKED per check and peer-review change impact.** Merge PR #29 only if all safety-critical checks pass; otherwise fix or defer it and proceed with submission baseline.
- [ ] **Step 7: Commit the acceptance report** to the documentation branch or accepted PR as appropriate.

**Acceptance:** safe real process-level dispatch trace exists and all untested environmental gaps are disclosed.

### Task 8: Review, integrate and visually validate PR #30 (G3)

**Files:**
- PR #30: `dashboard/src/App.tsx`, `dashboard/src/components/FleetPulse.tsx`, `dashboard/src/components/Panels.tsx`, `dashboard/src/polish.css`, `dashboard/src/main.tsx`
- Tests: `dashboard/tests/FleetPulse.test.tsx`, `dashboard/tests/App.test.tsx`, `dashboard/e2e/`
- Evidence: `docs/evidence/2026-11-04/dashboard-review.md`

**Interfaces:**
- Consumes: the backend's `effective_availability`, `stale`, `connectivity`, dispatch states and demand freshness.
- Produces: a readable UI which makes no independent safety or readiness decisions.

- [ ] **Step 1: Rebase PR #30 after backend integration.** Reconcile `App.tsx` with PR #29's pending/rejected/unknown dispatch handling; never resolve by replacing backend safety code with the earlier frontend file.
- [ ] **Step 2: Run dashboard TypeScript build** `npm.cmd --prefix dashboard run build`; expected exit 0.
- [ ] **Step 3: Run Vitest where supported.** `npm.cmd --prefix dashboard test -- --run` on GitHub Linux and on Windows. If workers time out before tests execute, mark Windows BLOCKED and diagnose separately; don't claim passing from a build.
- [ ] **Step 4: Add/revise tests for uncertain approvals and fresh-zero vs missing-data counters.** For stale Fleet / partial Demand, `FleetPulse` must render unknown not zero; HTTP 202 with `REJECTED` must not show 'approval recorded'.
- [ ] **Step 5: Run Playwright operator flows** using `npm.cmd --prefix dashboard run test:e2e` with dependencies/browser provisioned per project docs; inspect map, approve, reject, unavailable and reconnect paths.
- [ ] **Step 6: Manual visual review.** Capture light/dark at 1440, 1024, 768 and 390 px; keyboard-only navigation, screen reader status, alert contrast, long text and responsive map.
- [ ] **Step 7: Commit any focused UI fixes, review source diff, and merge PR #30 only on evidence.**

**Acceptance:** the operator sees accurate status and can review/approve with no misleading messages; both mode layouts remain usable.

## Handoff

After Task 8, freeze `main` and its exact SHA for the evaluation plan. If a safety issue remains, do not paper it over with a GUI banner or timeout increase. If the deadline forces a choice, submit a truthful, reproducible limited feature set and document what remained blocked.
