# Phase 2: dispatch lifecycle and autonomous EV relocation simulation

Singapore simulation only. Recommendation creation does not claim an EV or move it.
Dispatch approval is a separate POST, using an existing recommendation ID and a
caller-generated request UUID. Four independent services communicate over HTTP.

## Run locally

Start the three Phase 1 services as in README, then a fourth terminal:

```powershell
.\.venv\Scripts\python -m uvicorn ev_fleet.dispatch.app:app --port 8004 --workers 1
```

Use new vehicle IDs for moving nodes, or stop stationary nodes before reusing their
IDs. A vehicle ID belongs to one simulator process. In separate terminals:

```powershell
.\.venv\Scripts\python -m ev_fleet.simulator.cli --vehicle-id ev-moving-01 --zone bishan --soc 90 --dispatch-url http://localhost:8004
.\.venv\Scripts\python -m ev_fleet.simulator.cli --vehicle-id ev-moving-02 --zone tampines --soc 60 --dispatch-url http://localhost:8004
```

The original CLI without `--dispatch-url` remains stationary. Moving mode supports
`--speed-kph` (30), `--time-scale` (30 simulated seconds/real second), `--tick-seconds`
(0.2), `--poll-interval` (1), and existing `--interval` for telemetry (5 seconds).
`--updates` is only valid for stationary mode.

## Compose relocation profile

For a clean dedicated demo, start only moving nodes and their dependencies:

```powershell
docker compose --profile relocation up --build -d --wait fleet demand rebalancing dispatch ev-moving-01 ev-moving-02
```

Default `docker compose up` preserves the Phase 1 stationary service set. An
unqualified `--profile relocation up` also starts the default stationary nodes;
use the explicit service list above to avoid recommending a stationary node.
Do not mix independently running simulators with the same vehicle IDs.

Host ports are configurable with FLEET_PORT, DEMAND_PORT, REBALANCING_PORT and
DISPATCH_PORT; defaults remain 8001..8004. This lets a verification project run
alongside an existing project. Internal Compose URLs keep using service DNS/8000.

## Explicit approval

Select a Singapore hotspot/zone through the existing API, request a recommendation,
inspect it, then approve before its existing 10-second expiry:

```powershell
$recommendation = Invoke-RestMethod -Method Post -Uri http://localhost:8003/api/v1/recommendations -ContentType application/json -Body '{"target_zone_id":"cbd"}'
$approval = @{ request_id = [guid]::NewGuid().ToString(); recommendation_id = $recommendation.recommendation_id } | ConvertTo-Json
$dispatch = Invoke-RestMethod -Method Post -Uri http://localhost:8004/api/v1/dispatches -ContentType application/json -Body $approval
Invoke-RestMethod "http://localhost:8004/api/v1/dispatches/$($dispatch.dispatch_id)" | ConvertTo-Json -Depth 12
```

An expired/changed recommendation requires a fresh recommendation and new request
ID. Normal telemetry after recommendation is allowed: approval checks the latest
state, then atomically claims its current version. A race during that operation
returns 409 rather than silently approving another vehicle.

Repeat the exact approval body after a lost response: it identifies the same
dispatch. Reusing a request ID for another recommendation is 409. Approving the same
recommendation under a new request ID is 409 with the existing dispatch ID. A first
validation failure is 409; its stored REJECTED record remains inspectable and an
identical retry returns that record. Transient dependency failures can return a
tracked UNKNOWN record with 202; inspect state rather than treating 202 as movement.

## Ownership and state machines

Fleet owns atomic vehicle claims. A claim request includes expected Fleet instance
ID, expected vehicle version, command/dispatch IDs, target, expiry and reserve/energy
parameters. Fleet checks freshness, availability, existing claims and energy reserve
inside its store lock. It applies at least its own configured safety parameters.
No network I/O occurs while that lock is held.

Dispatch stores the exact claim request before sending it. Lost responses are retried
with the same identity/body; Fleet returns its original receipt even after the
recommendation expires. An unexecuted expired request cannot acquire a new claim.
Fleet keeps release receipts so delayed retries cannot resurrect completed claims.

Reported telemetry stays separate from cloud ownership. `effective_availability`
is UNAVAILABLE whenever a claim exists; `effective_relocation_state` shows DISPATCHED
while an EV has a claim but still reports IDLE. Connectivity remains independent.

    AVAILABLE -> DISPATCHED -> RELOCATING -> ARRIVED -> AVAILABLE

The final AVAILABLE corresponds to fresh IDLE telemetry after verified claim release.
Fleet confirms `current_zone_id` at arrival, clears it during relocation, and leaves
it unknown before the first confirmed arrival. Initial CLI zone names choose seed
coordinates; they are not a trusted zone-membership assignment.

    PENDING -> SENT -> ACKNOWLEDGED -> IN_PROGRESS -> ARRIVAL_PENDING -> SUCCEEDED

Dispatch also supports REJECTED, FAILED and UNKNOWN. A matching fresh telemetry
observation may establish progress without a separately received ACK. Physical
arrival must match the dispatch and target within ARRIVAL_TOLERANCE_KM (0.05 default).
Arrival telemetry may complete finalization before an explicit arrival event reaches
Dispatch. Final distance can also be recovered from telemetry. An ARRIVED event
alone cannot release a vehicle without matching fresh Fleet telemetry.

Explicit command rejection releases a claim only after matching IDLE telemetry at
the origin. Confirmed execution failure retains the claim. Phase 2 deliberately
does not expose an unsafe force-release or automatic reassignment on timeout.

## Communication and failure handling

EVs poll for commands; repeated polls return the same command ID. EV memory stores
accepted/completed IDs to avoid repeat movement. ACK duplicates must match their
original content. Events have per-dispatch sequence numbers: exact repeats are
idempotent; conflicting/out-of-order events, regressing distance and backward
lifecycle progress are rejected.

Dispatch uses a per-record operation lock around its coordination steps, allowing
unrelated HTTP handlers to proceed. Its background loop retries reconciliation at
a configurable interval; each dependency HTTP call uses a 2-second per-I/O timeout.
This is not a global hard deadline. In-memory state owners still need one worker.

ACK_TIMEOUT_SECONDS defaults to 15; PROGRESS_TIMEOUT_SECONDS defaults to 30.
The acknowledgement timer starts at claim acquisition, including commands that
have never been polled. Redelivery does not reset that timer.
Unacknowledged, offline/stale or unreachable outcomes become UNKNOWN while keeping
ownership. `claim_held: null` means a claim attempt had an ambiguous result, not that
the vehicle is available. A confirmed claim is true until an idempotent release.
Fresh command-matching telemetry can recover IN_PROGRESS/ARRIVED after disconnection.

Movement uses an independent asyncio task and monotonic elapsed time. Network
outages do not pause physical simulation; network tasks back off and send fresh
telemetry/events after reconnect. A newly started simulator refuses to overwrite an
unresolved journey with initial CLI coordinates. Duplicate session identities are
treated as conflicts, not as a reason to steal another node's session.

## Distance and SOC

Route length is the Phase 1 geographic distance multiplied by route factor. Position
interpolates along a synthetic segment according to fractional route completion;
this is not a road route, animation or external maps integration. Incremental
distance is speed times elapsed simulated time, clamped to the remaining route.

Energy consumed is actual simulated distance times kWh/km, converted to SOC using
battery capacity. Forecasts retain the conservative energy margin, so predicted
arrival SOC can be lower than observed SOC. Zero-distance moves consume no travel
energy; an unexpected depleted battery causes FAILED and leaves the claim held.

## APIs

Dispatch exposes typed OpenAPI at port 8004 `/docs` and `/openapi.json`.

| Method and path under /api/v1 | Purpose |
|---|---|
| POST /dispatches | Approve with recommendation_id and request_id; 202 tracked record |
| GET /dispatches/{id} | Reconcile and return state, progress, metrics and errors |
| GET /dispatches?vehicle_id=... | Read stored records; background reconciliation updates them |
| GET /vehicles/{id}/commands | Poll current command, returning commands: [] when none |
| POST /dispatches/{id}/acknowledgements | command_id, vehicle_id, accepted, optional reason |
| POST /dispatches/{id}/events | command_id, vehicle_id, sequence, observed_at, kind, distance_travelled_km, optional reason |

Fleet internal operations: PUT/GET `/vehicles/{id}/claims/{dispatch_id}` and POST
`/vehicles/{id}/claims/{dispatch_id}/release`. Existing Fleet and recommendation APIs
remain available; claims are an additional BUSY exclusion and do not change scores.
New optional telemetry `distance_travelled_km` supports completion reconciliation.

## Verification and limits

Run `python -m pytest -q` using the project virtual environment. The original 37 tests
remain unchanged. New tests cover claims, concurrency, idempotency, lifecycle,
response loss, stale/offline EVs, insufficient SOC, incremental motion, network-outage
movement, and four real backend processes with two separate moving-node processes.

For a repeatable smoke test of an already-running moving demo:

```powershell
.\.venv\Scripts\python scripts/phase2_smoke.py --base-port 8001 --output docs/evidence/my-phase2-smoke.json
```

This command explicitly approves one simulated relocation and records raw API
observations. It is an acceptance check, not an experiment framework.

State, command deduplication and receipts remain in memory and are capped only for
dispatch admission (10,000 records). Use finite demonstration populations. There
is no authentication, durable journal, exactly-once delivery or guaranteed recovery
after arbitrary combined restarts. A Fleet instance change invalidates ownership
assumptions; unresolved journeys stay blocked. Persistence and operational recovery
remain later work. No frontend, ML, reservations, maintenance, external maps/charging
APIs, Kubernetes deployment or real-road autonomy has been added.
