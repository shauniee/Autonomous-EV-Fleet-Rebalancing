# Dynamic demand and heat-map backend

The existing Demand Service now composes a read-only view of demand and Fleet
capacity for the six Singapore simulation zones. There is no frontend, map API,
ML, persistence, new service or dispatch lifecycle change.

## Ownership

Demand owns zone geometry, demand observations, demand versions and simulator
metadata. Fleet remains the only source of vehicle state and claims. Each heat-map
request copies one local demand snapshot, releases the store lock, and fetches one
Fleet snapshot over HTTP. Fleet data is not cached or independently stored in Demand.
The result carries both source identities/timestamps and the Fleet snapshot version.
It is an API composition, not a transaction across the services.

The Demand Service remains ready and accepts manual updates when Fleet is down.
Only heat-map capacity calculations depend on Fleet. Legacy `/zones` and `/hotspots`
remain independent of that dependency.

## Demand and simulator

`rental_demand` means the current desired number of available vehicles in a zone.
It maps to the existing stored field `desired_available_vehicles`. It is a capacity
requirement, not a bookings-per-minute rate, completed rentals or a prediction.

Simulation is disabled by default. When enabled, startup performs one tick to
initialize missing demand, then the background task waits the configured interval
between ticks. Initial values are uniform integers within the configured range.
Subsequent values use a bounded seeded random walk, with a delta from -max_step to
+max_step inclusive. A tick can leave a value unchanged, especially at the bounds.

An isolated `random.Random` instance and sorted zone IDs make the sequence repeatable
for the same seed, initial state and ordered manual/tick inputs. Real concurrent
timing is not a reproducibility guarantee. Time-of-day demand prediction is not added.
Timestamps remain UTC; the scenario is Singapore, with Asia/Singapore for future display.

All zone updates in a tick run under the Demand store lock. Manual writes use the
existing expected-version check. Whichever operation wins the lock commits first;
a stale manual version returns 409 rather than overwriting a simulated update.
A successful manual write sets a per-zone hold, default 60 seconds, measured with
the server monotonic clock. Ticks skip that zone until the hold expires. Other zones
continue changing. Simulator bounds apply only to generated values; valid manual
values outside those bounds are preserved during the hold, then return to the
configured range on the next eligible tick. Simulator writes never replace a newer
future-dated observation after a clock correction.

Configuration is read on startup. There is no unauthenticated runtime control API;
GET `/demand-simulation` reports settings and state. Disabled simulation leaves manual
values untouched. A service restart still loses all in-memory state, as in dispatch and relocation.

## Capacity and zone assignment

Count an EV as `available_evs` only when Fleet's effective availability is AVAILABLE,
its telemetry is fresh/online, it is IDLE without a claim or active dispatch, and
SOC is at least MIN_RESERVE_PCT. This is known usable local capacity, not a claim
that the EV could reach every possible destination. Excluded low-SOC vehicles are
reported separately; Fleet's own availability and dispatch rules are unchanged.

For a stationary vehicle, prefer a valid `current_zone_id` whose centre is within the
configured radius of its current coordinates. Otherwise use the nearest zone centre
within the radius, breaking ties lexicographically by zone ID. Default radius is 2 km.
Initial simulator nodes therefore count in their seeded zones even before their first
confirmed arrival. No zone ID is written back to Fleet by aggregation.

These circles are simulation catchments, not official boundaries. Vehicles outside
all circles, or without telemetry, appear in `unassigned_evs`. An assigned-zone ID is
only a classification for this response; it does not modify routing or ownership.

Each claimed EV is classified against its claim's destination, never as available
at the origin:

| Count | Evidence | Counts in projected supply? |
|---|---|---|
| inbound_relocating_evs | Active claim for same EV/Fleet instance and fresh RELOCATING telemetry with matching dispatch ID | Yes |
| inbound_pending_evs | Valid claim, fresh IDLE/DISPATCHED telemetry, no conflicting dispatch identity | No |
| arrived_pending_evs | Fresh matching ARRIVED telemetry while claim remains held | No |
| inbound_unconfirmed_evs | Stale/offline, failed or mismatched claim/telemetry | No |

On arrival the existing claim-release and fresh IDLE telemetry flow changes the EV
to current available supply. The short ARRIVED-but-claimed interval remains visible
without promising that EV as usable. Fleet telemetry is the evidence for inbound
motion; the heat-map API does not claim to expose authoritative Dispatch outcomes
or ETAs. Projection is conditional on observed journeys completing successfully.

Diagnostics can overlap: a stale inbound EV can be counted as excluded stale at its
last observed location and unconfirmed at its target. Do not sum diagnostic fields
as supply. A vehicle contributes to at most one actual supply category.

## Shortages and severity

    current_balance  = rental_demand - available_evs
    current_shortage = max(current_balance, 0)
    current_surplus  = max(-current_balance, 0)

    projected_supply   = available_evs + inbound_relocating_evs
    projected_balance  = rental_demand - projected_supply
    projected_shortage = max(projected_balance, 0)
    projected_surplus  = max(-projected_balance, 0)

Positive signed balances mean a shortage; negative means a surplus.

    shortage_ratio = current_shortage / rental_demand

For zero demand the ratio is zero. Severity is NORMAL when no current shortage,
WARNING for a positive shortage below the critical ratio, and CRITICAL at or above
that ratio (default 0.5). Unknown demand/capacity gives UNKNOWN. Inbound EVs do not
reduce the current hotspot severity. Example: demand 12, available 6, inbound 6 is
currently CRITICAL with shortage 6, even though projected shortage is zero.

`demand_level` is separate: LOW below 5, MEDIUM from 5 through 9, HIGH from 10;
UNKNOWN for missing/stale demand. Neither classification includes colours.

## APIs

Paths are under `/api/v1` on the existing Demand port (8002 by default).

| Endpoint | Response |
|---|---|
| GET /heat-map | Typed HeatMapResponse with all zones |
| GET /heat-map/hotspots | Same envelope, zones filtered to WARNING/CRITICAL; retains global quality/issues |
| GET /demand-simulation | Enabled/running state, seed/configuration, tick count, last tick/error and manual holds |
| PUT /zones/{id}/demand | Existing manual contract, unchanged |
| GET /hotspots?threshold=5 | Existing raw-demand threshold endpoint, unchanged |

HeatMapResponse includes `generated_at`, COMPLETE/PARTIAL status, issues,
`calculation_version`, policy settings, demand instance/snapshot timestamp,
Fleet instance/snapshot version/timestamp, Fleet status and global exclusions.
Every zone includes ID/name/position, rental demand and source/version/timestamp,
demand freshness/level, supply/diagnostic counts, current/projected balances,
shortages/surpluses, shortage ratio, hotspot severity and data quality.

Source/manual-hold metadata is separate from `ZoneState`, so merely reading status
or allowing a hold to expire cannot change a recommendation's target comparison.

## Partial and stale results

A bounded Fleet read (default 2-second whole-request timeout) is attempted once.
On timeout/unavailability/invalid response, return HTTP 200 with PARTIAL status,
known demand and null supply/shortage fields. Never substitute zero vehicles for
an unavailable Fleet. Invalid duplicate vehicle IDs fail snapshot validation.
An old or excessively future-dated Fleet snapshot is unusable in the same way.

Individual stale/offline vehicles are excluded from known capacity and confirmed
inbound supply. Zone quality becomes DEGRADED when stale exclusions or unconfirmed
inbound vehicles affect it. A response may be PARTIAL because a different zone is
missing demand or because some vehicles cannot be assigned. Low-SOC exclusion alone
is an ordinary eligibility decision, not stale data.

Missing or stale demand retains its last value/timestamp for inspection, but its
level/severity is UNKNOWN and balances are null. Fleet counts can still be known.
Snapshot age and individual telemetry age are checked independently. Wire timestamps
allow at most five seconds of future clock skew, matching existing observation policy.

## Configuration and running

Install/start the existing services as in README. To enable simulation in PowerShell
before starting Demand locally, or before `docker compose ... up`:

```powershell
$env:DEMAND_SIMULATION_ENABLED = 'true'
$env:DEMAND_RANDOM_SEED = '42'
$env:DEMAND_UPDATE_INTERVAL_SECONDS = '30'
```

For local use, FLEET_URL defaults to `http://localhost:8001`; Compose supplies
`http://fleet:8000`. No Fleet dependency is added to Demand readiness/startup.

| Variable | Default |
|---|---:|
| DEMAND_SIMULATION_ENABLED | false |
| DEMAND_RANDOM_SEED | 42 |
| DEMAND_UPDATE_INTERVAL_SECONDS | 30 |
| DEMAND_MIN_DEMAND / DEMAND_MAX_DEMAND | 0 / 20 |
| DEMAND_MAX_STEP | 3 |
| DEMAND_MANUAL_HOLD_SECONDS | 60 |
| DEMAND_MAX_AGE_SECONDS | 300 |
| DEMAND_FLEET_SNAPSHOT_MAX_AGE_SECONDS | 5 |
| DEMAND_TELEMETRY_MAX_AGE_SECONDS | 15 |
| DEMAND_ZONE_RADIUS_KM | 2 |
| MIN_RESERVE_PCT | 20 |
| DEMAND_CRITICAL_SHORTAGE_RATIO | 0.5 |
| DEMAND_MEDIUM_DEMAND / DEMAND_HIGH_DEMAND | 5 / 10 |
| DEMAND_FLEET_TIMEOUT_SECONDS | 2 |

All defaults are listed in `.env.example`; local applications do not automatically
load that file. Compose reads `.env` and forwards the configured variables.

```powershell
Invoke-RestMethod http://localhost:8002/api/v1/heat-map | ConvertTo-Json -Depth 12
Invoke-RestMethod http://localhost:8002/api/v1/heat-map/hotspots | ConvertTo-Json -Depth 12
Invoke-RestMethod http://localhost:8002/api/v1/demand-simulation | ConvertTo-Json -Depth 6
```

Manual update (read current version before writing):

```powershell
$zone = Invoke-RestMethod http://localhost:8002/api/v1/zones/cbd
$body = @{ expected_version = $zone.demand_version; observed_at = [DateTime]::UtcNow.ToString('o'); desired_available_vehicles = 7 } | ConvertTo-Json
Invoke-RestMethod -Method Put -Uri http://localhost:8002/api/v1/zones/cbd/demand -ContentType application/json -Body $body
```

## Dispatch integration and limits

Changing a demand or charging observation after recommendation does not move the
destination or invalidate operator review. Approval uses the latest observation,
and active CRDS recommendations receive a fresh benefit assessment before the Fleet
claim is acquired. A changed zone definition or Demand service instance still causes
TARGET_CHANGED and requires a fresh recommendation. Once approved, a journey's
destination/command is unchanged by subsequent demand ticks or manual updates. The
heat map reflects the new demand and observed inbound vehicle. No automatic dispatch,
cancellation or rerouting is introduced.

Tests cover formulas, boundaries, classification, deterministic ticks, concurrency,
dependency failure, approval across observation updates, simulation lifecycle and
real HTTP demand updates during a moving EV journey. See
[verification evidence](demand-heatmap-verification.md) for actual outcomes and runtime limits.

This capability remains in-memory, single-worker per state owner, simulation-only and
without frontend/ML/external map APIs. A later dashboard should consume `/heat-map`,
not reinterpret the legacy raw-demand `/hotspots` as shortage severity.
