# User-Controlled Demand Simulation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make zone-demand generation start paused and run only after an explicit Simulation Console action.

**Architecture:** The Demand service owns one guarded background task and exposes idempotent start/pause operations. The local Simulation Console proxies those operations and renders their status; Kubernetes supplies a paused default without restarting or scaling the Demand service.

**Tech Stack:** Python, asyncio, FastAPI, httpx, React, TypeScript, Vitest, Testing Library, Kubernetes ConfigMap

**Spec:** `docs/superpowers/specs/2026-11-08-user-controlled-demand-simulation-design.md`

## Global Constraints

- Demand simulation starts paused until an operator explicitly starts it.
- The operator's last explicit Start/Pause choice survives Demand service and Pod restarts.
- Start performs one immediate tick and then ticks at the configured interval; pause preserves current observations.
- Start and pause are idempotent and cannot create duplicate tasks.
- Manual demand holds, customer rentals, demand ranges, and rebalancing behavior remain unchanged.
- Do not deploy, mutate the live cluster, stage, commit, or push.

## Review Focus

- Concurrent or repeated start requests still create exactly one simulation loop; cover in Task 1.
- Pause during the interval cancels promptly and no later tick changes demand; cover in Task 1.
- A failed console-to-Demand request reports unavailable without claiming the requested action succeeded; cover in Task 2.
- UI polling reflects an externally changed simulation state and does not leave stale button state; cover in Task 3.
- Manual demand present at start remains protected until its hold expires; retain and rerun the existing hold test in Task 1.

---

### Task 1: Demand simulation lifecycle and API

**Files:**
- Modify: `src/ev_fleet/demand/simulation.py`
- Modify: `src/ev_fleet/demand/app.py`
- Modify: `tests/unit/test_demand_simulation.py`
- Modify: `tests/integration/test_heatmap_api.py`

**Interfaces:**
- Produces: `DemandSimulator.start() -> dict`, `DemandSimulator.pause() -> dict`, and `DemandSimulator.close() -> None`.
- Produces: `POST /api/v1/demand-simulation/start` and `POST /api/v1/demand-simulation/pause`, each returning `DemandSimulationStatus`.

- [ ] **Step 1: Write failing lifecycle tests**

Add tests proving initial paused status, immediate first tick on start, repeated/concurrent start idempotence, pause preservation, no post-pause tick, and close cleanup. Keep the existing manual-hold test in the focused run.

- [ ] **Step 2: Run the focused tests and verify the new cases fail**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_demand_simulation.py -q`

Expected: new lifecycle assertions fail because start/pause/close do not exist.

- [ ] **Step 3: Implement the guarded lifecycle**

Use one internal `asyncio.Lock` and one task reference. `start()` enables the generator, performs the immediate tick only on the transition from paused, creates one loop task, and returns status. `pause()` disables and cancels/awaits that task without modifying stored demand. `close()` delegates to pause. Keep `tick()` and manual-hold semantics unchanged.

- [ ] **Step 4: Wire lifespan and mutation endpoints**

Make lifespan call `start()` only when its startup setting is enabled and always call `close()` during shutdown. Add the two response-model-validated POST endpoints.

- [ ] **Step 5: Add API integration assertions**

Verify status is paused before start, start immediately supplies demand, repeated start stays running without a second loop, pause preserves the snapshot, and a subsequent start resumes.

- [ ] **Step 6: Run Demand tests**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_demand_simulation.py tests\integration\test_heatmap_api.py -q`

Expected: all selected tests pass.

### Task 2: Simulation Console Demand proxy

**Files:**
- Modify: `simulation-console/backend/demand.py`
- Create: `tests/unit/test_simulation_console_demand.py`

**Interfaces:**
- Consumes: the Demand service status/start/pause endpoints from Task 1.
- Produces: `GET /api/probe/v1/demand-simulation/status` and `POST /api/probe/v1/demand-simulation/{action}` for `start|pause`.

- [ ] **Step 1: Write failing proxy tests**

Load the console demand module using the established `importlib.util` pattern. Assert valid status passthrough, exact start/pause forwarding without retries, invalid action rejection, Demand HTTP failure mapping, timeout mapping, and invalid-response rejection.

- [ ] **Step 2: Run the focused proxy tests and verify failure**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_simulation_console_demand.py -q`

Expected: tests fail because the proxy endpoints do not exist.

- [ ] **Step 3: Implement validated proxy operations**

Add a strict Pydantic status model and bounded no-retry GET/POST forwarding through `request.app.state.client` and the existing Demand service URL. Return clear `502` responses for invalid upstream data and `503` for transport failure.

- [ ] **Step 4: Run the focused proxy tests**

Run: `.venv\Scripts\python.exe -m pytest tests\unit\test_simulation_console_demand.py -q`

Expected: all selected tests pass.

### Task 3: Demand workspace controls and paused Kubernetes default

**Files:**
- Create: `simulation-console/frontend/src/DemandSimulationControl.tsx`
- Modify: `simulation-console/frontend/src/main.tsx`
- Modify: `k8s/configmaps.yaml`
- Create: `dashboard/tests/SimulationDemandControl.test.tsx`

**Interfaces:**
- Consumes: the console proxy endpoints from Task 2.
- Produces: a self-polling `DemandSimulationControl` with Start/Pause buttons and explicit Paused, Running, Starting, Pausing, and Unavailable feedback.

- [ ] **Step 1: Write failing component tests**

Assert initial status polling, correct button disabling, start and pause requests, refreshed status after each action, external-state polling updates, and actionable unavailable/rejected messages.

- [ ] **Step 2: Run the component test and verify failure**

Run: `npm.cmd --prefix dashboard test -- --run SimulationDemandControl.test.tsx`

Expected: fail because the component does not exist.

- [ ] **Step 3: Implement and place the control**

Create the focused component using the Rental controller's bounded fetch/poll pattern, but use `start` and `pause` terminology. Render it above `DemandForm` in the Demand workspace so manual values remain available while paused.

- [ ] **Step 4: Set the deployment default to paused**

Change `DEMAND_SIMULATION_ENABLED` in `k8s/configmaps.yaml` from `"true"` to `"false"`. Do not alter intervals or other simulation settings.

- [ ] **Step 5: Run frontend tests and builds**

Run: `npm.cmd --prefix dashboard test -- --run SimulationDemandControl.test.tsx`

Run: `npm.cmd --prefix dashboard run build`

Run: `npm.cmd --prefix simulation-console/frontend run build`

Expected: all commands pass.

### Task 4: Repository verification

**Files:**
- No additional files.

**Interfaces:**
- Consumes: Tasks 1-3 as one local change set.

- [ ] **Step 1: Run formatting and whitespace validation**

Run: `git diff --check`

Expected: no output and exit code 0.

- [ ] **Step 2: Run the repository local checks**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\test-local.ps1`

Expected: backend tests, dashboard tests/build, and simulation console build pass. Report Docker/PostgreSQL/Kubernetes integration as not run because this change does not require live infrastructure mutation.

- [ ] **Step 3: Review the final diff**

Confirm only the approved demand lifecycle, console control, paused default, tests, and these planning documents changed; preserve all pre-existing uncommitted work.
