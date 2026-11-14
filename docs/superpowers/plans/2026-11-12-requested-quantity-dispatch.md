# Requested quantity dispatch implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax.

**Goal:** Fulfil the operator's requested relocation quantity whenever enough feasible EVs exist.
**Architecture:** Carry an explicit planning mode from the dashboard through saved CRDS metadata and revalidation. Keep existing ranking and all safety checks; coverage becomes advisory in fulfilment mode.
**Tech Stack:** FastAPI/Pydantic, Python, React/TypeScript, pytest and Vitest.
**Spec:** ../specs/2026-11-12-requested-quantity-dispatch-design.md

## Global constraints
Preserve existing work and API default behavior. Retain explicit human approval, route/energy/capacity/freshness/ownership checks. No deployment or live operational mutations; tests use isolated in-memory services only.

## Review focus
- Non-beneficial moves must survive approval revalidation, including the last EV at a donor zone.
- Missing routes, expired evidence and changed battery must remain blocking.
- Partial fulfilment must report physical limits rather than economic policy.
- Positive total gain must not conceal an individually non-beneficial move.
- UI must send explicit intent and never describe a negative score as an improvement.

## Task 1: Planning and approval
Files: contracts/crds.py, contracts/recommendations.py, rebalancing/crds_service.py, rebalancing/app.py, rebalancing/crds_evidence.py; tests/unit/test_requested_quantity.py.
- [x] Add isolated failing tests for six non-beneficial moves, legacy useful-only behavior, capacity shortfall, fresh approval after demand changes, and safety failures.
- [x] Observe intended failures before implementing the explicit mode and server-owned revalidation intent.
- [x] Run focused backend regression tests.

## Task 2: Operator interface and documentation
Files: dashboard/src/App.tsx, api/contracts.ts, components/BatchPanel.tsx, CrdsSummary.tsx, crdsMessages.ts; dashboard/tests; README.md and docs/bulk-approval-consistency.md.
- [x] Add failing assertions for explicit request intent and honest trade-off confirmation.
- [x] Implement mode-specific labels and warnings within existing styling.
- [x] Run dashboard tests/build, backend regression suite, and diff whitespace checks.
- [x] Obtain one fresh code review and address blocking findings.

## Ledger
- User approved scope in chat: fulfil feasible quantities with coverage warnings; retain rental-company POV.
- Review found nearest-site capacity and same-zone counting gaps; add maximum capacity matching and explicit already-in-target evidence. Route verification stays bounded, not exhaustive across every pair.
- Reusing current checkout to preserve the complete uncommitted app; no Git integration actions.

- Verification: 526 existing backend checks passed in the full run. One new charger fixture was invalid (missing connector/power), corrected without production-code changes; final focused run passed all 14 requested-quantity cases, including an additional positive-total warning test.
- Dashboard: 75 tests passed; TypeScript and production build passed. Existing bundle-size warning remains.
- Browser: isolated Edge preview at 1440px/390px in light/dark had no page errors or horizontal page/dialog overflow. Preview files and server removed afterward; captures retained outside the repo.
- Fresh review findings resolved: alternate-site capacity, same-zone counting, and partial-plan copy. No remaining blocking findings.
- Python editor/Pyright diagnostics NOT VERIFIED: neither checker is available in the project environment.
- No deployment, commit, index changes or live operational mutations performed for this feature.
