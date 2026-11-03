# Autonomous EV Fleet Rebalancing - Singapore

This repository contains a distributed, simulation-only system for observing an
electric-vehicle fleet and helping a human dispatcher rebalance available vehicles
between Singapore zones.

The system combines simulated demand, vehicle telemetry, parking capacity, charging
information and explicit operator approval. It does not control real vehicles.
Optional integrations can provide real parking, charging, routing and traffic
observations, but movement and operational decisions remain simulated.

## Start the application

### Prerequisites

- Docker Desktop using Linux containers
- Docker Desktop Kubernetes enabled
- `kubectl` with the `docker-desktop` context
- Windows PowerShell
- Python 3 available through `py -3` or `python`
- Host ports 8080 and 8090 available

From the repository root, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

This single command:

1. Runs `scripts/setup-local.ps1` to validate or create `.venv` and install runtime
   dependencies when the dependency definition changes.
2. Validates the local Docker Desktop Kubernetes context.
3. Checks that an existing PostgreSQL database is reachable without deleting data.
4. Refuses to redeploy while rentals, relocations or charging work are active.
5. Validates permitted integration settings from `.env` without printing secrets.
6. Builds uniquely tagged backend, operator-dashboard and simulation-console images.
7. Applies the Kubernetes resources and waits for every workload to roll out.
8. Opens or reuses the two local dashboard tunnels.

After startup:

- Operator dashboard: <http://127.0.0.1:8080>
- Simulation console: <http://127.0.0.1:8090>

Keep the PowerShell window open when it created new port-forward sessions. Press
`Ctrl+C` to close only those tunnels. PostgreSQL data remains on the
`postgres-data-postgres-0` persistent-volume claim.

To reconnect to an existing deployment without rebuilding or restarting it:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -ConnectOnly
```

To check only the existing PostgreSQL StatefulSet:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -CheckPostgresOnly
```

For the complete procedure and troubleshooting, see
[Local Kubernetes deployment](docs/kubernetes-local.md).

## Local Python environment

`scripts/setup-local.ps1` is called automatically during a full Windows Kubernetes
startup. It can also be run directly:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

The script checks that `.venv\Scripts\python.exe` starts successfully. If the
environment is missing or references a removed Python installation, it recreates the
project-local environment. It installs from a root `requirements.txt` when present;
otherwise it installs the project described by `pyproject.toml`.

A SHA-256 marker inside `.venv` prevents dependency installation on every startup.
Installation runs again when the selected dependency file changes or when setup is
forced:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1 -Force
```

## Runtime components

Each backend process owns a distinct part of the simulation and communicates with
other components through HTTP APIs.

| Component | Code | Responsibility | Local port |
|---|---|---|---:|
| Fleet Service | `src/ev_fleet/fleet/` | Vehicle registration, ordered telemetry, freshness, rentals and atomic claims | 8001 |
| Demand Service | `src/ev_fleet/demand/` | Zone demand, supply projections, shortages and heat-map composition | 8002 |
| Rebalancing Service | `src/ev_fleet/rebalancing/` | Eligibility, scoring, ranking and short-lived recommendations | 8003 |
| Dispatch Service | `src/ev_fleet/dispatch/` | Approval, command lifecycle, acknowledgements and reconciliation | 8004 |
| Facilities Service | `src/ev_fleet/facilities/` | Parking locations, charger observations, capacity holds and allocations | 8005 |
| EV simulators | `src/ev_fleet/simulator/` | Position, battery state, command polling and simulated movement | None |
| Operator dashboard | `dashboard/` | Map, recommendations, dispatch review and operational status | 8080 |
| Simulation console | `simulation-console/` | Demand input, workload controls and service observations | 8090 |

Shared models and utilities are intentionally separate:

- `src/ev_fleet/contracts/` contains validated API request and response models.
- `src/ev_fleet/common/` contains shared settings, clocks, logging and travel calculations.
- A service does not import another service's private store or repository.

## Main workflows

### Vehicle relocation

```text
Fleet telemetry + zone demand
        -> operator selects a shortage zone
        -> Rebalancing ranks eligible EVs
        -> operator reviews a recommendation
        -> Dispatch receives explicit approval
        -> Fleet atomically claims the EV
        -> simulator acknowledges and moves
        -> Dispatch verifies arrival
        -> Fleet releases the EV in the destination zone
```

A recommendation is advisory. It does not reserve or move a vehicle. Only an
explicit Dispatch approval can begin relocation.

The operator can request one EV or a batch of up to 20. Batch recommendations rank
vehicle-to-facility pairs, respect simulated parking capacity and may return partial
fulfilment. Approval revalidates both vehicle eligibility and parking availability.

### Demand and shortages

The simulation console supplies deterministic manual demand for Jurong East,
Woodlands, Bishan, Punggol, Tampines and CBD. The Demand Service combines this with
fresh Fleet observations to calculate:

- available and projected vehicle supply;
- current and projected shortages;
- confirmed and unconfirmed inbound relocations;
- data-quality warnings and hotspot severity.

Timestamps use UTC in APIs and Asia/Singapore in the dashboards. Distances are in
kilometres, energy in kWh and state of charge as a percentage.

### EV selection

Rebalancing excludes vehicles with missing or stale telemetry, offline connectivity,
an active claim or rental, insufficient predicted arrival charge, or another
conflicting operation. Eligible vehicles receive an explainable score:

```text
35% distance utility
20% current state of charge
35% predicted arrival reserve
10% charging suitability
```

The highest score wins, followed by deterministic tie-breaks. A nearest-eligible-EV
strategy exists only as an experimental comparison; the application uses the
energy-aware strategy.

### Rentals and charging

Fleet records idempotent rental starts and confirmed returns. A rented vehicle is
ineligible for relocation even if its ordinary telemetry says `AVAILABLE`. Returns
use fresh telemetry received after rental start and remain visible after service
restart.

Charging alerts and ground-staff tasks are operator-reviewed simulation features.
They do not connect to a real charging provider or physically charge a vehicle.

See [Rental forecasts and charging operations](docs/rental-forecast-charging-ops.md).

## Repository map

```text
src/ev_fleet/                 Python backend package
  common/                     Shared configuration, clocks and calculations
  contracts/                  API schemas shared between services
  fleet/                      Vehicle, telemetry, rental and claim ownership
  demand/                     Demand observations and heat-map calculations
  rebalancing/                Eligibility and recommendation strategies
  dispatch/                   Approval and relocation coordination
  facilities/                 Parking and charging observations and capacity
  simulator/                  Simulated EV nodes and movement

dashboard/                    React operator interface and Nginx configuration
simulation-console/           Simulation control backend and React frontend
k8s/                          Kubernetes manifests and optional simulation jobs
scripts/                      Setup, startup, verification and evaluation commands
tests/                        Python unit and integration tests
experiments/                  Reproducible strategy evaluation code
results/                      Preserved evaluation output
docs/                         Architecture, API, operations and feature guides

pyproject.toml                Python project metadata and dependency ranges
requirements.lock             Exact backend container dependency set
.env.example                  Supported local integration settings
Dockerfile                    Backend container build
docker-compose.yml            Local multi-container configuration
```

### Kubernetes files

| File | Purpose |
|---|---|
| `k8s/kustomization.yaml` | Collects the manifests rendered and applied by the startup script |
| `k8s/namespace.yaml` | Creates the isolated `ev-fleet` namespace |
| `k8s/configmaps.yaml` | Non-secret backend configuration shared by workloads |
| `k8s/data-services.yaml` | PostgreSQL and Redis workloads, storage and health checks |
| `k8s/backends.yaml` | Fleet, Demand, Rebalancing, Dispatch and Facilities workloads |
| `k8s/services.yaml` | Cluster networking for backend and data services |
| `k8s/simulators.yaml` | Default simulated EV workloads |
| `k8s/dashboard.yaml` | Operator dashboard Deployment and Service |
| `k8s/simulation-console.yaml` | Simulation console Deployment and Service |
| `k8s/hpa-dashboard.yaml` | Optional CPU autoscaling policy for the operator dashboard |
| `k8s/simulations/` | Optional customer-rental Job, Deployment and controller permissions |

## Important scripts

| Script | Purpose |
|---|---|
| `scripts/setup-local.ps1` | Creates or repairs `.venv` and synchronizes runtime dependencies |
| `scripts/start-kubernetes.ps1` | Safely builds, deploys and connects to the Windows Kubernetes environment |
| `scripts/start-kubernetes.sh` | macOS Docker Desktop Kubernetes startup |
| `scripts/test-local.ps1` | Runs backend tests and frontend tests/builds; `-Integration` adds disposable infrastructure checks |
| `scripts/render_facilities_secret.py` | Validates whitelisted `.env` values and renders in-memory Kubernetes artifacts |
| `scripts/setup-local-metrics.ps1` | Installs or updates Metrics Server for local HPA testing |
| `scripts/run_strategy_evaluation.py` | Runs reproducible strategy comparisons and writes raw evaluation results |
| `scripts/analyze_strategy_results.py` | Summarizes saved evaluation results and produces tables and graphs |

The two evaluation filenames retain their original names for compatibility. They are
experimental tools and are not part of application startup.

## Configuration and optional integrations

Copy `.env.example` to `.env` and set only the integrations you need. The Windows
startup script validates an allowlist of facilities, routing, traffic-control and
traffic-simulation settings. Credential values are piped directly to Kubernetes and
are not written to generated manifest files or printed by the script.

Available optional integrations include:

- [LTA DataMall parking and charging observations](docs/lta-datamall.md)
- [OneMap road routing](docs/onemap-routing.md)
- live or simulated traffic settings documented in `.env.example`

The default system remains fully simulated when these integrations are disabled.

## Simulation analytics and HPA observability

The [Simulation Analytics guide](docs/simulation-analytics.md) describes a
local-only Performance Analytics panel at <http://127.0.0.1:8090> with bounded
Normal / High / Extreme HTTP load presets, read-only Dashboard HPA CPU and Pod
metrics, observed scaling-event charts, and persistent experiment CSV exports.

Presets describe **synthetic dashboard HTTP demand**, not real telemetry
injections or active EV counts. This feature never modifies vehicle state or
the dispatch workflow. Kubernetes Metrics Server must be healthy for resource
measurements, and HPA scale-out depends on actual CPU utilisation.

## Dashboard autoscaling

The Kubernetes resources include CPU-based autoscaling for the stateless operator
dashboard. A fresh Docker Desktop cluster also needs Metrics Server:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 `
  -SetupMetricsServer -AllowInsecureLocalKubeletTls
```

The insecure kubelet TLS option is only for local Docker Desktop testing. Never use
it in production. State-owning services and individual EV simulators remain at one
replica for correctness.

## Optional customer-rental workload

The rental workload is disabled by default. Run either the finite Job:

```powershell
kubectl apply -f .\k8s\simulations\customer-job.yaml
kubectl -n ev-fleet logs -f job/customer-rental-simulation
```

or the persistent interactive Deployment:

```powershell
kubectl apply -f .\k8s\simulations\customer-deployment.yaml
kubectl -n ev-fleet logs -f deployment/customer-rental-demo
```

Do not run both at the same time because they use the same six vehicle IDs. See
[Customer rental simulation](docs/customer-rental-simulation.md) for safe stop and
restart instructions.

## Testing and validation

Run the repository's local-first verification command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
```

For telemetry, Docker Compose, Kubernetes, database or deployment-sensitive changes:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 -Integration
```

Integration mode uses disposable local resources through Docker Desktop and `act`.
It must never be pointed at existing production Kubernetes or PostgreSQL data. If
Docker or `act` is unavailable, treat integration verification as not verified.

The strategy comparison tools under `experiments/` can generate paired seeded trial
data, confidence intervals, tables and SVG graphs without changing the application's
selection policy.

## Safety and limitations

- This is a trusted-local simulation without authentication or authorization.
- Internal APIs must not be exposed to an untrusted network.
- PostgreSQL snapshots support local restart recovery but not safe multi-replica
  transactions across state-owning services.
- Simulator journey progress is process-local. Deployment is therefore blocked while
  active rentals, relocations or charging tasks exist.
- The startup script never deletes PostgreSQL persistent-volume data.
- Parking allocation is simulated and is not a real reservation.
- Demand, movement, arrival, rentals and charging are simulated.
- Optional real observations do not provide real-road autonomy or vehicle control.
- OpenStreetMap tiles require browser network access; service panels continue to work
  if map tiles are unavailable.

## Documentation by topic

- [Architecture and service ownership](docs/architecture.md)
- [API contracts](docs/api-contracts.md)
- [Dispatch lifecycle](docs/dispatch-relocation.md)
- [Demand and heat-map calculations](docs/demand-heatmap.md)
- [Operator dashboard behaviour](docs/operator-dashboard.md)
- [Parking and charging facilities](docs/parking-charging-facilities.md)
- [Rental lifecycle](docs/rental-lifecycle.md)
- [Multi-EV parking-aware allocation](docs/multi-ev-parking-allocation.md)
- [Evaluation method](docs/strategy-evaluation.md)
- [Local Kubernetes deployment](docs/kubernetes-local.md)
- [PostgreSQL persistence and recovery](docs/kubernetes-local.md#postgresql-bootstrap-and-safe-recovery)
