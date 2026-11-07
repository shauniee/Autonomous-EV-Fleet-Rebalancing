# Continuous Traffic Rerouting Design

## Intent

An EV that is already relocating should behave like a cautious GPS. When a road closure affects its remaining path, Dispatch should automatically replace that path with the best validated safe alternative. When congestion affects the remaining path, Dispatch should replace it only when the alternative is materially better. The dashboard should immediately show the active replacement route instead of leaving the original line on the map.

## Goals

- Reassess every active, acknowledged relocation against fresh live or approved simulated traffic.
- Reroute around a confirmed closure whenever a safe road route exists.
- Reroute for congestion when the replacement ETA is either at least 10 percent better or at least 60 seconds better than the assessed ETA of the current remaining path.
- Apply a 30-second cooldown after a successful congestion reroute so small traffic changes cannot make the EV oscillate between routes. A newly detected closure overrides the cooldown.
- Route from the EV's latest confirmed road-aligned telemetry position to the existing destination.
- Preserve the strict 5-metre route-origin check, the destination/parking hold, the Fleet claim, the command identity, SOC reserve rules, and road-only movement.
- Persist each accepted route revision and make simulator and dashboard consumption idempotent.
- Stop on the affected road when no safe alternative is available; never manufacture a straight-line connector.

## Non-goals

- Changing the destination, parking facility, parking hold, recommendation, Fleet claim, or dispatch command identity.
- Adding real vehicle control, lane-level navigation, or turn-by-turn voice guidance.
- Weakening endpoint, arrival, telemetry freshness, traffic freshness, or energy validation.
- Treating a dashboard redraw as authority to move an EV.
- Claiming multi-replica Dispatch safety; this checkout retains the existing single-replica snapshot model.

## Constraints

- OneMap remains the primary road-route provider. Configured OSRM alternatives may be considered, but every candidate must be assessed by the server-side full traffic feed.
- A revision's first coordinate must be within 0.005 km of the latest Fleet telemetry position. Facility endpoints retain the existing facility access tolerance; ordinary destinations retain the existing arrival tolerance.
- The route must keep the current target and must leave the EV above the existing operational SOC reserve after the remaining trip.
- Traffic or routing provider failures must leave the last accepted route unchanged. A closure already aligned with the current simulator segment continues to stop movement until a valid revision is accepted or the closure clears.
- Guaranteed alternate-route discovery requires a configured provider that can return distinct road geometries. The existing OneMap integration supplies one route and does not accept a closure-avoidance constraint; configured OSRM currently supplies the candidate alternatives.
- Existing user changes in the dirty worktree must be preserved.

## Approaches Considered

### 1. Aggressive best-route switching

Recalculate continuously and switch whenever any candidate has a lower score. This most literally follows the instantaneous best route, but minor feed changes can repeatedly replace the route and make both movement and the map flicker.

### 2. Validated switching with hysteresis and cooldown — selected

Closures trigger an immediate search. Congestion triggers a switch only for a material ETA improvement, followed by a short cooldown. This provides GPS-like behavior without route thrashing and preserves the project's safety validations.

### 3. Operator-approved rerouting

Continue reporting `OPERATOR_REVIEW_REQUIRED` and wait for a person to approve a replacement. This is safer operationally but does not meet the requested automatic GPS behavior.

## Design

### Route revision contract

The original `RelocationCommand` and `command_id` remain immutable. Add a separate `RouteRevision` value containing:

- `dispatch_id`, `command_id`, and `vehicle_id` for identity binding;
- a monotonically increasing `revision` number;
- `issued_at`, `reason` (`ROAD_CLOSED` or `CONGESTION`), and the validated `RoadRoute`;
- the cumulative journey distance when the revision begins, used to calculate progress against the new remaining route;
- the assessed old and new remaining ETAs for audit and dashboard explanation.

The Dispatch record stores the latest revision and a bounded revision history. The existing initial command payload is never rewritten. Snapshot persistence therefore recovers the active revision after a Dispatch restart without violating command idempotency.

`RoadRoute.duration_seconds` continues to represent the traffic-adjusted ETA consumed by the simulator and dashboard. Add an optional provider-baseline duration captured before traffic scoring, and use that baseline for every later reassessment. This prevents repeated traffic checks from applying delay to an ETA that was already traffic-adjusted.

### Automatic reassessment

The Dispatch reconciliation worker examines active, acknowledged journeys on a bounded interval. Under the record's existing lock it:

1. Reads a fresh Fleet snapshot and rejects stale, missing, mismatched, or off-recorded-route telemetry.
2. Determines the active route (latest revision or initial command), its current remaining geometry, and the traffic assessment for that geometry.
3. Does nothing when traffic coverage is not fresh or no material disruption is recorded.
4. For a closure, searches immediately even during the congestion cooldown.
5. For congestion, searches only when congestion affects the remaining route and the cooldown permits it.
6. Requests a new OneMap route from the current position to the unchanged destination and asks the existing traffic-aware selector to score it plus configured OSRM alternatives.
7. Compares the best safe candidate with the current remaining route. A closure accepts any safe candidate. Congestion accepts a candidate when either its assessed ETA improves by at least 10 percent or the absolute saving is at least 60 seconds.
8. Revalidates the 5-metre origin, destination endpoint, Fleet/vehicle identity, telemetry position, current SOC, energy margin, and minimum reserve after routing completes.
9. Persists the new revision before it can be returned to the simulator or dashboard.

Concurrent or repeated checks observe the same record lock, revision number, cooldown timestamp, and telemetry sample, so the same traffic observation cannot create duplicate revisions.

### Simulator application

The simulator continues polling its Dispatch record. When it sees a higher route revision for its active command, it validates the dispatch, command, vehicle, monotonic revision, unchanged destination, permitted route source, route start within 5 metres, and local SOC reserve before applying it.

Movement gains an explicit route-local progress value while retaining cumulative journey distance for telemetry and Dispatch events. Applying a revision resets only route-local progress and the route geometry; it never resets odometer, SOC, cumulative journey distance, event sequence, acknowledgement, or ownership. Reapplying the same revision is a no-op; conflicting payload reuse is rejected.

Synthetic congestion may cap speed while a replacement is being calculated. A synthetic closure keeps speed at zero until the accepted active route no longer contains the closed segment. Live traffic affects route selection through Dispatch and does not directly write simulator coordinates.

### Dashboard behavior

The dashboard derives the effective route from the latest revision, falling back to the initial command. The map removes the old active polyline and draws the revised road geometry on the next normal poll. Route details show the revision reason and revised ETA. Progress is calculated from the revision's cumulative-distance offset and remaining route distance, so it does not jump backwards when a longer or shorter replacement is installed.

### Failure behavior

- No safe alternative for a closure: retain the active route, report `NO_SAFE_ALTERNATIVE`, and let segment safety keep the EV stopped.
- No meaningful congestion improvement: retain the active route and continue at the traffic-limited speed.
- Stale telemetry, partial traffic, provider timeout, invalid geometry, identity mismatch, destination mismatch, or insufficient reserve: retain the active route and expose a non-mutating reason.
- Persistence failure: do not publish or apply the candidate revision.
- Revision rejected locally by the simulator: keep the last valid active route and surface the validation error; do not partially switch geometry.

## Testing and Verification

Implementation will follow test-first coverage for:

- closure-triggered automatic revision and cooldown override;
- congestion thresholds at 10 percent and 60 seconds, including no-switch cases;
- duplicate checks and monotonic revision idempotency;
- fresh telemetry, 5-metre origin, unchanged destination, SOC reserve, and parking-hold preservation;
- no-alternative, provider failure, stale/partial feed, and persistence-failure behavior;
- movement continuity across a revision, cumulative distance/SOC preservation, and no teleportation;
- dashboard effective-route rendering, stable progress, and reroute reason/ETA display.

After targeted red/green tests, run `./scripts/test-local.ps1`. Because traffic routing, telemetry, persistence, and deployment behavior are affected, also run `./scripts/test-local.ps1 -Integration`. If Docker Desktop or `act` is unavailable, report the integration suite as **NOT VERIFIED**, never passed.

## Builder Decisions Left Open

- Choose names and internal helper boundaries that fit the existing modules.
- Choose a small bounded revision-history limit sufficient for diagnostics without growing snapshots indefinitely.
- Reuse the existing reconciliation cadence where practical; the exact bounded interval may be selected to avoid provider overload while keeping rerouting responsive.
