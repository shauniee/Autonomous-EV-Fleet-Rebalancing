# Fresh Boot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add one Windows `-FreshBoot` command that recreates the local runtime, reloads `.env`, rebuilds images, replaces project Pods, and preserves PostgreSQL data.

**Architecture:** Extend the existing Windows setup and Kubernetes deployment path rather than introduce a wrapper. Fresh boot adds an explicit `.venv` recreation mode and an explicit file-only configuration mode, then uses the existing safety gates, image build, manifest application, and rollout verification while also restarting PostgreSQL and Redis Pods against their existing storage.

**Tech Stack:** Windows PowerShell 5.1, Python 3, pytest, Docker Desktop, Kubernetes/kubectl, Kustomize

**Spec:** `docs/superpowers/specs/2026-11-06-fresh-boot-design.md`

## Global Constraints

- Preserve the `ev-fleet` namespace, PostgreSQL PVCs, snapshots, and records.
- Remove only the resolved project `.venv`; never delete a broad or unresolved path.
- Never print, persist, or commit rendered Secret values.
- Fresh boot reads whitelisted integration values from the saved `.env` without process-environment overrides.
- Retain the existing fail-closed checks for rentals, relocations, charging, database health, Docker context, and Kubernetes context.
- Do not prune old Docker images or change macOS/Linux startup behavior.
- Do not commit changes unless the user separately authorizes a commit.

## Review Focus

- A stale process-level OneMap token must not override the fresh-boot `.env` token; Task 1 adds unit and CLI coverage.
- A `.venv` directory, junction, or missing environment must be replaced only at the exact project path; Task 2 adds guarded removal and PowerShell syntax verification.
- Active EV workflows must still block fresh boot before destructive runtime replacement; Task 2 reuses the existing safety gates and Task 3 verifies the live path fails closed if the cluster is busy.
- PostgreSQL must receive a new Pod without losing its PVC or records; Task 3 records the PVC identity and read-only database evidence before and after the live check.
- A failed data-service or application rollout must stop the command with the responsible workload; Task 2 uses the existing checked restart and dependency-ordered `Wait-Rollout` path.

---

### Task 1: File-only Secret Rendering

**Files:**
- Modify: `scripts/render_facilities_secret.py:24-75`
- Modify: `tests/unit/test_facilities_env.py`

**Interfaces:**
- Produces: `settings(path: Path, defaults: dict = DEFAULTS, use_environment: bool = True) -> dict`
- Produces: CLI flag `--file-only`, which sets `use_environment=False` without changing existing default behavior.

- [ ] **Step 1: Write failing loader and CLI tests**

Add tests proving `use_environment=False` returns the file token despite a conflicting process variable, the default still honors environment overrides, and `--routing --file-only` emits JSON containing the fixture file value rather than the conflicting fixture environment value.

- [ ] **Step 2: Run the focused tests and confirm the new cases fail**

Run: `.\.venv\Scripts\python.exe -m pytest tests\unit\test_facilities_env.py -q`

Expected: FAIL because `settings` does not accept `use_environment` and `--file-only` is not recognized.

- [ ] **Step 3: Implement the file-only interface**

Update `settings` to apply `os.environ` only when `use_environment` is true. Recognize and remove `--file-only` from CLI arguments before resolving the `.env` path, then pass `use_environment=not file_only`. Keep the whitelist and validation unchanged.

- [ ] **Step 4: Run the focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\unit\test_facilities_env.py -q`

Expected: all tests PASS and no fixture secret is printed.

### Task 2: Fresh Runtime Orchestration

**Files:**
- Modify: `scripts/setup-local.ps1:1-117`
- Modify: `scripts/start-kubernetes.ps1:1-362`

**Interfaces:**
- Consumes: renderer CLI flag `--file-only` from Task 1.
- Produces: `scripts/setup-local.ps1 -Recreate` for exact-path `.venv` replacement.
- Produces: `scripts/start-kubernetes.ps1 -FreshBoot` as the single user command.

- [ ] **Step 1: Extend `setup-local.ps1` with guarded recreation**

Add `[switch]$Recreate`. Resolve the project root, verify the removal target is exactly `<project>\.venv`, remove only that target using the existing link-aware behavior, then follow the existing create/install path. Keep `-Force` as reinstall-only behavior.

- [ ] **Step 2: Add fresh-boot mode validation and setup invocation**

Add `[switch]$FreshBoot` to `start-kubernetes.ps1`; reject combinations with `-ConnectOnly` and `-CheckPostgresOnly`. In deploy mode, invoke `setup-local.ps1 -Recreate` only for fresh boot and retain normal setup otherwise.

- [ ] **Step 3: Make fresh boot render from `.env` only**

Extend `Render-EnvArtifact` with `[switch]$FileOnly`, append `--file-only` when set, and pass it for every facilities, routing, traffic-control, and traffic-config render during fresh boot.

- [ ] **Step 4: Replace all project Pods while preserving data**

After manifests and in-memory configuration are applied, explicitly restart `statefulset/postgres` and `deployment/redis` during fresh boot. Let uniquely tagged images and the existing configuration-consumer restarts replace application Pods. Reuse the existing dependency-ordered waits so PostgreSQL and Redis become healthy before application readiness is accepted.

- [ ] **Step 5: Parse both PowerShell scripts**

Run PowerShell's `System.Management.Automation.Language.Parser.ParseFile` against `scripts/setup-local.ps1` and `scripts/start-kubernetes.ps1`.

Expected: zero parser errors for both files.

### Task 3: Operator Documentation and Verification

**Files:**
- Modify: `README.md:23-85`
- Modify: `docs/kubernetes-local.md:21-61`

**Interfaces:**
- Consumes: `scripts/start-kubernetes.ps1 -FreshBoot` from Task 2.
- Produces: one documented, copy-pasteable fresh-boot command and its data-safety boundary.

- [ ] **Step 1: Document the command and exact behavior**

Add `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -FreshBoot` to both documents. State that it recreates `.venv`, ignores inherited overrides for whitelisted `.env` values, rebuilds images, replaces all project Pods, preserves PostgreSQL PVC data, and does not prune images or recreate the cluster.

- [ ] **Step 2: Run the required local suite**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1`

Expected: backend, dashboard, and simulation-console checks PASS.

- [ ] **Step 3: Run the required disposable integration suite**

Run: `powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 -Integration`

Expected: Docker engine, disposable PostgreSQL telemetry, Compose smoke, and Kubernetes manifest checks PASS. If Docker Desktop or `act` is missing, report integration as NOT VERIFIED rather than passed.

- [ ] **Step 4: Verify fresh boot against local Docker Desktop Kubernetes**

Before running, record the current `postgres-data-postgres-0` PVC identity and a read-only PostgreSQL/Fleet state check without printing credentials. Run the fresh-boot command only if the existing safety gate reports no active workflows. Observe successful workload rollouts and dashboard health, then stop only newly created port-forward jobs with `Ctrl+C`. Confirm the PVC identity and read-only state remain present afterward.

- [ ] **Step 5: Review the final diff**

Run: `git diff --check` and `git status --short`

Expected: no whitespace errors; only the approved scripts, tests, documentation, design, and plan are changed, while the user's existing `.vscode/` files remain untouched.
