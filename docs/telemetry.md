# EV digital twin and telemetry

Each EV simulator is the only source of its car's physical state (position,
battery, speed). Fleet records what simulators report and decides what is
fresh. The dashboard only displays it.

## Simulators and reporting rates

The local Kubernetes deployment runs 20 moving EV simulators, one per Pod. Each
runs a 0.2 s movement loop and polls Dispatch for commands about once a second.
Telemetry is sent:

- up to once per real second while moving or relocating,
- up to once every two seconds while charging,
- every `TELEMETRY_INTERVAL_SECONDS` (5 s by default) while parked.

State changes (command accepted, relocation started, arrived or failed,
charging started or finished) trigger an immediate send. Failed sends retry with
exponential backoff while the EV keeps moving locally. Fleet's stale (15 s) and
offline (30 s) thresholds show the gap. Simulators never send fake `OFFLINE`
readings.

Every observation carries a UUID session ID, a rising sequence number, a
timestamp, GPS position, SOC and the dispatch it belongs to. Fleet ignores exact
repeats and rejects out-of-order samples and samples from a replaced session.

`SIMULATION_TIME_SCALE` sets how many simulated seconds pass per real second
(10 by default in Compose and Kubernetes).

## Simulated sensor fields

Telemetry also includes speed, heading, odometer, battery and motor
temperature, instantaneous power (positive when driving, negative when
charging), state of health, GPS quality and fault codes. These are labelled
**SIMULATED**; they are not a calibrated battery or thermal model. The fields are
optional so older simulators and API clients still validate.

## Restart and recovery

A restarted simulator restores odometer and heading from its last accepted idle
reading. It never resumes an unresolved journey by guessing where the car is.

Simulators hold an ownership lease and checkpoints in Fleet. A simulator can
automatically take back an EV whose previous owner expired, but only when that
EV is idle and not charging. Fleet first checks the saved checkpoint against the
last published observation, the simulation settings and current task and
Dispatch evidence. Position, SOC, odometer and heading are kept, and downtime
adds no travel or charge. Fresh telemetry is needed before the EV is available
again.

Active journeys, charging, rentals, failed dispatches, missing checkpoints and
unreachable dependencies all block automatic recovery. Those go through the
operator recovery flow in Dispatch.

## Frequent telemetry and claim versions

Fleet raises a vehicle's `state_version` on every accepted sample. At 1 Hz,
binding a dispatch claim to that number would cause constant
`VEHICLE_VERSION_CONFLICT` errors. Fleet therefore also keeps a
`control_version`. It only changes on session ownership changes,
availability, charging or relocation transitions, and rental, claim or task
operations.

Dispatch binds its claim to `control_version`. Fleet still rechecks current
availability, rental and claim ownership, battery reserve and that the route
starts within 5 m of the latest reported position. Callers that omit
`expected_control_version` get the original strict `state_version` check.

## History

The latest accepted telemetry is part of Fleet's snapshot. Every accepted
observation is also appended to the `telemetry_samples` table, keyed by
`(vehicle_id, session_id, sequence)`, in the same transaction as the snapshot.
Duplicates and rejected samples are not stored.

`GET /api/v1/vehicles/{vehicle_id}/telemetry-history?limit=100` (1–500) feeds
the dashboard's digital-twin panel. Without `DATABASE_URL`, history is in memory
and capped at 500 samples per vehicle. PostgreSQL history has no retention
policy: 20 EVs moving at 1 Hz all day would produce about 1.7 million rows.

## Smooth map movement (display only)

The dashboard polls Fleet every second and animates each EV between two
accepted readings along its approved route. It never shows a car past its
newest reading.

A step is animated only when the session ID matches, the sequence and timestamp
both increase, the gap is at most 4 seconds, and both positions lie within 50 m
of the EV's own approved route. The animation takes 90% of the gap between
readings, clamped to 250–1400 ms. Unrelated dashboard refreshes do not restart
an animation in progress.

If data is stale or offline, Fleet polling fails, the route is missing, or a
reading is off-route, the EV stays at its last confirmed position. After an
outage longer than 4 seconds the next reading is shown directly; the jump is an
honest correction. A failed Fleet poll shows a warning and greys out EV markers.
The browser has no control over Fleet, Dispatch or the simulators.
