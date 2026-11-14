# Charging Cable Handoff Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans task by task, with test-first implementation and a fresh final code review.

**Goal:** Enforce and explain human cable connection and disconnection around simulated charging.
**Architecture:** Facilities owns allocation-bound durable cable receipts; Movement follows verified parking state. Fleet validates charging evidence and keeps attached/waiting EVs ineligible. Charging operations displays state and records named team confirmations.
**Tech Stack:** Existing Python/FastAPI/Pydantic, React/TypeScript/Vitest and Playwright.
**Spec:** ../specs/2026-11-12-charging-cable-handoff-design.md

## Global Constraints
Preserve human dispatch approval, default 80% charging target and existing dashboard identity. No dependencies, migrations, deployment, staging, commits or remote changes. Use the current checkout on a local feature branch.

## Review Focus
- Lost responses must retry one durable request without another physical transition.
- Old allocations, missing observations and overlapping relocation must not authorize a cable action.
- Restart must preserve cable decisions without charging during downtime.
- Charging completion must not make a plugged-in vehicle rentable or dispatchable.
- Network failures and stale Fleet data must leave actions disabled and explain recovery.

### Task 1: Allocation-bound handoff and physical gates
Files: contracts/facilities.py, contracts/vehicles.py, facilities/charging.py, facilities/app.py, facilities/store.py, simulator/movement.py, fleet/store.py, fleet/recovery.py, simulator/recovery.py, rebalancing policy/evidence.
Interfaces: POST /api/v1/vehicles/{vehicle_id}/charging-cable accepts allocation dispatch_id, request_id, CONNECT/DISCONNECT and staff_name; VehicleParking exposes session and cable state.
- [x] Write and run failing API and movement lifecycle tests.
- [x] Implement idempotent durable transitions, fresh Fleet validation, rollback, physical gates and recovery validation.
- [x] Run charging, movement, parking, eligibility and recovery regressions; update fixtures to include explicit connection evidence where charging is intended.

### Task 2: Ground-team interface
Files: dashboard/src/components/ChargingHandoffs.tsx, ChargingOperations.tsx, App.tsx, api/contracts.ts, workspace.css; dashboard/tests/ChargingHandoffs.test.tsx.
Interfaces: existing vehicle-parking snapshot and cable endpoint; consume current Fleet vehicles, notify and refresh callbacks.
- [x] Write and run failing UI tests for connection, target/disconnection, stale/errors and idempotent retry.
- [x] Add a responsive stage/action view with team attribution and disabled/loading/error states; preserve the separate manual task log.
- [x] Run frontend tests and builds, and one batched desktop/mobile/light/dark browser review.

### Task 3: Verify and document
- [x] Run full backend tests, dashboard tests/build and console build; review the diff and whitespace.
- [x] Obtain an independent review and resolve material findings with regression tests.
- [x] Document the walkthrough, simulation boundary and actual verification limitations. Leave changes uncommitted.
