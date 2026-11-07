# Legacy Parking Road-Access Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Persist a road-matched access coordinate once for legacy occupied holds that lack `access_position`.

**Architecture:** Facilities advertises legacy missing access state and owns an atomic, constrained repair mutation. The idle simulator invokes Dispatch road matching before its first telemetry session, persists the result through Facilities, and then uses the returned parking state.

**Tech Stack:** Python, FastAPI, Pydantic, asyncio, pytest

**Spec:** `docs/superpowers/specs/2026-11-06-legacy-parking-road-access-repair-design.md`

## Global Constraints

- Do not loosen the five-metre route-origin safety limit.
- Repair only occupied holds with missing access coordinates.
- Preserve the 150-metre facility road-access validation.
- Persist before publishing repaired telemetry.
- Do not mutate live Kubernetes or PostgreSQL data.
- Do not overwrite unrelated uncommitted changes.

## Review Focus

- Existing access coordinates cannot be rewritten.
- Missing, released, and reserved holds cannot be repaired.
- A failed persistence or invalid response cannot publish telemetry.
- A repaired hold survives restart and does not road-match again.
- Strict five-metre route-origin coverage remains green.

### Task 1: Add Facilities repair contract tests

**Files:**
- Modify: `tests/unit/test_persistence.py`
- Modify: `src/ev_fleet/contracts/facilities.py`
- Modify: `src/ev_fleet/facilities/store.py`
- Modify: `src/ev_fleet/facilities/app.py`

**Interfaces:**
- Produces: `VehicleParking.access_position_missing`.
- Produces: `FacilitiesStore.repair_vehicle_parking_access(vehicle_id, position)`.
- Produces: `PUT /api/v1/vehicles/{vehicle_id}/parking/access-position`.

- [x] Add persistence, distance, state, and overwrite regression tests first.
- [x] Run the focused tests and confirm failure before implementation.
- [x] Implement the atomic repair and endpoint.
- [x] Run the focused tests until green.

### Task 2: Repair legacy parking during safe simulator startup

**Files:**
- Modify: `tests/unit/test_road_only_movement.py`
- Modify: `src/ev_fleet/simulator/commands.py`

**Interfaces:**
- Consumes: `VehicleParking.access_position_missing` and Facilities repair endpoint.
- Consumes: Dispatch `POST /api/v1/road-matches`.
- Produces: persisted road-aligned position before telemetry-session creation.

- [x] Add a startup regression test proving one road-match and durable repair.
- [x] Run the focused test and confirm failure before implementation.
- [x] Implement legacy repair between parking restoration and session creation.
- [x] Run the focused road-only tests until green.

### Task 3: Verify locally and with integrations

**Files:**
- No additional files.

**Interfaces:**
- Consumes: repository test scripts required by `AGENTS.md`.

- [x] Run `.\scripts\test-local.ps1`.
- [ ] Run `.\scripts\test-local.ps1 -Integration`; if Docker Desktop or `act` is unavailable, report NOT VERIFIED. **NOT VERIFIED:** the script stops because `act` is not installed.
