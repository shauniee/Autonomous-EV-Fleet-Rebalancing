# Autonomous EV Fleet Rebalancing - Singapore

CSC3104 Cloud and Distributed Computing project. This repository contains a
distributed, simulation-only system that helps a human dispatcher move available
electric vehicles to Singapore zones with insufficient supply.

The system observes simulated demand and vehicle telemetry, recommends a suitable
EV, requires explicit dispatcher approval, and then simulates the EV travelling to
the selected zone. It does not control real vehicles and does not use live demand,
traffic, road-routing, or charging-station data.

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
- A separate simulation console for setting demand and observing service state.
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

## Kubernetes startup

### Prerequisites

- Docker Desktop with Kubernetes enabled and Linux containers
- `kubectl` using the `docker-desktop` context
- PowerShell
- Host ports 8080 and 8090 available

This is the only startup workflow. From the repository root, run one command:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

The script validates Docker Desktop, builds the three application images from
the checked-in dependency lock, deploys PostgreSQL, Redis, every backend, both
dashboards and all 20 EV simulators, waits for readiness, and opens both local
port-forward tunnels. Keep that PowerShell window open.

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
- Coordinates, demand, charging, movement, and arrival are simulated. There is no
  real-road autonomy, live map routing, ML, maintenance model, real reservation
  system, or external charging integration.
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
