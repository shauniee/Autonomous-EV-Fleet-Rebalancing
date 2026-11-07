# Fresh Boot Command Design

## Intent

Add one Windows command for rebuilding the complete local EV Fleet runtime when
generated environments, container images, Pods, or Kubernetes credentials may
be stale. The command must preserve PostgreSQL persistent data.

## Goals

- Add `-FreshBoot` to `scripts/start-kubernetes.ps1`.
- Recreate `.venv` from scratch and reinstall the declared Python dependencies.
- Treat the saved project `.env` as authoritative during a fresh boot so stale
  inherited PowerShell variables cannot override whitelisted Kubernetes values.
- Build new uniquely tagged backend, dashboard, and simulation-console images.
- Reapply manifests, ConfigMaps, and generated Secrets without printing secret
  values or writing them to the repository.
- Replace every project Pod, including PostgreSQL and Redis Pods, then wait for
  data services and application workloads in dependency order.
- Retain the existing checks that block restarts during rentals, relocations, or
  charging workflows.
- Finish only when the expected workloads are Ready, or fail with the workload
  that prevented completion.

## Data-Safety Boundary

Fresh boot will not delete the `ev-fleet` namespace, PVCs, PostgreSQL records,
snapshots, or user-created source/configuration files. PostgreSQL may receive a
new Pod, but it will mount the existing PVC. The command will not prune old
Docker images automatically because that removes rollback material and is not
required to create fresh images.

## Approach

The selected approach extends the existing Windows deployment script instead of
adding a second orchestration path.

- `scripts/setup-local.ps1` gains an explicit recreation mode that removes only
  the resolved project `.venv` directory, recreates it, and reinstalls declared
  dependencies.
- `scripts/render_facilities_secret.py` gains an explicit file-only option. Its
  existing environment-override behavior remains available outside fresh boot,
  while fresh boot uses values read from `.env` only.
- `scripts/start-kubernetes.ps1 -FreshBoot` invokes those modes, follows the
  existing image-build and manifest-apply path, forces replacement of data and
  application Pods, and uses the existing rollout/readiness checks.

Alternatives considered were a separate wrapper script, which could drift from
the main deployment path, and namespace recreation, which conflicts with PVC
preservation. Both were rejected.

## Verification

- Add unit coverage proving file-only rendering ignores conflicting inherited
  environment values without exposing secrets.
- Exercise PowerShell syntax and existing local checks through
  `scripts/test-local.ps1`.
- Because this changes Kubernetes/deployment behavior, also run
  `scripts/test-local.ps1 -Integration` with Docker Desktop and `act`. If an
  integration prerequisite is unavailable, report that suite as not verified.
- Do not run fresh boot against production or non-local data.

## Non-Goals

- Resetting PostgreSQL, deleting PVCs, or clearing application records.
- Recreating Docker Desktop's Kubernetes cluster or restarting Docker Desktop.
- Pruning the Docker image cache or other unrelated generated files.
- Changing macOS/Linux startup behavior in this Windows-focused change.
