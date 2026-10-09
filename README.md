# Autonomous EV Fleet Rebalancing — Singapore

CSC3104 Cloud and Distributed Computing project. A distributed, simulation-only
system for selecting and relocating EVs to dispatcher-selected Singapore zones.
Recommendations do not reserve or dispatch vehicles. Phase 2 adds a separate
explicit approval operation and gradual autonomous relocation simulation.

## Included

- Independent Fleet, Demand and Rebalancing FastAPI services.
- Concurrent in-memory state updates, versioned snapshots and ordered telemetry.
- Stationary EV nodes that independently register and send periodic telemetry.
- Six representative Singapore zones: Jurong East, Woodlands, Bishan, Punggol,
  Tampines and CBD. Coordinates are approximate simulation points, not pickup bays.
- Configurable reserve exclusions and explainable multi-metric EV ranking.
- Health endpoints, unit/API tests and a real three-process HTTP integration test.
- Dispatch Service with atomic vehicle claims, idempotent commands and timeout reconciliation.
- Optional moving EV nodes with incremental position/SOC updates and verified arrival.
- Four-service real HTTP tests and repeatable container smoke checks.

Phase 3B adds a React dispatcher dashboard with a Leaflet/OpenStreetMap display.
No real-road autonomy, ML, maintenance/reservations, external charging integration,
database, Kubernetes deployment or experiment runner is included.

## Phase 3A demand and heat-map backend

Phase 3A now adds a [dynamic demand and heat-map backend](docs/phase3a-demand-heatmap.md)
on the existing Demand Service. `GET /api/v1/heat-map` combines demand with fresh
available/inbound Fleet capacity; simulation is opt-in and manual updates remain
supported. See [Phase 3A verification](docs/phase3a-verification.md).

## Phase 2 relocation demo

See [the Phase 2 guide](docs/phase2-dispatch.md) for approval requests, lifecycle,
failure handling, local startup and configuration. The original stationary demo
below remains supported. To start only moving nodes and all four services:

```powershell
docker compose --profile relocation up --build -d --wait fleet demand rebalancing dispatch ev-moving-01 ev-moving-02
```

Use the Dispatch API at <http://localhost:8004/docs> to explicitly approve an unexpired
recommendation. Acknowledgement, relocation, SOC consumption and arrival are then
simulated. Stop only this Compose project with `docker compose --profile relocation down`.
If another project occupies the default ports, configure FLEET_PORT, DEMAND_PORT,
REBALANCING_PORT and DISPATCH_PORT for this project.

## Local startup (PowerShell)

Python 3.10+ is supported; Docker uses Python 3.12. From this directory:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install --upgrade pip
.\.venv\Scripts\python -m pip install -e '.[test]'
```

Run each command in its own terminal:

```powershell
.\.venv\Scripts\python -m uvicorn ev_fleet.fleet.app:app --port 8001 --workers 1
.\.venv\Scripts\python -m uvicorn ev_fleet.demand.app:app --port 8002 --workers 1
.\.venv\Scripts\python -m uvicorn ev_fleet.rebalancing.app:app --port 8003 --workers 1
```

Then run two independent stationary nodes in separate terminals:

```powershell
.\.venv\Scripts\python -m ev_fleet.simulator.cli --vehicle-id ev-01 --zone bishan --soc 80
.\.venv\Scripts\python -m ev_fleet.simulator.cli --vehicle-id ev-02 --zone tampines --soc 35
```

All services expose `/docs`, `/openapi.json`, `/health/live` and `/health/ready`.
Local API docs: <http://localhost:8001/docs>, <http://localhost:8002/docs>,
<http://localhost:8003/docs>. Use one worker per state-owning service. State is
lost on restart. A rebalancing restart invalidates its recommendation records.

The application reads environment variables; it does not automatically load `.env`.
For local overrides, set variables in the service terminal, e.g. `$env:MIN_RESERVE_PCT = '25'`.
Defaults and variable names are listed in `.env.example`.

## Singapore demo

The Demand Service seeds the six zones at startup. Demand and charging observations
start unknown. Set synthetic CBD demand and charging availability:

```powershell
$demand = @{ expected_version = 0; observed_at = [DateTime]::UtcNow.ToString('o'); desired_available_vehicles = 12 } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://localhost:8002/api/v1/zones/cbd/demand -ContentType application/json -Body $demand
$charging = @{ expected_version = 0; observed_at = [DateTime]::UtcNow.ToString('o'); available_slots = 2 } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://localhost:8002/api/v1/zones/cbd/charging -ContentType application/json -Body $charging
Invoke-RestMethod http://localhost:8002/api/v1/hotspots
Invoke-RestMethod http://localhost:8001/api/v1/vehicles
Invoke-RestMethod -Method Post -Uri http://localhost:8003/api/v1/recommendations -ContentType application/json -Body '{"target_zone_id":"cbd"}' | ConvertTo-Json -Depth 12
```

For subsequent updates, retrieve the zone and use its latest `demand_version` or
`charging_version` as `expected_version`. These versions are independent.
Hotspots are zones with desired available vehicles >= 5 by default; they do not
yet represent demand minus available supply. Their observations include timestamps.

Timestamps use UTC on the wire. Future display and time-of-day demand scenarios
use `Asia/Singapore` (UTC+08:00). Distance is km, energy kWh and SOC percent.

## Docker Compose

```powershell
docker compose config --quiet
docker compose up --build -d
docker compose ps
```

Compose starts three services and two stationary EV containers. Run the demo
requests above and verify health, two fresh EVs, and an explainable recommendation.
Stop one EV with `docker compose stop ev-01`; after 15 seconds it must be excluded
as stale, and after 30 seconds Fleet reports it offline. Restart it with
`docker compose start ev-01` and check telemetry recovers. Stop the demo with
`docker compose down`.

Compose health checks gate startup only. Runtime requests still use timeouts,
bounded read retries and explicit dependency errors. To override defaults in
Compose, copy `.env.example` to `.env` and adjust the policy variables.
Container APIs bind to the host loopback interface; this is a trusted local demo.

## Verification

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pip check
```

The integration test launches three temporary Uvicorn processes on loopback ports,
registers two independent EV nodes, checks a farther high-SOC EV wins, verifies
recommendation expiry and stale exclusions, then shuts down its processes.
It requires permission to bind local sockets. It is not Docker validation.

Phase 2 adds a four-service integration test with two independent moving-node
processes, plus claim, lifecycle, failure-injection and movement tests. Original
Phase 1 tests remain unchanged. Container execution evidence is tracked separately.

See [architecture](docs/architecture.md), [API contracts](docs/api-contracts.md),
[roadmap](docs/implementation-plan.md), [Phase 1 verification](docs/verification.md),
and [Phase 2 verification](docs/phase2-verification.md).
## Phase 3B dispatcher dashboard

The React dashboard adds a Singapore demand map, fleet telemetry, recommendation review, explicit dispatch approval and live relocation monitoring. Backend business rules and service ownership remain unchanged.

See [dashboard setup and workflow](docs/phase3b-dashboard.md) and [verification status](docs/phase3b-verification.md).

```powershell
docker compose --profile relocation up --build fleet demand rebalancing dispatch ev-moving-01 ev-moving-02 dashboard
```

Dashboard: http://127.0.0.1:8080. Enable dynamic demand with `DEMAND_SIMULATION_ENABLED=true`, or use the existing manual demand API. For local development, run the existing backend and moving nodes, then `cd dashboard`, `npm.cmd ci`, and `npm.cmd run dev`.

## Phase 4 experimental evaluation

Phase 4 compares the unchanged energy-aware policy with a nearest-eligible-EV baseline using paired seeded scenarios, isolated state, repeated trials, raw JSON/CSV, confidence intervals, tables and SVG graphs. It does not change the application default or dispatch lifecycle.

See [the Phase 4 evaluation guide](docs/phase4-evaluation.md) and
[the verified results](docs/phase4-verification.md). Run the full 600-trial matrix with:

```powershell
.\.venv\Scripts\python.exe scripts\run_phase4_evaluation.py --repetitions 30
```
