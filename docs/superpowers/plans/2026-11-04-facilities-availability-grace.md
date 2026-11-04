# Facilities Availability Grace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop transient Facilities timeouts from flickering a verified idle EV to unavailable while preserving fail-closed startup and prolonged-outage behaviour.

**Architecture:** `MovingNode` will timestamp its last successful, currently applicable Facilities verification using monotonic time. A refresh exception retains state only when that verification is at most the configured 10-second grace age; otherwise existing fail-closed clearing remains unchanged. Kubernetes supplies the explicit grace value and the CLI validates it.

**Tech Stack:** Python 3.12, asyncio, pytest, Kubernetes ConfigMap, Windows PowerShell verification scripts

**Spec:** `docs/superpowers/specs/2026-11-04-facilities-availability-grace-design.md`

## Global Constraints

- Default Facilities failure grace is exactly 10 seconds.
- Startup remains unavailable until the first successful Facilities verification.
- Explicit Facilities states and all existing Fleet safety exclusions apply immediately.
- No production or existing Kubernetes/PostgreSQL data may be targeted by tests.

## Review Focus

- First-ever Facilities request fails: EV remains unavailable with no grace.
- One timeout after a verified response: parking and availability remain unchanged within 10 seconds.
- Outage reaches the 10-second boundary and then exceeds it: boundary is retained; excess fails closed.
- A successful response after a failure resets the grace age.
- A valid pending/unverified Facilities response bypasses grace and makes the EV unavailable immediately.

---

### Task 1: Grace-period behaviour

**Files:**
- Modify: `tests/unit/test_road_only_movement.py`
- Modify: `src/ev_fleet/simulator/commands.py`

**Interfaces:**
- Consumes: `MovingNode.refresh_parking()` and `MovingNode.send_telemetry()`
- Produces: `MovingNode(..., facilities_grace_seconds: float = 10)` and bounded monotonic Facilities verification state

- [x] **Step 1: Write failing behavioural tests**

Add focused tests proving startup failure stays unavailable, a verified state survives failures through exactly 10 seconds, a failure after 10 seconds clears parking and availability, successful verification resets the window, and a valid pending response applies immediately.

- [x] **Step 2: Run the focused tests and verify RED**

Run: `python -m pytest tests/unit/test_road_only_movement.py -k "parking_refresh" -q`

Expected: FAIL because `MovingNode` currently clears authority on the first exception and has no grace parameter/state.

- [x] **Step 3: Implement the minimal grace state**

Add and validate `facilities_grace_seconds`, record a monotonic timestamp only when a valid response leaves parking verified, retain state on an exception while its age is `<= facilities_grace_seconds`, and otherwise execute the existing fail-closed transition. Valid pending/unverified responses must clear any prior grace eligibility immediately.

- [x] **Step 4: Run the focused tests and verify GREEN**

Run: `python -m pytest tests/unit/test_road_only_movement.py -k "parking_refresh" -q`

Expected: all selected tests pass.

### Task 2: Runtime configuration and deployment

**Files:**
- Modify: `src/ev_fleet/simulator/cli.py`
- Modify: `k8s/configmaps.yaml`
- Modify: `tests/unit/test_simulator_cli.py` if present, otherwise add CLI coverage to the closest existing simulator test module

**Interfaces:**
- Consumes: `MovingNode(..., facilities_grace_seconds=...)` from Task 1
- Produces: `--facilities-grace-seconds` with `FACILITIES_FAILURE_GRACE_SECONDS` default and Kubernetes value `"10"`

- [x] **Step 1: Write a failing CLI/configuration test**

Verify non-finite or negative grace values are rejected, zero is allowed to restore immediate fail-closed behaviour, and the Kubernetes ConfigMap supplies `FACILITIES_FAILURE_GRACE_SECONDS: "10"`.

- [x] **Step 2: Run the test and verify RED**

Run the exact owning pytest module with `python -m pytest <module> -q`.

Expected: FAIL because the CLI option and ConfigMap value do not exist.

- [x] **Step 3: Wire the configuration**

Parse the CLI option as a float, default it from `FACILITIES_FAILURE_GRACE_SECONDS` or `10`, validate it as finite and non-negative, and pass it through `run_node` to `MovingNode`. Add the Kubernetes ConfigMap value.

- [x] **Step 4: Run the owning tests and verify GREEN**

Run the exact owning pytest module and the focused parking tests; expect all pass.

### Task 3: Repository verification

**Files:**
- Verify only

**Interfaces:**
- Consumes: completed Tasks 1-2
- Produces: local and integration verification evidence

- [x] **Step 1: Run required local verification**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1`

Expected: exit code 0 with no test failures.

- [x] **Step 2: Run required deployment-sensitive integration verification**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 -Integration`

Expected: exit code 0. If Docker Desktop or `act` is unavailable, report the integration suite as NOT VERIFIED rather than passed.

Result: NOT VERIFIED because `act` is not installed. A direct client-side Kubernetes dry run validated the edited ConfigMaps.

- [x] **Step 3: Review the final diff**

Confirm only the approved grace behaviour, configuration, tests, design, and plan are changed; do not modify unrelated user work.
