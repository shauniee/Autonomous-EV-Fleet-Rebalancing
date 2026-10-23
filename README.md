# Autonomous EV Fleet Rebalancing - Singapore

CSC3104 Cloud and Distributed Computing project. This repository contains a
distributed, simulation-only system that helps a human dispatcher move available
electric vehicles to Singapore zones with insufficient supply.

The system observes simulated demand and vehicle telemetry, recommends a suitable
EV, requires explicit dispatcher approval, and then simulates the EV travelling to
the selected zone. It does not control real vehicles and does not use live demand,
traffic, road-routing, or charging-station data.

## What the project demonstrates

- Four independently running FastAPI services communicating over HTTP.
- Simulated EV processes that register and continuously publish ordered telemetry.
- Six representative Singapore zones: Jurong East, Woodlands, Bishan, Punggol,
  Tampines, and CBD.
- Demand, available supply, projected supply, shortages, and hotspot severity.
- Explainable energy-aware EV selection with eligibility and reserve checks.
- Human-approved dispatch, atomic vehicle claims, idempotent commands, gradual
  movement, SOC consumption, arrival verification, and timeout reconciliation.
- A React/TypeScript dashboard with a Leaflet/OpenStreetMap display.
- Docker Compose and local Docker Desktop Kubernetes deployment options.
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
| EV simulators | Simulated physical position, SOC, command polling, and movement | None |
| Dashboard | Operator view, recommendation review, and explicit approval | 8080 |

Runtime state is exchanged only through service APIs. One service does not import
another service's in-memory store.

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

## Quick start: complete Docker Compose demonstration

### Prerequisites

- Docker Desktop with Docker Compose
- PowerShell

From the repository root, start all four services, two moving EV simulators, and
the dashboard:

```powershell
docker compose --profile relocation up --build -d --wait `
  fleet demand rebalancing dispatch ev-moving-01 ev-moving-02 dashboard
```

Check that the containers are healthy:

```powershell
docker compose --profile relocation ps
```

Open the dashboard at <http://127.0.0.1:8080>.

Demand starts unknown unless the optional demand simulator is enabled. For a
repeatable manual demonstration, create a CBD hotspot through the dashboard proxy:

```powershell
$zone = Invoke-RestMethod http://127.0.0.1:8080/api/demand/zones/cbd
$body = @{
  expected_version = $zone.demand_version
  observed_at = [DateTime]::UtcNow.ToString('o')
  desired_available_vehicles = 12
} | ConvertTo-Json

Invoke-RestMethod -Method Put `
  -Uri http://127.0.0.1:8080/api/demand/zones/cbd/demand `
  -ContentType application/json `
  -Body $body
```

Then use the dashboard:

1. Select **CBD**.
2. Select **Recommend EV**.
3. Review the ranking, distance, energy, and predicted arrival SOC.
4. Select **Review dispatch**.
5. Select **Approve Dispatch**.
6. Watch the relocation change from pending to in progress and then succeeded.
7. Confirm that the EV finishes in CBD and becomes available again.

Follow the moving components in PowerShell if needed:

```powershell
docker compose --profile relocation logs -f dispatch ev-moving-01 ev-moving-02
```

Stop only this Compose project:

```powershell
docker compose --profile relocation down
```

## Other ways to run it

### Stationary Phase 1 demonstration

The default Compose configuration starts Fleet, Demand, Rebalancing, and two
stationary EVs. It does not start Dispatch or the dashboard.

```powershell
docker compose up --build -d --wait
docker compose ps
docker compose down
```

API documentation is then available at:

- Fleet: <http://127.0.0.1:8001/docs>
- Demand: <http://127.0.0.1:8002/docs>
- Rebalancing: <http://127.0.0.1:8003/docs>

### Native Python and dashboard development

Python 3.10 or newer is supported. Docker uses Python 3.12. Install the backend:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e '.[test]'
```

Run these services in separate PowerShell terminals:

```powershell
.\.venv\Scripts\python.exe -m uvicorn ev_fleet.fleet.app:app --port 8001 --workers 1
.\.venv\Scripts\python.exe -m uvicorn ev_fleet.demand.app:app --port 8002 --workers 1
.\.venv\Scripts\python.exe -m uvicorn ev_fleet.rebalancing.app:app --port 8003 --workers 1
.\.venv\Scripts\python.exe -m uvicorn ev_fleet.dispatch.app:app --port 8004 --workers 1
```

Run two moving EVs in two more terminals:

```powershell
.\.venv\Scripts\python.exe -m ev_fleet.simulator.cli --dispatch-url http://127.0.0.1:8004 --vehicle-id ev-moving-01 --zone bishan --soc 90 --time-scale 30
.\.venv\Scripts\python.exe -m ev_fleet.simulator.cli --dispatch-url http://127.0.0.1:8004 --vehicle-id ev-moving-02 --zone tampines --soc 60 --time-scale 30
```

Install and run the development dashboard:

```powershell
Set-Location dashboard
npm.cmd ci
npm.cmd run dev
```

Open <http://127.0.0.1:5173>. Vite proxies browser API requests to ports
8001-8004, so backend CORS changes are not required.

### Local Kubernetes

The repository includes one-replica Kubernetes Deployments, internal ClusterIP
Services, ConfigMaps, health probes, and local-demo resource limits under `k8s/`.
It uses locally built images with Docker Desktop Kubernetes and does not require a
container registry or cloud service.

Follow [the local Kubernetes guide](docs/kubernetes-local.md) for image builds,
namespace creation, deployment, port-forwarding, verification, Pod recovery, logs,
troubleshooting, and teardown.

## Demand options

Manual demand is recommended for a deterministic demonstration. Dynamic demand is
optional and disabled by default. Enable it before starting Compose with:

```powershell
$env:DEMAND_SIMULATION_ENABLED = 'true'
docker compose --profile relocation up --build -d --wait `
  fleet demand rebalancing dispatch ev-moving-01 ev-moving-02 dashboard
```

Compose reads `.env` automatically. Native Python processes do not; set their
environment variables in the relevant terminal. All supported settings and defaults
are listed in [.env.example](.env.example).

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
ports. It does not reuse the Compose or Kubernetes deployment.

Validate Compose configurations without starting containers:

```powershell
docker compose config --quiet
docker compose --profile relocation config --quiet
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
src/ev_fleet/fleet/        Fleet API and in-memory vehicle/claim state
src/ev_fleet/demand/       Demand API, simulation, and heat-map composition
src/ev_fleet/rebalancing/  Eligibility, strategies, ranking, and recommendations
src/ev_fleet/dispatch/     Approval and relocation coordination
src/ev_fleet/simulator/    Stationary and moving simulated EV processes
dashboard/                 React dashboard, Nginx proxy, and frontend tests
k8s/                       Local Kubernetes resources
tests/                     Python unit and integration tests
experiments/phase4/        Reproducible evaluation runner and analysis
scripts/                   Smoke, browser-test, and evaluation entry points
docs/                      Architecture, contracts, phase guides, and evidence
results/                    Preserved Phase 4 evaluation output
```

## Important limitations

- Fleet, Demand, Rebalancing, Dispatch, and simulator state is held in process
  memory. Restarting a process or Pod can lose the state owned by that process.
- Kubernetes can replace a failed Pod, but that does not recover lost in-memory
  dispatch history.
- State-owning services and individual EV simulators must remain at one worker and
  one replica until durable shared state and cross-replica coordination exist.
- There is no authentication or authorization. Internal APIs must not be exposed to
  an untrusted network.
- Coordinates, demand, charging, movement, and arrival are simulated. There is no
  real-road autonomy, live map routing, ML, persistent database, maintenance model,
  reservation system, or external charging integration.
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
