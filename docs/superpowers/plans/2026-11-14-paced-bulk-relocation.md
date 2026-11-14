# Paced bulk relocation implementation plan

> **For agentic workers:** Use superpowers:subagent-driven-development per task; preserve this checkout and all prior uncommitted work. No commits or live rollout.

**Goal:** Accept one human approval, durably queue and safely complete a feasible bulk relocation with automatic transient retries.
**Architecture:** Per-dispatch durable approval intents plus small batch-job metadata; a bounded single admission worker reuses existing claim reconciliation. Rebalancing verifies queued intent with Dispatch before fresh CRDS checks. Dashboard polls job progress.
**Tech Stack:** FastAPI, asyncio, PostgreSQL, React, pytest, Vitest.
**Spec:** docs/superpowers/specs/2026-11-14-paced-bulk-relocation-design.md

- [x] 1. Backend queue, durable schema integration, global cap/backoff/expiry/cancellation, prompt HTTP acceptance and restart tests. Owner: backend implementer. Files: dispatch batch_jobs.py/service.py/store.py/app.py, contracts dispatch.py/recommendations.py, settings if needed, queue tests.
- [x] 2. Verified approved-intent CRDS revalidation. Owner: root. Files: contracts/crds.py, rebalancing/app.py and focused tests.
- [x] 3. Clear queued progress and polling. Owner: frontend implementer. Files: dashboard API contracts, App batch lifecycle, BatchPanel, approval messages and focused tests. Preserve incumbent design.
- [x] 4. Cross-task review and isolated 50-EV paced completion; full local checks; record limits/results. Prepare concrete safe local upgrade only after success.

## Ledger

User explicitly chose gradual background starts with automatic transient retry.
Previous storage phase: full local backend584 + dashboard75 and both builds passed;
28 disposable storage/renewal contracts passed. Simultaneous50 capacity failed;
those failures motivate pacing and remain documented. Keep all durability changes.


Root verification: nine CRDS authorization tests pass (valid queued intent after
original TTL, forged IDs, expiry, naïve timestamp, content deadline, changed policy
and model). Nineteen combined intent/legacy progress regressions passed. A real
PostgreSQL queue reload retained exact IDs and admitted both children once after
the original recommendation expired. Frontend focused52 and build passed; a final
full dashboard run is underway. Queue worker17 cases passed before a final genuine
rejection cleanup regression. Capacity run uses fifty real guarded nodes in five
processes, max4 active claims,30x simulated travel, unchanged2s RPC/30s leases.


Paced capacity result: 50/50 SUCCEEDED after one human approval, at most4 active,
acceptance1.006s, completion231.46s with30x synthetic travel. Two transient simulator
RETRYING transitions recovered automatically; no RECOVERY_WAIT or manual retry.
Completed checkpoint responses p95=558.13ms,p99=827.56ms,max=1203.02ms (2s deadline).
The image predates a genuine-rejection cleanup fix and a duplicate-POST response
publication fix; those paths were not exercised by the happy-path50 run and have
independent failing-then-passing unit regressions. No further capacity rerun is
needed solely for those paths. Full current regression checks remain pending.

Ruling: replace the superseded50-simultaneous admission test with the paced50
completion test, since the user explicitly chose gradual starts and all admission
paths now enforce max4 by default. Retain the isolated50-write storage comparator
and all prior failed measurements in artifacts/docs; never claim simultaneous50
capacity. No request deadline or ownership guard was relaxed.


Final verification: Windows scripts/test-local.ps1 passed613 backend tests
(30 opt-in PostgreSQL skips),86 dashboard tests and both frontend TypeScript/Vite
builds. All29 disposable PostgreSQL storage/queue checks passed separately, plus
the paced50 completion test. Production backend dependency check and Fleet/
Dispatch/Rebalancing startup passed without network or existing DB; dashboard
image served health, HTML and updated assets. Kubernetes server dry-runs passed
for the four proposed image changes. A20.7MB read-only backup and its catalog were
validated. Seven existing paused journeys passed resume/end assessments; no
ownership/journey action has been taken. Images and proposal are recorded in
docs/paced-local-upgrade.md and the task artifacts. Final source review has no
important findings. Browser and Pyright/Pylance checks are NOT VERIFIED because
those tools are unavailable. No staging, commits, pushes or live upgrade.


Authorized local rollout completed: Rebalancing, Fleet, Dispatch and dashboard use
the built bulk-local images; Fleet/Dispatch legacy promotion succeeded before
readiness. Preserved seven existing claims/commands/checkpoints, then resumed
four and waited for SUCCEEDED+claim release before resuming three. All seven
completed. Final fleet:20ONLINE/RUNNING,15available,zeroactiveclaims; five retain
pre-existing charging/stopped-workflow restrictions. Local dashboards reachable;
live read-only OneMap25.688km route succeeded. No new test dispatches, simulator
controller edits, commits or pushes. Pre/post operational artifacts and backups
are retained outside the repository. Documentation-only closeout requires diff
check, not a repeat of unchanged implementation tests.
