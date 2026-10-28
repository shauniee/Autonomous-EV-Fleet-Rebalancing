# Edge-owned EV digital twin, adaptive telemetry and history

This extension is developed on PR #13's existing branch. The simulator is
authoritative for physical state; the browser is **read-only**.

## Architecture and rates

- 20 standard EV simulator processes: one per Kubernetes Pod, with the
  existing 0.2 s movement loop and 1 s Dispatch command poll.
- Accepted moving/relocating telemetry: up to **once per real second**;
  simulated charging: up to **once per two seconds**;
  parked/idle: configurable `TELEMETRY_INTERVAL_SECONDS` (5 s default).
- State transitions (dispatch accepted, relocation started/arrived/failed,
  charging started/completed) set a wake-up event, causing an immediate
  telemetry attempt after the in-flight request finishes.
- Failed network sends are retried using the existing exponential backoff;
  the EV continues calculating local motion. Fleet's unchanged stale
  (15 s) and offline (30 s) thresholds reflect gaps without fake `OFFLINE`
  telemetry. All observations keep UUID session identity, increasing
  sequence, timestamp, physical GPS/SOC and dispatch ownership.
- Kubernetes/Compose demo time scale now defaults to **10×** instead of
  30×; configurable via `SIMULATION_TIME_SCALE`. Use `1` for
  real-time motion, or `5` for slower demonstration. Never redeploy an
  active journey just to change speed.

## Synthetic condition telemetry

The simulator includes speed, heading, cumulative odometer, battery and
motor temperatures, instantaneous power, state of health, GPS quality,
and fault codes. These are explicitly **SIMULATED**, not independent
hardware sensors or a calibrated battery thermal model. Power is
positive during traction and negative during charging. Current readings
are strictly validated by the Fleet telemetry contract; fields are
optional to preserve compatibility with the original stationary/rental
simulators and existing API clients.

The odometer and heading restore from the last accepted *idle* reading
when an EV simulator restarts. An unresolved journey is never resumed
by guessing physical location or generating new browser motion.

## Frequent telemetry and safe dispatch concurrency

At 1 Hz, Fleet increments the observation `state_version` for every accepted
sample. Comparing that field during dispatch approval would cause continual
`VEHICLE_VERSION_CONFLICT` errors. Fleet therefore also maintains a
`control_version`, incremented for session ownership changes, availability
or charging/relocation transitions, and rental/claim/task operations.
Dispatch binds its claim to that epoch, while Fleet **still rechecks** current
availability, rental/claim ownership, battery reserve and the claimed
OneMap route's start against the latest accepted physical position (strict
5 m origin match). Legacy callers that omit `expected_control_version`
retain their strict original `state_version` locking semantics. The
updated epoch is safely initialised when restoring older Fleet snapshots.

## PostgreSQL history and current state

The operational state remains the latest accepted telemetry stored
in the Fleet `service_snapshots` namespace. Separately, the
`telemetry_samples` PostgreSQL table stores append-only accepted
observations, uniquely keyed by `(vehicle_id, session_id, sequence)`.
Rows include observation/receipt timestamps and JSONB telemetry.
Snapshot and history inserts occur in the **same database transaction**.
Duplicate or rejected telemetry is not appended again.

A bounded inspection API is provided:

```
GET /api/v1/vehicles/{vehicle_id}/telemetry-history?limit=100
```

`limit` ranges from 1 to 500. The new Dashboard digital-twin panel
shows current metrics and SOC/speed history for the selected EV.
When running without `DATABASE_URL`, history is explicitly ephemeral
and capped at 500 observations per vehicle. Production Postgres
history has no retention/pruning policy yet: monitor disk usage and
plan retention, archive or time-based partitions for long-term
operation. Each 20-EV fleet publishing at 1 Hz generates up to
1.7 million samples/day if all cars remain moving continuously.

## Map animation is *only* a display effect

For an active road route, Leaflet checks the session/sequence of two
successive accepted Fleet observations and projects both onto the
approved OneMap geometry. Visual easing follows that path during a
short 800 ms transition; it does not extrapolate beyond the newer
observation or issue movement commands. Route projection is prepared
once per new reading, making individual animation frames inexpensive.
Missing geometry, off-route observations (>50 m) and stale data
disable easing; the last reported position remains visible with its
freshness indicator. Browser rendering cannot change any Fleet,
Dispatch or simulator state.

## Verification

Run `python -m pytest tests/unit/test_digital_twin.py -q`,
`npm test -- --run` in `dashboard`, and the normal repository
CI/Compose checks. Offline tests cover telemetry identity,
duplicate suppression, bounds, simulation motion and map interpolation.
Actual Kubernetes/PostgreSQL operations and live OneMap geometry
remain to be verified locally before merging/deploying.
