# Architecture

How the services divide ownership, keep state consistent and handle failure.
For the component list, ports and main workflows, see the README sections
*Runtime components* and *Main workflows*. Topic details are linked below.
The earlier "initial service foundation" version of this page is in git history.

All demand, vehicles, movement, rentals and charging are simulated. Nothing
controls a real vehicle.

## Ownership

Each state-owning service is the only writer of its data. Others read it over HTTP.

| Owner | Authoritative state |
|---|---|
| Fleet | Vehicle registrations, telemetry sessions and sequence numbers, rentals, charging tasks, and the vehicle claim (who may move a car) |
| Demand | Zones, demand targets and their versions |
| Rebalancing | Short-lived recommendations, the CRDS request queue, traffic scenarios |
| Dispatch | Approvals, relocation commands and their lifecycle |
| Facilities | Parking and charger catalogue, parking holds |
| EV simulators | Their own simulated position and battery, reported as telemetry |

Rules:

- A service never imports another service's store or runtime state. Shared code is
  limited to `contracts/` (validated Pydantic wire models), `common/` (settings,
  clock, persistence, travel maths) and pure helper functions.
- Redis (`DashboardCache`, shared traffic cache) is display-only and fails open.
  Decisions never read it.

## Consistency

- **One writer per service.** Each state-owning service runs as one replica with
  one worker. Its state lives in memory behind an `asyncio.Lock`.
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
  number. Fleet ignores exact repeats and rejects out-of-order samples and samples
  from a replaced session.
- **Freshness bounds.** Missing or old data is never guessed. Telemetry is stale
  after 15 s and offline after 30 s, demand expires after 300 s, and
  recommendations after 120 s. CRDS returns DEFER instead of MOVE when its inputs
  are incomplete.

## Failure handling

- **Fail-closed persistence.** With `DATABASE_URL` set, each service saves one JSONB
  snapshot per namespace to PostgreSQL after every change. If a save fails or its
  outcome is unknown, the service stops trusting RAM: every non-health request
  returns `503 PERSISTENCE_STATE_UNCERTAIN`, and the process exits with code 75 so
  Kubernetes restarts it and reloads the last committed snapshot.
- **Uncertain dispatch outcomes.** A lost acknowledgement or an ambiguous claim
  moves the dispatch to `UNKNOWN`. The claim is kept, never released on a guess. A
  reconciliation loop in Dispatch (every second) re-reads Fleet and Facilities
  until the outcome is known.
- **Lifecycles.** Vehicle: IDLE → DISPATCHED → RELOCATING → ARRIVED → IDLE.
  Dispatch: PENDING → SENT → ACKNOWLEDGED → IN_PROGRESS → SUCCEEDED, or REJECTED,
  FAILED or UNKNOWN. Recovery and resolution flows let an operator close stuck
  relocations explicitly.
- **Simulator recovery.** Moving simulators hold a lease and checkpoints in Fleet,
  so a restarted simulator pod resumes its journey without acting twice.

## Communication

All service-to-service traffic is HTTP request/response. Simulators poll Dispatch
for commands about every second, and the dashboards poll the services. Polling is a
deliberate choice: it is at-least-once, works through `kubectl port-forward`, and
pairs with idempotent endpoints. There is no message broker and no push channel.

## Deployment (local Kubernetes)

Defined with Kustomize in `k8s/` and run on Docker Desktop Kubernetes
(`imagePullPolicy: Never`, so no managed cloud):

- Five backend Deployments (one replica each) with readiness, liveness and
  startup probes and resource limits.
- PostgreSQL StatefulSet and Redis.
- EV simulators: a StatefulSet of 18 plus two Deployments, and an optional
  customer-rental workload with its own RBAC.
- Traffic collector, operator dashboard (nginx), simulation console.
- One HorizontalPodAutoscaler, currently on the dashboard.
- Optional CRDS HTTP scoring workers (`k8s/crds-worker.yaml`) are **not** in the
  default deployment. The default `CRDS_EXECUTION_MODE` is `LOCAL`.

See [local Kubernetes deployment](kubernetes-local.md).

## Further reading

- [CRDS](crds.md): the recommendation model, futures, and HOLD/MOVE/DEFER
- [Dispatch lifecycle](dispatch-relocation.md)
- [API contracts](api-contracts.md)
- [Parking and charging facilities](parking-charging-facilities.md)
- [OneMap routing](onemap-routing.md)
