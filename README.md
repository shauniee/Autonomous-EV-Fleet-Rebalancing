# Autonomous EV Fleet Rebalancing - Singapore

CSC3104 Cloud and Distributed Computing project. This repository contains a
distributed, simulation-only system that helps a human dispatcher move available
electric vehicles to Singapore zones with insufficient supply.

The system observes simulated demand and vehicle telemetry, recommends a suitable
EV, requires explicit dispatcher approval, and then simulates the EV travelling to
the selected zone. It does not control real vehicles and does not use live demand,
traffic or road-routing data. Optional LTA DataMall integration supplies real parking
and charging observations for simulated destinations; see [LTA setup](docs/lta-datamall.md).

## What the project demonstrates

- Five independently running FastAPI services communicating over HTTP, including the mock Facilities Service.
- Simulated EV processes that register and continuously publish ordered telemetry.
- Six representative Singapore zones: Jurong East, Woodlands, Bishan, Punggol,
  Tampines, and CBD.
- Demand, available supply, projected supply, shortages, and hotspot severity.
- Explainable energy-aware EV selection with eligibility and reserve checks.
- Human-approved dispatch, atomic vehicle claims, idempotent commands, gradual
  movement, SOC consumption, arrival verification, and timeout reconciliation.
- A React/TypeScript operator dashboard with a Leaflet/OpenStreetMap display.
- A separate simulation console for setting demand, controlling the optional customer rental workload, and observing expected return zones.
- Operator-reviewed low-battery alerts, ground-staff charging tasks and advisory rental-return forecasts.
- A complete local Docker Desktop Kubernetes deployment.
- Unit, integration, dashboard, browser, container, and experimental evaluation
  evidence.

## Architecture and ownership

Each service owns a separate part of the simulation:

| Component | Responsibility | Default host port |
|---|---|---:|
| Fleet Service | Vehicle registration, telemetry, freshness, versions, zones, and claims | 8001 |
| Demand Service | Zone demand, charging observations, heat-map composition, and optional demand simulation | 8002 |
| Rebalancing Service | Eligibility checks, scoring, ranking, and short-lived recommendations | 8003 |
| Dispatch Service | Approval, claim coordination, commands, lifecycle events, and reconciliation | 8004 |
| Facilities Service | Simulated parking, chargers and capacity observations | 8005 |
| EV simulators | Simulated physical position, SOC, command polling, and movement | None |
| Operator dashboard | Map, recommendation review, and explicit dispatch approval | 8080 |
| Simulation console | Demand input and read-only service, fleet, and dispatch observations | 8090 |

Runtime state is exchanged only through service APIs. One service does not import
another service's private store or database repository.

The relocation flow is:

```text
Demand and Fleet observations
        -> select hotspot in dashboard
        -> request Rebalancing recommendation
        -> review selected EV and explanation
        -> explicitly approve through Dispatch
        -> Fleet atomically claims the EV
        -> EV simulator receives and acknowledges the command
        -> EV reports movement and decreasing SOC
        -> Dispatch verifies arrival
        -> Fleet releases the claim in the destination zone
```

A recommendation is advisory. It neither reserves nor moves an EV. Only the
separate Dispatch approval operation can start a relocation.

See [Rental forecasts and charging operations](docs/rental-forecast-charging-ops.md) for local-only controls, safety assumptions and current limitations. Rental returns do not automatically alter the Rebalancing algorithm, and ground-staff tasks do not themselves physically charge EVs.

## PostgreSQL portable startup

On a fresh Docker Desktop cluster, the checked-in PostgreSQL StatefulSet
now reserves sufficient first-start time before liveness checks begin, explicitly
uses SCRAM password authentication, and verifies the actual `ev_fleet` SQL
database through the Pod network before reporting ready. Existing PVC data is
never automatically deleted or reinitialised.

If a previously interrupted installation already has a partial PostgreSQL
data directory, the deployment script stops with a diagnostic instead of
overwriting it. See [PostgreSQL startup and recovery](docs/kubernetes-local.md#postgresql-bootstrap-and-safe-recovery).

## Kubernetes startup

### Prerequisites

- Docker Desktop with Kubernetes enabled and Linux containers
- `kubectl` using the `docker-desktop` context
- PowerShell
- Host ports 8080 and 8090 available

On Windows, choose the command based on whether you want to deploy code changes.

**Run updated code — build, deploy, and open both dashboards:**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

Use this command after changing the code. It builds new backend, operator
Dashboard, and Simulation Console images, deploys them to Kubernetes, waits for
the updated Pods, and then opens the dashboard connections. It also validates
the whitelisted facilities, routing, live-traffic and traffic-simulation values
from root `.env`, pipes backend-only keys directly into Kubernetes Secrets, and
restarts their consumers without printing the keys.

**Run the existing deployed code — open both dashboards only:**

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -ConnectOnly
```

Use this command when the system is already deployed and you do not want to
apply local code changes. It only opens or reuses the dashboard connections;
it does not read `.env`, build images, deploy code, or restart Pods.

On macOS with Docker Desktop Kubernetes (kubeadm), Python 3 and curl available:

```bash
bash scripts/start-kubernetes.sh
```

The macOS script loads only the facilities settings from root `.env` into a backend
Kubernetes Secret. To use real parking and charging observations, set
`FACILITIES_SOURCE=LTA_DATAMALL` and `LTA_DATAMALL_ACCOUNT_KEY=...` there.
See [LTA DataMall integration](docs/lta-datamall.md) for modelling and freshness rules.
Optional [OneMap routing](docs/onemap-routing.md) adds road-following simulation,
map route overlays and directions using a separate backend access token.
The macOS script builds native images with unique tags and replaces application
Pods, including simulators, to load the integration. Run it between demos with no
active relocations. To reopen both interfaces without reading `.env` or deploying:

```bash
bash scripts/start-kubernetes.sh --connect-only
```

Existing healthy tunnels are reused; extra EV rollouts do not block the web apps.
See the [parking and charging walkthrough](docs/lta-datamall.md#end-to-end-walkthrough)
to verify real destinations, simulated charging to 80%, and subsequent parking.

On **Windows**, each normal invocation builds backend, Dashboard and Simulation
Console images with a **new local version tag**, renders the Kubernetes manifests
to those tags, applies them, and waits for Pods to roll out. There is **no need**
to manually run `kubectl rollout restart` after a code change. The unchanged
`k8s-local` aliases are retained only for optional standalone test Job commands.

Deployment is blocked if Fleet reports an in-progress rental, relocation or
charging operation, or the customer rental simulator/Job is running. Finish
those sessions first; the script never resets PostgreSQL or forcibly completes
a customer journey. Do not issue new dispatches while deploying.

The original `-SetupMetricsServer -AllowInsecureLocalKubeletTls` command remains
**Deploy mode**. Metrics Server setup is idempotent but typically only needed
once on a new cluster. The startup command keeps its terminal open only for
newly created dashboard tunnels; existing healthy tunnels are reused.
See [the local deployment guide](docs/kubernetes-local.md) for troubleshooting.

### Optional HPA for the Dashboard

The Kubernetes manifests include Dashboard CPU autoscaling (1–3 replicas,
60% CPU target), but a fresh cluster also needs Metrics Server:

```powershell
powershell -ExecutionPolicy Bypass -File .\\scripts\\start-kubernetes.ps1 `
  -SetupMetricsServer -AllowInsecureLocalKubeletTls
```

The `-AllowInsecureLocalKubeletTls` workaround bypasses kubelet certificate
verification **only for local Docker Desktop testing**; never enable it for
production. Omit it when using valid kubelet certificates. Without the
optional setup switch, the usual startup continues to work, but HPA will
need an existing healthy Metrics API. Other state-owning services remain
single-replica for correctness. Details:
[Dashboard HPA and Metrics Server](docs/kubernetes-local.md#dashboard-horizontal-pod-autoscaler-hpa).

Open both Kubernetes interfaces:

- Simulation console (simulation dashboard): <http://127.0.0.1:8090>
- Operator dashboard: <http://127.0.0.1:8080>

Demand starts unknown. In the simulation console, create a repeatable manual
hotspot:

1. Select **CBD** under **Set zone demand**.
2. Enter `12` for desired available vehicles.
3. Select **Apply demand**.
4. Confirm that CBD shows the updated demand observation.

Then use the operator dashboard:

1. Select **CBD**.
2. In **Relocation request**, choose the number of EVs needed, from 1 to 20.
3. Select **Recommend EV** or **Recommend N EVs**.
4. Review the ranking and every proposed vehicle-to-parking assignment.
5. Select **Review dispatch**.
6. Approve the reviewed EV or batch within the 120-second review window.
7. Watch each relocation change from pending to in progress and then succeeded.
8. Confirm that the EVs finish in CBD and become available again.

Press `Ctrl+C` in the startup window when finished. PostgreSQL data remains in
the `postgres-data-postgres-0` Kubernetes PVC for the next startup. For logs,
troubleshooting, and teardown, see
[Local Kubernetes deployment](docs/kubernetes-local.md).

## Demand input

Manual demand through the simulation console is used for a deterministic
demonstration. The console reads the current Demand version before applying an
observation and confirms the updated value. All supported service settings and
defaults are listed in [.env.example](.env.example).

Timestamps use UTC on the wire. Dashboard times use Asia/Singapore. Distances are
kilometres, energy is kWh, and state of charge is a percentage.

## EV selection policy

Rebalancing first excludes EVs with missing/stale telemetry, offline connectivity,
unavailability, an active relocation or claim, or insufficient predicted arrival
SOC. Eligible EVs receive an explainable weighted score:

```text
35% distance utility
20% current SOC
35% predicted arrival reserve
10% charging suitability
```

The highest score wins, followed by deterministic tie-breaks. Phase 4 also includes
a nearest-eligible-EV strategy strictly as an evaluation baseline; the application
default remains energy-aware.

## Verification

Run the backend regression suite and dependency check:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check
```

Run dashboard tests and a production build:

```powershell
Set-Location dashboard
npm.cmd test
npm.cmd run build
```

Run the isolated browser end-to-end workflow after installing Chromium once:

```powershell
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

The browser suite starts isolated backend services and moving EV processes on test
ports. It does not reuse the running Kubernetes deployment.

Validate the Kubernetes resources without changing the cluster:

```powershell
kubectl kustomize .\k8s
kubectl apply --dry-run=client -k .\k8s
```

## Phase 4 evaluation

Phase 4 compares the unchanged energy-aware policy with the nearest-eligible-EV
baseline using paired seeded scenarios, repeated trials, raw records, confidence
intervals, tables, and SVG graphs. It does not change production selection or the
dispatch lifecycle.

Run the complete 600-trial matrix with:

```powershell
.\.venv\Scripts\python.exe scripts\run_phase4_evaluation.py --repetitions 30
```

See [the evaluation guide](docs/phase4-evaluation.md),
[verification report](docs/phase4-verification.md), and preserved output under
`results/phase4-evaluation/`.

## Repository map

```text
src/ev_fleet/contracts/    Validated request and response models
src/ev_fleet/common/       Shared settings, logging, time, and travel calculations
src/ev_fleet/fleet/        Fleet API and durable vehicle/claim state
src/ev_fleet/demand/       Demand API, simulation, and heat-map composition
src/ev_fleet/rebalancing/  Eligibility, strategies, ranking, and recommendations
src/ev_fleet/dispatch/     Approval and relocation coordination
src/ev_fleet/simulator/    Stationary and moving simulated EV processes
src/ev_fleet/facilities/   Mock parking/charging catalogue and allocations
dashboard/                 React dashboard, Nginx proxy, and frontend tests
simulation-console/        Demand input and service-observation console
k8s/                       Local Kubernetes resources
tests/                     Python unit and integration tests
experiments/phase4/        Reproducible evaluation runner and analysis
scripts/                   Smoke, browser-test, and evaluation entry points
docs/                      Architecture, contracts, phase guides, and evidence
results/                    Preserved Phase 4 evaluation output
```

## Important limitations

- Fleet, Demand, Rebalancing recommendations, Dispatch workflows, rentals and
  mock parking allocations use PostgreSQL-backed durable snapshots. Redis stores
  only short-lived display cache entries and may be lost without affecting
  dispatch correctness.
- Individual EV simulator journey progress remains process-local. A simulator Pod
  replacement preserves its stable vehicle identity but starts a new telemetry
  session and reconciles from Fleet and Dispatch APIs.
- Durable snapshots provide restart recovery, not multi-replica transaction
  safety. State-owning services and individual EV simulators must remain at one
  worker and one replica until database-level per-entity locking and race tests
  are implemented.
- There is no authentication or authorization. Internal APIs must not be exposed to
  an untrusted network.
- Demand, movement and arrival are simulated. Optional LTA facilities retain real
  coordinates and reported availability with local simulation allocations. There is no
  real-road autonomy, live map routing, ML, maintenance model, real reservation
  system, or charging-session integration.
- Public OpenStreetMap tiles require browser network access. The dashboard's zone
  list and service panels continue working if tiles fail.

## Further documentation

- [Architecture and ownership](docs/architecture.md)
- [API contracts](docs/api-contracts.md)
- [Dispatch lifecycle](docs/phase2-dispatch.md)
- [Demand and heat-map calculations](docs/phase3a-demand-heatmap.md)
- [Dashboard behaviour](docs/phase3b-dashboard.md)
- [Local Kubernetes deployment](docs/kubernetes-local.md)
- [Implementation history](docs/implementation-plan.md)

## Phase 5A: simulated parking and charging catalogue

A separate simulated Facilities Service now provides 15 clearly labelled MOCK
parking/charging locations across the six simulated zones. The dispatcher map
shows their pins and per-zone capacities, independently of Fleet/Demand health.
The legacy single-EV ranking remains zone-based; the Phase 5C batch pathway now
uses mock parking capacity persisted as simulation state in PostgreSQL. Mock charger
bays are included in (not added to) reported parking capacity. The local
Kubernetes manifests include the new Facilities Service.

See [Phase 5A guide](docs/phase5a-mock-facilities.md) for endpoints, scope,
verification instructions and the planned rental / multi-EV / parking allocation
extensions. Historical Phase 4 evidence remains unchanged.

## Phase 5B: simulated free-floating rentals

Fleet now tracks simulated rental start and confirmed return events via
version-checked, idempotent HTTP endpoints. A rented EV cannot be ranked or
claimed for immediate relocation, even if regular vehicle telemetry reports
AVAILABLE. The return position and SOC are taken from fresh telemetry that
arrived **after** rental start, so the customer may end at any position inside
the simulated operating envelope. Confirmed rental status and expected return
time appear in the Fleet dashboard and EV map and survive service restarts in
PostgreSQL. There is **no pre-dispatch, parking allocation or
automatic rental simulator** yet. For API examples and tests, see
[Phase 5B guide](docs/phase5b-rental-lifecycle.md).


## Phase 5C: multi-EV parking-aware allocation

The dispatcher can request **1–20 EVs** for any selected zone using the
parking-aware workflow. The backend ranks eligible EV–facility pairs with a
deterministic greedy heuristic (existing 85% energy-aware score + 15% parking
availability ratio), respects mock parking capacity and charging needs, and
proposes partial fulfilment when insufficient vehicles/spaces exist.

Batch recommendations do not hold vehicles or parking places. Explicit batch
approval revalidates capacity and EV eligibility. The Facilities Service
atomically holds mock parking slots before Fleet vehicle claims; completed
arrivals mark slots occupied. Child dispatch outcomes are reported individually;
the existing single-EV path remains available.

**Not production-ready:** PostgreSQL snapshots provide local restart recovery,
but do not provide per-entity multi-replica locking or cross-service transactions.
Single replicas remain mandatory until those constraints and race tests exist.
No rental-driving simulator, real parking reservation, charging provider, or
live data integration is included.

See [Phase 5C guide](docs/phase5c-bulk-parking.md) for API usage,
assumptions, test commands and safety limitations.

## Optional customer rental experiment

The customer simulation is a **separate, finite Kubernetes Job in the same
`ev-fleet` namespace**, not another cluster or dashboard. It uses six dedicated
rental-capable simulated EVs, each with one authoritative telemetry owner.
Existing EV simulators remain unchanged. The Job exercises existing Fleet
rental APIs and generates movement, battery consumption and returns; returned
EVs can subsequently be considered for approved relocations.

Run after the base Kubernetes application is healthy:

```powershell
kubectl apply -f .\k8s\simulations\customer-job.yaml
kubectl -n ev-fleet logs -f job/customer-rental-simulation
```

It is disabled by default and deliberately does not alter Demand or
automatically dispatch cars. See
[customer rental simulation](docs/customer-rental-simulation.md) for scenario
settings, safe reruns, limitations and evaluation guidance.


For an **interactive demo where returned customer cars stay online and can be
relocated**, use the alternative persistent single-replica Deployment instead:

```powershell
kubectl apply -f .\k8s\simulations\customer-deployment.yaml
kubectl -n ev-fleet logs -f deployment/customer-rental-demo
```

Do **not** start the finite Job and Deployment together: they use the same
six vehicle IDs. See the simulation guide for safe stop/restart instructions.
