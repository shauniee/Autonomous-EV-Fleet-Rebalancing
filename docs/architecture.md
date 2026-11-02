# initial service foundation architecture

The current demand and heat-map extension is documented in [dynamic demand and heat-map composition](demand-heatmap.md).
It adds an optional simulator and a read-only API composition module inside Demand,
using a Fleet HTTP snapshot without storing a second source of vehicle truth.

This document records the preserved initial service foundation foundation. The implemented dispatch and relocation
extension, including Dispatch Service, cloud-owned claims and moving simulators,
is described in [dispatch and relocation dispatch](dispatch-relocation.md). References below to future
dispatch describe the original design boundary; that boundary is now implemented.

This project is independently implemented for Singapore point-to-point EV rental
rebalancing. All demand, charging and autonomous vehicle behaviour is simulated.

## Ownership and boundaries

Fleet owns registrations, telemetry sessions, current vehicle observations and
versions. Demand owns zones and versioned demand/charging observations.
Rebalancing retrieves HTTP snapshots, calculates eligibility and scores, and stores
recommendations for a short lifetime. Stationary EV nodes own their current
simulated physical state and independently submit telemetry. No service imports
another service's store to exchange runtime state.

The shared package contains validated contracts, clock, settings and request logging.
Routes are kept with app factories while services are small; store modules handle
state operations and pure policy/metrics modules handle scoring. Separate empty
routes/service layers would add little value at this stage.

## Singapore model

Six zones are seeded: Jurong East, Woodlands, Bishan, Punggol, Tampines and CBD.
Positions use a Singapore simulation envelope of latitude 1.15..1.50 and longitude
103.60..104.10; this rectangle is not a national boundary or road accessibility check.
Zone points are approximate. Demand and charger slots must be supplied manually;
neither is asserted to be current Singapore operational data.

Store aware UTC timestamps; future UI/scenarios use Asia/Singapore. Use km, kWh
and SOC percentages. Haversine distance times a route factor is an estimate;
no real road routing, traffic, charging queues or road autonomy is modelled.

## Eligibility and scoring

Exclude missing telemetry, offline/stale EVs, unavailable EVs, non-IDLE relocation
states, active dispatch IDs, and predicted arrival SOC below the configured reserve.
Reserve equality is eligible. Registration without telemetry is never eligible.

    distance = haversine_distance * route_factor
    energy = distance * consumption_kwh_per_km * energy_margin_factor
    arrival_soc = current_soc - 100 * energy / battery_capacity_kwh

For eligible EVs:

    D = 1 - min(distance / distance_scale_km, 1)
    B = current_soc / 100
    R = (arrival_soc - min_reserve_pct) / (100 - min_reserve_pct)
    C = arrival_soc / 100 + charger_available * (1 - arrival_soc / 100)
    score = 0.35*D + 0.20*B + 0.35*R + 0.10*C

`charger_available` is 1 only for a fresh manually supplied observation with at least
one free slot. Unknown/stale data uses 0 and remains explicitly labelled.
Charging cannot override the reserve exclusion. Current and predicted SOC are
correlated; weights are initial versioned policy constants, not validated findings.
Fixed normalization avoids candidate-set-dependent scores. Tie breaks: descending
arrival SOC, ascending distance, ascending vehicle ID. Raw scores are not rounded
before sorting. No nearest-EV baseline or experiment runner is implemented yet.

Recommendations include score components, reasons, source versions/instance IDs,
policy settings, target observations, timestamps and measured request latency.
They are advisory and expire after 10 seconds by default. In-memory retention is
pruned on reads/writes and capped at 10,000 live recommendations.

## Concurrency and telemetry

Each state owner uses an asyncio lock around short operations. Snapshots are copied
under the lock. No lock is held across network I/O. Use one worker and one replica
per in-memory state owner. Multiple workers would have independent, conflicting state.

Registration is idempotent for identical metadata. Telemetry-session creation uses
a caller-generated request UUID; retrying it returns the same session. A new request
supersedes the old session. Retried retired session requests are rejected. Only one
active node may use a vehicle ID; authentication and bounded durable session history
are later work. IDs and records are finite-demo data, not production-scale storage.

Sequence numbers order telemetry within a session. Identical last-observation retries
are no-ops and never refresh freshness. Older sequences and conflicting duplicates
return 409. Older observation timestamps cannot supersede a newer observation.
New-session telemetry is required after session replacement; old physical status is
retained but marked stale. This prevents registration/session resets from making a
previously relocating vehicle automatically available.

Reject observations already 15 seconds old or over 5 seconds into the future by
default. Accepted freshness combines initial observation age and elapsed server
monotonic time. Stale starts at 15 seconds and offline at 30. Connectivity can stay
ONLINE while telemetry is stale. Rebalancing rechecks observation time after transport.
Deployment clocks should be synchronized; this is not a distributed clock guarantee.

## Failures and future dispatch

HTTP reads use a 2-second HTTPX timeout per I/O phase and at most one retry with
backoff/jitter for transport failures and 5xx responses. This is bounded retry count,
not an end-to-end hard request deadline. Invalid upstream JSON/schema fails explicitly.
Fleet/Demand are read concurrently; there is no transaction across their snapshots.

Simulator registration/session retries reuse identity; after telemetry transport
ambiguity it sends a higher sequence with a fresh observation. It backs off during
disconnection. After fleet restart, 404 triggers re-registration. Conflicting identity
errors stop the node instead of repeatedly stealing sessions.

Liveness means the HTTP process responds. Readiness means local initialization has
completed; dependency availability is not folded into readiness, to avoid cascading
withdrawal. A ready rebalancing process can still return 503/504 when a dependency
fails. Instance IDs identify a new in-memory epoch. All state is lost on restart.

dispatch and relocation adds approval and commands. Approval revalidates target and vehicle state,
then Fleet atomically claims the EV using its expected version. Competing approvals
cannot both claim it. Cloud-owned claims remain separate from reported availability.
Stable dispatch/command IDs support idempotency and reconciliation across service
failures. Never hold a Fleet lock while contacting Dispatch or a vehicle.

Vehicle lifecycle: IDLE -> DISPATCHED -> RELOCATING -> ARRIVED -> IDLE. Availability
returns only after validated arrival and claim release. Connectivity is independent.
Dispatch lifecycle: PENDING -> SENT -> ACKNOWLEDGED -> IN_PROGRESS -> SUCCEEDED,
with REJECTED, FAILED and UNKNOWN outcomes. An acknowledgement timeout is ambiguous;
retain the claim and reconcile rather than releasing the EV for another command.
Exactly-once delivery and durable crash recovery are not claimed by this prototype.

## Deployment and later measurement

Compose runs independent services and nodes. Kubernetes manifests are deferred.
Later introduce durable shared state and transactional claims before scaling state
owners; then use Deployments, Services, resource limits and health probes. Dispatcher
and device authentication are required before external exposure.

Later compare EV-aware selection with nearest eligible EV using identical exclusions
and matched scenario inputs. Capture actual distance, command/arrival outcomes,
predicted/observed arrival SOC, below-reserve arrivals, demand coverage, decision
latency and recovery outcomes. Demand coverage needs an explicit time-window and
vehicle-to-zone rule before comparison. Current metadata supports instrumentation
but is neither a durable audit journal nor an experiment framework.
