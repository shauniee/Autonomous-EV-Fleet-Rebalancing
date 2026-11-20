# Architecture

How the services divide ownership, keep state consistent and handle failure.
All demand, vehicles, movement, rentals and charging are simulated. Nothing
controls a real vehicle.

## Components

All backend code is in `src/ev_fleet/`. Each service is a FastAPI app built by
`create_app()` in `<service>/app.py`, and all of them ship in one container image.

| Component | Port (host) | Owns |
|---|---|---|
| Fleet | 8001 | Vehicles, telemetry sessions, rentals, charging tasks, vehicle claims |
| Demand | 8002 | Zones, demand targets, heat map, optional demand generator |
| Rebalancing | 8003 | Recommendations (CRDS or legacy weighted), request queue, batches, traffic scenarios, charger advice |
| Dispatch | 8004 | Approvals, relocation commands and their lifecycle, recovery |
| Facilities | 8005 | Parking and charger catalogue, parking holds, cable handoff |
| EV simulators | none | Their own simulated position and battery, reported as telemetry |
| Traffic collector | none | Fetches LTA traffic feeds into Redis |
| CRDS workers | none | Optional HTTP scoring workers (`CRDS_EXECUTION_MODE=HTTP`) |

Two browser apps sit on top:

- The **operator dashboard** (`dashboard/`, port 8080) is how a person runs the
  fleet. See [operator dashboard](operator-dashboard.md).
- The **simulation console** (`simulation-console/`, port 8090) drives the
  simulated world. See [simulation console](simulation-console.md).

Shared code is limited to `contracts/` (validated Pydantic wire models that
forbid extra fields) and `common/` (settings, clock, persistence, travel maths,
the `base_app` factory).

## Ownership

Each state-owning service is the only writer of its data. Others read it over
HTTP.

- A service never imports another service's store or runtime state. Importing
  pure helper functions is allowed.
- Redis `DashboardCache` is display-only and fails open. Decisions never read it.
- The shared traffic cache in Redis is written only by the traffic collector.
  Rebalancing and Dispatch read it for route checks; a missing or old feed is
  reported as incomplete traffic coverage, never as a clear road.

## Consistency

- **One writer per service.** Each state-owning service runs as one replica with
  one worker. Its state lives in memory behind an `asyncio.Lock`, and no lock is
  held across network I/O.
- **Recommendation is not dispatch.** A recommendation reserves nothing. Only an
  explicit `POST /api/v1/dispatches` approval starts a relocation. Dispatch then
  revalidates the vehicle, route, battery, parking and CRDS benefit, takes a
  Facilities parking hold, and asks Fleet for a claim.
- **Optimistic claims.** A Fleet claim carries the expected Fleet instance and
  vehicle version. If either changed since the recommendation, the claim is
  refused (`VEHICLE_VERSION_CONFLICT`, `FLEET_INSTANCE_CHANGED`).
- **Idempotency.** Approvals, claims, holds, rentals and charging steps are keyed
  by request or dispatch ID. A retry returns the original receipt. A different
  body with the same ID is a 409.
- **Ordered telemetry.** Each simulator session has an ID and a rising sequence
  number. Fleet ignores exact repeats and rejects out-of-order samples and
  samples from a replaced session.
- **Freshness bounds.** Missing or old data is never guessed. Telemetry is stale
  after 15 s and offline after 30 s, demand expires after 300 s, and
  recommendations after 120 s. CRDS returns DEFER instead of MOVE when its inputs
  are incomplete.

## Failure handling

- **Fail-closed persistence.** With `DATABASE_URL` set, each service saves one
  JSONB snapshot per namespace to PostgreSQL after every change (Fleet and
  Dispatch also save per-record rows). If a save fails or its outcome is
  unknown, the service stops trusting RAM: every non-health request returns
  `503 PERSISTENCE_STATE_UNCERTAIN`, and the process exits with code 75 so
  Kubernetes restarts it and reloads the last committed snapshot.
- **Uncertain dispatch outcomes.** A lost acknowledgement or an ambiguous claim
  moves the dispatch to `UNKNOWN`. The claim is kept, never released on a guess.
  A reconciliation loop in Dispatch re-reads Fleet and Facilities until the
  outcome is known.
- **Lifecycles.** Vehicle: IDLE → DISPATCHED → RELOCATING → ARRIVED → IDLE.
  Dispatch: PENDING → SENT → ACKNOWLEDGED → IN_PROGRESS → SUCCEEDED, or REJECTED,
  FAILED or UNKNOWN. Recovery and resolution flows let an operator close stuck
  relocations explicitly.
- **Simulator recovery.** Moving simulators hold a lease and checkpoints in
  Fleet, so a restarted simulator Pod resumes without acting twice. See
  [telemetry](telemetry.md#restart-and-recovery).

## Communication

All service-to-service traffic is HTTP request and response. Simulators poll
Dispatch for commands about every second, and both browser apps poll the
services. Polling is a deliberate choice: it is at-least-once, works through
`kubectl port-forward`, and pairs with idempotent endpoints. There is no message
broker and no push channel.

The dashboard reaches the services through nginx, which maps
`/api/<service>/...` to `/api/v1/...` and blocks internal routes. See
[deployment](deployment.md).

## Conventions

- Timestamps are UTC in APIs and Asia/Singapore in the UIs. Units are km, kWh and SOC %.
- Errors are `{"error": {"code", "request_id"}}` with an `UPPER_SNAKE_CASE` code.
- Settings come from environment variables matching the field name in upper
  case (`Settings.from_env`), with prefixed groups such as `CRDS_*` and
  `DEMAND_*`. `.env.example` lists them.

## Further reading

- [CRDS](crds.md): the recommendation model, futures, and MOVE/HOLD/DEFER
- [Dispatch lifecycle](dispatch-relocation.md): approval, claims, bulk jobs
- [Demand and heat map](demand-heatmap.md)
- [Telemetry and digital twin](telemetry.md)
- [Parking, charging and handoff](facilities-and-charging.md)
- [OneMap routing](onemap-routing.md), [traffic-aware routing](traffic-aware-routing.md), [LTA DataMall](lta-datamall.md)
- [Docker, nginx and Kubernetes](deployment.md)
- [API contracts](api-contracts.md)
