# Dispatch lifecycle and EV relocation

Singapore simulation only. Creating a recommendation does not claim or move an
EV. A relocation starts only when an operator approves it with a separate POST
to Dispatch, quoting the recommendation ID and a caller-generated request UUID.
Fleet, Demand, Rebalancing, Dispatch and Facilities talk to each other over HTTP.

## Explicit approval

The operator picks a target zone, requests a recommendation, reviews it and
approves it before its `expires_at` (120 seconds by default). With
[CRDS](crds.md), only a `MOVE` with `approval_allowed=true` can be approved.

Approval works from the latest state, not the state at recommendation time.
Dispatch revalidates the vehicle, battery reserve, route, parking and CRDS
benefit against the final OneMap route and current commitments. It then takes a
Facilities parking hold and asks Fleet to claim the vehicle at its current
version. A race during that step returns 409 rather than silently approving a
different vehicle. An expired or changed recommendation needs a fresh
recommendation and a new request ID.

Approvals are idempotent:

- Repeating the exact approval body after a lost response returns the same dispatch.
- Reusing a request ID for a different recommendation is 409.
- Approving the same recommendation under a new request ID is 409 and names the existing dispatch.
- A first validation failure is 409. Its stored REJECTED record stays inspectable, and an identical retry returns it.
- A transient dependency failure can return a tracked UNKNOWN record with 202. A 202 means "tracked", not "moving".

## Ownership and state machines

Fleet owns atomic vehicle claims. A claim request carries the expected Fleet
instance ID, the expected vehicle `control_version` (see
[telemetry](telemetry.md#frequent-telemetry-and-claim-versions)), command and
dispatch IDs, target, expiry and energy-reserve parameters. Fleet checks
freshness, availability, rentals, existing claims, battery reserve and that the
route starts at the vehicle's latest reported position, all inside its store
lock. No network I/O happens while that lock is held.

Dispatch stores the exact claim request before sending it. Lost responses are
retried with the same identity and body, and Fleet returns its original receipt
even after the recommendation expires. Fleet keeps release receipts so a delayed
retry cannot resurrect a completed claim.

Reported telemetry is kept separate from cloud ownership.
`effective_availability` is UNAVAILABLE whenever a claim exists, and
`effective_relocation_state` shows DISPATCHED while the EV still reports IDLE.

    AVAILABLE -> DISPATCHED -> RELOCATING -> ARRIVED -> AVAILABLE

The final AVAILABLE needs fresh IDLE telemetry after a verified claim release.
Fleet sets `current_zone_id` on arrival, clears it during relocation, and leaves
it unknown before the first confirmed arrival.

    PENDING -> SENT -> ACKNOWLEDGED -> IN_PROGRESS -> ARRIVAL_PENDING -> SUCCEEDED

Dispatch also has REJECTED, FAILED and UNKNOWN. Fresh telemetry that matches the
command can establish progress without a separate ACK. Arrival must match the
dispatch and target within `ARRIVAL_TOLERANCE_KM` (0.05 by default), and an
ARRIVED event alone cannot release a vehicle without matching fresh Fleet
telemetry.

An explicit command rejection releases the claim only after matching IDLE
telemetry at the origin. A confirmed execution failure keeps the claim. There is
deliberately no force-release or automatic reassignment on timeout; stuck
journeys go through the operator recovery and resolution flows.

## Communication and failure handling

EVs poll Dispatch for commands, and repeated polls return the same command ID.
Each EV remembers accepted and completed command IDs so it never repeats a move.
Duplicate ACKs must match the original. Events carry a per-dispatch sequence
number: exact repeats are idempotent, while conflicting or out-of-order events,
shrinking distance and backward lifecycle steps are rejected.

Dispatch locks per record around its coordination steps, so unrelated requests
proceed in parallel. A background loop reconciles open dispatches, and every
dependency call has a 2-second timeout.

`ACK_TIMEOUT_SECONDS` (15) starts when the claim is acquired, even if the EV has
never polled, and redelivery does not reset it. `PROGRESS_TIMEOUT_SECONDS` is 30.
Unacknowledged, stale, offline or unreachable outcomes become UNKNOWN while the
claim is kept. `claim_held: null` means a claim attempt had an ambiguous result,
not that the vehicle is free. Fresh telemetry that matches the command can move
an UNKNOWN record back to IN_PROGRESS or ARRIVED.

The simulator moves in its own asyncio task on monotonic time, so a network
outage does not pause the simulated journey. It backs off and resends fresh
telemetry and events after reconnecting. A restarted simulator refuses to
overwrite an unresolved journey with its start-up coordinates.

## Distance and battery

The approved route comes from OneMap (or a labelled `OFFLINE_DEMO` straight line,
see [OneMap routing](onemap-routing.md)). The simulator follows that route
geometry, advancing by speed × elapsed simulated time and never past the end.
Energy used is distance travelled × kWh/km, converted to SOC using battery
capacity. Forecasts keep a conservative energy margin, so predicted arrival SOC
can be lower than the observed one. A battery that unexpectedly runs out makes
the dispatch FAILED and keeps the claim.

## Multi-EV plans and parking holds

For demand rebalancing, CRDS plans requests of 1 to 50 EVs. It chooses a
compatible destination per EV, verifies road evidence, and updates modelled
supply and remaining facility capacity after each proposed move. A plan can
contain fewer assignments than requested.

Two planning modes exist:

- **Optimise coverage** (`OPTIMIZE_COVERAGE`, the API default) proposes only
  moves that clear the useful-gain threshold.
- **Fulfil exact quantity** (`FULFIL_REQUEST`) keeps going past that threshold
  and may take the last available EV from a donor zone. The UI shows the signed
  coverage change and warns about any step that does not pay off. It never
  plans with stock that does not exist, and cars already assigned to the
  destination count towards the quantity.

A capacity-aware matching step spreads EVs across compatible car parks instead
of sending them all to the closest one. Ordinary and charger bays are matched
separately so chargers stay free for EVs that need them.

Approval creates one child dispatch per assignment, each with its own identity.
Facilities reserves simulated space before Fleet claims the EV. A definitively
rejected claim releases its parking hold, while an ambiguous one keeps it until
reconciliation. Verified arrival turns the hold into occupied capacity, which
stays occupied until the car leaves (for example at the start of a rental).

With `CRDS_MODE=DISABLED`, the legacy weighted planner is used instead. It
scores each EV and facility pair as 85% energy-aware score plus 15% available
parking ratio and picks pairs greedily. EVs predicted to arrive below 35% SOC,
and every explicit `CHARGE` request, need a free charging bay.

## Paced bulk jobs

A bulk approval creates a durable job instead of holding the browser request
open while every vehicle is admitted. The operator approves the exact
assignments while the recommendation is fresh, and that authorisation lasts up
to 30 minutes (`BATCH_AUTHORIZATION_SECONDS`, maximum 1800).

Dispatch then admits the assignments gradually. By default at most four
relocating or uncertain journeys run at once (`DISPATCH_IN_FLIGHT_LIMIT`), with
at least one second between admissions (`DISPATCH_ADMISSION_INTERVAL_SECONDS`).
Single approvals take slots too. Each admission rechecks the vehicle, route,
battery reserve, ownership, facility capacity and CRDS policy. Dispatch never
swaps in another EV or destination.

Transient dependency, routing, rate-limit and planning-timeout failures retry
with bounded backoff. Permanent safety or policy failures stay visible. Stable
child request and claim IDs prevent duplicate claims or holds across retries
and restarts. After authorisation expires, no new claims or holds are created,
but known effects still reconcile. Queued items that have not started can be
cancelled; an item that may have touched Fleet or Facilities must first be
reconciled.

### Planning revision

CRDS checks a global planning revision during approval and retries once if it
changed. The revision only advances for things that change commitments:
admissions, claims, parking, arrival, failure, recovery and entering or leaving
UNKNOWN. Routine distance updates and the normal SENT, ACKNOWLEDGED and
IN_PROGRESS steps of an already-admitted journey do not advance it. Without
this, ordinary movement of one EV would invalidate every other child in a batch.

## Durable storage

Fleet and Dispatch write each vehicle or dispatch as its own row in
`snapshot_records`, plus compact metadata in `service_snapshots`. Metadata,
changed rows, deletions and telemetry history commit in one PostgreSQL
statement, and a response is only sent after the commit. An ambiguous write
fences the service until durable state is reloaded (see
[architecture](architecture.md#failure-handling)).

Fleet groups frequent checkpoints and lease renewals into shared transactions
(up to 64 vehicles per transaction, 2 ms collection window). Dispatch batches
overlapping observations into one durable frame the same way. Unchanged command
polls do not write anything.

## APIs

Dispatch exposes OpenAPI at `/docs` and `/openapi.json` on port 8004.

| Method and path under `/api/v1` | Purpose |
|---|---|
| POST /dispatches | Approve one recommendation (`recommendation_id`, `request_id`); 202 tracked record |
| GET /dispatches/{id} | Reconcile and return state, progress, metrics and errors |
| GET /dispatches?vehicle_id=... | Read stored records |
| POST /batches | Approve a batch recommendation (`request_id`, `batch_recommendation_id`); may be PARTIAL |
| GET /batches/{request_id} | Saved batch receipt |
| GET /vehicles/{id}/commands | Simulator command poll; `commands: []` when none |
| POST /dispatches/{id}/acknowledgements | Simulator ACK |
| POST /dispatches/{id}/events | Simulator progress events |

Rebalancing serves `POST /api/v1/batch-recommendations` and
`GET /api/v1/batch-recommendations/{id}`. Fleet's claim endpoints
(`/vehicles/{id}/claims/{dispatch_id}` and `.../release`) and the Facilities hold
endpoints are internal. The dashboard's nginx blocks them from the browser (see
[deployment](deployment.md#nginx-in-the-dashboard)).

## Limits

There is no authentication. Delivery is at-least-once with idempotent receivers,
not exactly-once. State-owning services must run as one replica with one worker.
Parking holds are simulated allocations and give no real parking or charging
rights.
