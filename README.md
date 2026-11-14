# Autonomous EV Fleet Rebalancing - Singapore

This repository contains a distributed, simulation-only system for observing an
electric-vehicle fleet and helping a human dispatcher rebalance available vehicles
between Singapore zones.

The system combines simulated demand, vehicle telemetry, parking capacity, charging
information and explicit operator approval. It does not control real vehicles.
Optional integrations can provide real parking, charging, routing and traffic
observations, but movement and operational decisions remain simulated.

## What this project demonstrates

This is a **distributed-systems coursework demonstration**, not an autonomous
vehicle service. The goal is to show how separate services work together to
decide **whether an EV should be relocated from one Singapore zone to another**,
and how a human-approved request is safely carried out by simulated vehicles.

| Interface | Local URL | What to demonstrate |
|---|---|---|
| **Simulation Console** | <http://127.0.0.1:8090> | Change zone demand, pause/start demand generation, create controlled traffic/rental scenarios, and inspect service health and analytics. |
| **Operator / Map Dashboard** | <http://127.0.0.1:8080> | View Singapore zones and simulated EV telemetry, inspect shortages and recommendation evidence, approve a relocation, and track the EV along a verified road route to arrival. |

An example presentation is **increasing demand in CBD**, observing the shortage,
requesting a suitable EV, **explicitly approving** the dispatch, then watching
Fleet telemetry and Dispatch status change as the simulator moves and arrives.
The system also showcases distributed-system concerns: independent services,
freshness and partial failures, eligibility and battery checks, competing claims,
idempotent approvals, and recovery from uncertain outcomes.

In the operator dashboard's facility-based Rebalance workflow, the requested EV
count is an operational instruction: plan that many incoming relocations when
feasible. Coverage scores rank the plan and warn about trade-offs; low benefit
alone does not prevent explicit human approval. EVs already assigned to the
destination do not fill the incoming count. The planner matches compatible
parking/charging capacity across eligible destinations before checking road
routes. Fresh data, routes, battery reserve, ownership, capacity and expiry still
apply, so physical or evidence constraints can leave a request partly unfilled.
Advisory API requests retain useful-only optimisation unless they explicitly set
`planning_mode=FULFIL_REQUEST`; saved recommendations retain this intent during
approval revalidation.

Recommendations are **not** dispatch commands. A high-demand zone does not
guarantee a move: the decision may legitimately be **MOVE** (useful, eligible
relocation), **HOLD** (no useful verified move), or **DEFER** (assessment incomplete).
No real vehicles are controlled.

Set up the local demo on [Windows](#quick-start-on-windows-laptop-or-desktop)
or [macOS](#quick-start-on-macos).

## Quick start on Windows (laptop or desktop)

Run these commands from **Windows PowerShell** in the repository root. Each
computer needs its own Docker Desktop setup and local `.env` file. For a longer
demonstration checklist, see [Local demo readiness](docs/local-demo-readiness.md).

### Step 1 — Install prerequisites and get the code

- Git and Windows PowerShell
- Python **3.12+** and Node.js **22+**
- Docker Desktop using **Linux containers**, with its **Kubernetes** feature enabled
- `kubectl` configured for the **`docker-desktop`** context
- Available local ports **8080** (map dashboard) and **8090** (Simulation Console)

On a new computer, clone the repository:

```powershell
git clone https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing.git
cd Autonomous-EV-Fleet-Rebalancing
```

The repository must be accessible to your GitHub account. If you already have
a checkout, open it in PowerShell. Create `.env` only if it is missing.

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Check the Docker Desktop context and local Kubernetes readiness:

```powershell
docker context show
kubectl config current-context
kubectl get nodes
```

Both the Docker context and Kubernetes context should target your local
Docker Desktop installation, and the Kubernetes node should report `Ready`.
Do not deploy to an unintended remote cluster.

Follow the [shared credential setup](#configuration-and-optional-integrations)
before launch if you want live LTA data or an end-to-end relocation demo.

### Step 2 — Prepare, test, and launch

Run the following **in order**:

**1. Prepare the Python environment**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

Creates or repairs the project-local `.venv` and installs the required Python
packages. Generally needed on initial setup or when dependencies change; the
normal startup command also calls this automatically.

**2. Test the code before demonstrating it**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
```

Runs the Python backend tests, React operator-dashboard unit tests and build,
and Simulation Console build. It installs frontend npm dependencies if missing.
A `PASS` here does **not** prove live OneMap routing, Kubernetes, or both user
interfaces work together; the live demo check below is still required. Stop
and investigate failures instead of assuming everything works.

**3. Start the complete local system**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

Validates the local Docker/Kubernetes environment and existing data, refuses
unsafe redeployment during active workflows, builds updated Docker images,
deploys services/simulators/dashboards, waits for their rollouts, and opens
or reuses local dashboard connections. **Keep the PowerShell window open**
while its port-forward connections are needed; `Ctrl+C` stops tunnels it
created, not the Kubernetes deployment.

### Step 3 — Open both dashboards

- **Simulation Console:** <http://127.0.0.1:8090>
- **Operator / Map Dashboard:** <http://127.0.0.1:8080>

The Simulation Console **creates the scenario**; the Operator Dashboard
**evaluates, approves, and shows its outcome**. Both views use backend state;
the map must not invent vehicle motion when telemetry is missing.

### Step 4 — Try an end-to-end relocation scenario

1. In the **Simulation Console → Demand**, **pause** automatic demand generation
   for predictable manual values. Select all six zones and apply a fresh
   desired-supply value of **0**; then select **CBD** alone and apply a larger
   target (for example, **12**). This creates *simulated* high demand, not a
   real observation. While the simulator is paused, manually set targets stay
   valid until updated. CRDS still needs a valid target in every zone.
2. In the **Operator Dashboard → Dispatch**, confirm Fleet, Demand and Facilities
   observations are available. Select **CBD** on the map or zone list and
   request a recommendation. Inspect **Evidence** if the result is HOLD/DEFER.
3. If the result is **MOVE**, review the proposed EV, parking destination,
   arrival battery and checked road route. Click **Review dispatch** and then
   **Approve**. Approval is deliberately separate from recommendation.
4. Follow **Relocations** and the **Map**: observe command acknowledgement,
   reported movement, arrival, battery use, and eventual claim release.
   Accepted bulk jobs show queued, admitted and arrived counts and continue
   automatically after refresh. For an ambiguous approval submission,
   **check/retry the original request ID**; do not create a second approval blindly.

A relocation can only succeed when an available EV, compatible destination,
adequate battery reserve, the applicable planning policy and **verified OneMap road
route** all exist. Manual demand can become stale (default: **300 seconds**);
refresh it if a subsequent recommendation reports expired observations.
Traffic scenarios, rentals, and charging may temporarily make vehicles
ineligible. See [demo readiness and recovery](docs/local-demo-readiness.md)
for expected failure cases.

### Other PowerShell commands and when to use them

| Command | Use it when | Important effect |
|---|---|---|
| `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -ConnectOnly` | The deployment is already running and you only need the dashboards. | Opens/reuses local tunnels **without** rebuilding, restarting Pods or reloading `.env`. |
| `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -FreshBoot` | You explicitly need to recreate the project Python environment and replace project Pods. | Rebuilds/restarts local project workloads and reloads `.env` while **preserving PostgreSQL PVC data**. It still refuses to proceed during active rentals, relocations or charging and is **not** a routine first fix. |
| `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -CheckPostgresOnly` | You want to check the current local PostgreSQL StatefulSet without full redeployment. | Database health check only. |
| `powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 -Integration` | You changed infrastructure, Kubernetes, Compose or database behaviour. | Runs additional **disposable local** infrastructure checks; requires Docker Desktop and `act`. Do not use production data. |

To reload credentials or other `.env` edits, see [Apply `.env` changes](#apply-env-changes).

### Common reasons a demo cannot dispatch

| Symptom | Check |
|---|---|
| OneMap routing is unavailable | Check the routing-status banner and the [OneMap configuration and token expiry](#onemap-rest-access-token-for-relocations). After updating `.env`, run normal startup. |
| No recommendation / **DEFER** | Open the recommendation **Evidence** panel for stale demand, Fleet or routing issues. An unavailable route is not proof that no EV is eligible. |
| No useful move / **HOLD** | Inspect EV availability, parking, battery reserve and the *fleet-wide* shortage effect; high demand in one zone alone is insufficient. |
| Data is stale | Confirm simulator Pods and services are healthy; refresh demand observations and try again. |
| Review dispatch is disabled | Wait for fresh data or request a new non-expired recommendation; do not bypass checks. |
| Different result on laptop and PC | Confirm the same code commit, local `.env` configuration, Kubernetes context, service readiness and clock/observation freshness. |

For environment repair and deeper diagnostics, see
[Local Kubernetes deployment](docs/kubernetes-local.md) and
[Local-first testing](docs/local-testing.md).

## Quick start on macOS

### 1. Set up Kubernetes in Docker Desktop

1. Install the build for your Mac's chip (Apple silicon or Intel), then open
   the app. [Download Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/).
2. In Docker Desktop, open **Kubernetes** and click **Create cluster**.
3. Select **Kubeadm**, click **Create**, and wait for Kubernetes to show as
   running. If you already have a running Kubeadm cluster, use it.

See [Docker Desktop's Kubernetes guide](https://docs.docker.com/desktop/use-desktop/kubernetes/#enable-kubernetes).

Then open Terminal and check that Docker is reachable and the `docker-desktop`
node reports `Ready`.

```sh
docker --context desktop-linux info
kubectl --context docker-desktop get nodes
```

### 2. Get the code and configure the demo

For the remaining steps, use Terminal with Git, `bash`, `curl`, and
Python **3.12+** available as `python3`.

For a new checkout, clone the repository using a GitHub account with access.
If you already have a checkout, open it in Terminal and start with the `.env` step.

```sh
git clone https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing.git
cd Autonomous-EV-Fleet-Rebalancing
```

From the repository root, create `.env` only if it is missing.

```sh
if [ ! -e .env ]; then cp .env.example .env; fi
```

Follow the [shared credential setup](#configuration-and-optional-integrations)
before launch if you want live LTA data or an end-to-end relocation demo.

### 3. Start the local system

```sh
bash scripts/start-kubernetes.sh
```

Startup builds the images, applies the local configuration, waits for core
services, and opens port forwards. Keep this terminal open while using the apps.
`Ctrl+C` closes the port forwards started by this script. The Kubernetes
workloads keep running.

- **Simulation Console** at <http://127.0.0.1:8090>
- **Operator / Map Dashboard** at <http://127.0.0.1:8080>

EV simulators may still be starting when the dashboards open. Check their pods
with `kubectl --context docker-desktop -n ev-fleet get pods` and wait for online
vehicles in the Operator Dashboard before trying the [relocation walkthrough](#step-4--try-an-end-to-end-relocation-scenario).

### 4. Reconnect or apply configuration changes

To reopen the dashboards using the existing deployment, run:

```sh
bash scripts/start-kubernetes.sh --connect-only
```

If a dashboard port is occupied, set `DASHBOARD_PORT` or
`SIMULATION_CONSOLE_PORT` to an unused port before running the script. Use the
URLs printed in Terminal.

To reload credentials or other `.env` edits, see [Apply `.env` changes](#apply-env-changes).

## Local Python environment

On macOS, the Kubernetes startup only needs host `python3` for configuration.
To work on the backend locally, create a virtual environment from the repository
root and install the project with its test dependencies.

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

For macOS development checks, see [the local checks in `AGENTS.md`](AGENTS.md#local-checks-on-windows-and-macos).
Local frontend checks require Node.js **22+** and `npm ci` in each frontend
directory before the first run.

### Windows Python environment

`scripts/setup-local.ps1` is called automatically during a full Windows Kubernetes
startup. It installs the runtime and local test dependencies and can also be run
directly:

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

The operator can request one EV or a batch of up to 50. Batch recommendations rank
vehicle-to-facility pairs, respect simulated parking capacity and may return partial
fulfilment. A bulk approval durably queues the reviewed assignments for up to
30 minutes. Dispatch starts at most four relocations at once by default and retries
transient failures automatically. Every admission revalidates current vehicle,
route, battery, parking and configured policy checks. See
[human-approved bulk jobs](docs/paced-bulk-relocation.md).

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

Rebalancing excludes EVs with missing or stale telemetry, offline connectivity,
unavailability, a rental or active relocation/claim, insufficient predicted
arrival charge, or another conflicting operation. CRDS is the default
rebalancing policy (`CRDS_MODE=ACTIVE`). It compares
eligible moves across declared futures, ranks their minimum fleet-wide shortage
reduction, and can recommend MOVE, HOLD or DEFER. Each dispatch still requires
operator approval and fresh revalidation. Execution defaults to LOCAL, so the
optional HTTP workers are not required. The existing CHARGE workflow is preserved.

Older generated demand can widen the demand scenarios while it remains valid.
CRDS models route and return delays separately from demand age.

Set `CRDS_MODE=ADVISORY` to review CRDS recommendations without permitting their
dispatch, or `CRDS_MODE=DISABLED` to select the legacy weighted policy. Its score is:

```text
35% distance utility
20% current state of charge
35% predicted arrival reserve
10% charging suitability
```

In this **legacy weighted policy**, the highest score wins, followed by
deterministic tie-breaks. A nearest-eligible-EV strategy exists only as an
experimental comparison; **default CRDS** instead evaluates the minimum
fleet-wide shortage reduction across its declared scenarios.

### Rentals and charging

Fleet records idempotent rental starts and confirmed returns. A rented vehicle is
ineligible for relocation even if its ordinary telemetry says `AVAILABLE`. Returns
use fresh telemetry received after rental start and remain visible after service
restart.

Charging visits require a human handoff: an approved simulated EV arrives and
waits for ground staff to confirm **Cable connected**. It then charges to its
target (80% by default), stays unavailable while plugged in, and returns to
service only after **Cable disconnected** and fresh EV telemetry. Named-team
confirmations are recorded with the occupied charging allocation. The separate
manual assistance task log remains available. These features do not connect to
a real charging provider or physically charge a vehicle.

See [Human charging handoff](docs/charging-cable-handoff.md) for the demonstration.

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

On Windows and macOS, put your own credentials in the repository-root `.env`
and keep it out of version control. The dashboards, simulated demand, and
telemetry work with the defaults in `.env.example` and require no API credentials.
Enable the integrations you need for your demo.

### LTA DataMall Account Key for live observations

Request your own API access from LTA DataMall. LTA sends the **Account Key**
to the email address you provide. Copy it into `LTA_DATAMALL_ACCOUNT_KEY`.
[Request LTA DataMall API access](https://datamall.lta.gov.sg/content/datamall/en/request-for-api.html).

```dotenv
LTA_DATAMALL_ACCOUNT_KEY=your-emailed-account-key
FACILITIES_SOURCE=LTA_DATAMALL
TRAFFIC_SOURCE=LTA_DATAMALL
```

Both live feeds use the same key. `FACILITIES_SOURCE=LTA_DATAMALL` enables live
parking and charging data. `TRAFFIC_SOURCE=LTA_DATAMALL` enables live road
traffic. To enable just one feed, leave the other at its default (`MOCK` for
facilities or `DISABLED` for traffic). You can run a relocation demo with mock
facilities and OneMap routing. See [LTA DataMall configuration](docs/lta-datamall.md).

### OneMap REST access token for relocations

Relocation approval requires a verified OneMap road route. Use your separate
OneMap account to generate the **REST access token** for `ONEMAP_ACCESS_TOKEN`.
Each OneMap token lasts **3 days**.
[Register for a OneMap API account](https://www.onemap.gov.sg/apidocs/register).

1. In Postman or another HTTP client, send a `POST` to
   `https://www.onemap.gov.sg/api/auth/post/getToken` with `Content-Type: application/json`
   and a JSON body containing your registered OneMap `email` and `password`.
   [OneMap authentication](https://www.onemap.gov.sg/apidocs/authentication).
2. Copy the returned `access_token` into `.env` and select OneMap routing.

   ```dotenv
   ROUTING_PROVIDER=ONEMAP
   ONEMAP_ACCESS_TOKEN=your-onemap-rest-access-token
   ```

3. Repeat the request before the token expires, replace `ONEMAP_ACCESS_TOKEN`,
   and [apply the change](#apply-env-changes) if the project is already deployed.

See [OneMap routing configuration](docs/onemap-routing.md) for more details.

### Apply `.env` changes

After adding or updating either credential, run normal startup when the
simulation is idle. Wait for active relocations, rentals, and charging tasks to
finish before redeploying.

- **Windows** `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1`
- **macOS** `bash scripts/start-kubernetes.sh`

Connect-only mode reopens the dashboards using the existing configuration.

## Simulation analytics and HPA observability

The [Simulation Analytics guide](docs/simulation-analytics.md) describes a
local-only Performance Analytics panel at <http://127.0.0.1:8090> with bounded
Normal / High / Extreme HTTP load presets, read-only Dashboard HPA CPU and Pod
metrics, observed scaling-event charts, and persistent experiment CSV exports.

Presets describe **synthetic dashboard HTTP demand**, not real telemetry
injections or active EV counts. This feature never modifies vehicle state or
the dispatch workflow. Kubernetes Metrics Server must be healthy for resource
measurements, and HPA scale-out depends on actual CPU utilisation.

## Shared live traffic cache

The [Shared Traffic Cache guide](docs/shared-traffic-cache.md) describes a
single LTA collector, versioned Redis snapshots, per-feed freshness enforcement,
and the lightweight reads used by Rebalancing. Missing traffic remains unknown.

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
- Simulator journey progress is process-local. The Windows startup blocks
  redeployment during active rentals, relocations or charging tasks. On macOS,
  wait for those tasks to finish before redeploying.
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
