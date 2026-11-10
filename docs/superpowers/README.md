# Superpowers — CSC3104 final-delivery hub

**Planning snapshot:** 4 November 2026. **Final coursework deadline:** Sunday **15 November 2026, 23:59**. **Status:** group approval needed; this branch contains documents **only**.

The dated plans preserve their assumptions at the time of writing. For current
policy behavior and the scope of the archived selector evaluation, see [CRDS](../crds.md).

Start here:

1. [Master design and decision record](specs/2026-11-04-csc3104-delivery-design.md) — scope, original-contribution framing, architecture, invariants, release gates and dates.
2. [Stability, tests and PR integration](plans/2026-11-04-stability-testing-and-pr-integration.md) — exact files, acceptance tests and implementation steps for G0–G3.
3. [Strategy evaluation and Kubernetes evidence](plans/2026-11-04-evaluation-and-evidence.md) — reproducible matched trial protocol, failure accounting, metrics and limitations for G4.
4. [Article, testing instructions, video and final package](plans/2026-11-04-course-submission.md) — task-level G5 coursework handoff.

## Task order and merge dependencies

```text
Approve design and task plans
           |
           v
G0: trust baseline & CI ----> G1: diagnose EV availability changes
           |                               |
           +---------------+---------------+
                           |
                           v
G2: verify PR #29 backend routing/cache/persistence safety
                           |
                           v
Merge #29 only if verified -- otherwise use proven limited baseline
                           |
                           v
G3: rebase PR #30, reconcile App.tsx, run UI build/tests/browser checks
                           |
                           v
G4: freeze code/policy and collect two DISTINCT evidence sources:
    (a) in-process paired strategy trials; (b) Kubernetes service metrics
                           |
                           v
G5: six-page article + independent instructions + 10–15 min video
                           |
                           v
Audit all four deliverables and submit no later than 15 Nov 23:59
```

## What we already have

- PR #27 and #28 were merged. The dashboard Vitest 4.1.11 Windows worker startup still times out **before test collection** despite PR #28.
- [PR #29](https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing/pull/29): draft backend safety, routing and Redis/persistence fixes; unverified in deployed runtime.
- [PR #30](https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing/pull/30): draft Impeccable-guided UI refinement; needs browser review and rebasing **after #29**.
- Previous local run reported 245 Python tests passed + 1 skipped **before these draft PRs were validated**.
- The 10-scenario, 30-seed, two-policy comparison harness already exists. Do not replace it with speculative ML weeks before submission.
- Critical OneMap limitation: `ROUTING_PROVIDER=SIMULATED` returns no authoritative road route and production Dispatch deliberately fails with `ONEMAP_ROUTE_REQUIRED`. A real routed demo needs valid OneMap; test-only route fixtures are strictly labelled and isolated.

## Team workflow

Assign group members to: (1) backend/test evidence, (2) dashboard/e2e, (3) experiment/statistical analysis, and (4) report/video/submission. These are suggested *responsibilities*, not assumptions about actual membership. Everyone should read the release-gates design and contribute to the article/video. Each task produces a small reviewed change or independently reproducible evidence. Do not enable a new high-risk feature after 11 November unless it is essential to the safe demonstration.

## First executable actions **after design approval**

1. Run and record a clean baseline and GitHub checks on an exact SHA (Task 1–2, stability plan).
2. Obtain evidence for one intermittent AVAILABLE/UNAVAILABLE transition, without changing the stale threshold (Task 3).
3. Review and execute the focused PR #29 tests in a safe environment before deciding whether to merge (Task 4–7).
4. Check whether the group can provision a valid OneMap token before 6 November; if not, prepare a truthful restricted live demonstration plus isolated offline fixture trace.

**Verification language:** PASS = executed and evidenced; FAIL = executed and failed; BLOCKED = unable to execute; NOT RUN = not attempted. Neither a mergeable label nor a successful TypeScript build implies Vitest tests ran.

## Approval gate

This planning branch proposes the design and task plan. It does **not** approve product-code implementation, merge draft PRs, change the live Kubernetes cluster, or submit coursework. Read the [master design](specs/2026-11-04-csc3104-delivery-design.md), confirm/correct the originality claim and OneMap demo mode, then approve execution.
