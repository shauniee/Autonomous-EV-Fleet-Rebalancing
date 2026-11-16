# Simulation console and operator dashboard

The project has two browser apps with different jobs:

| | Operator dashboard (port 8080) | Simulation console (port 8090) |
|---|---|---|
| Plays the role of | The fleet operator | The outside world and the experimenter |
| Does | Watches the fleet, requests recommendations, approves relocations, handles charging | Sets demand, starts customer rentals, injects traffic events, runs load experiments |
| Built with | React + Leaflet, served by nginx | React frontend served by its own FastAPI backend |
| Talks to | The five services through nginx `/api/<service>/...` | Its own backend at `/api/probe/v1/...`, which calls the services |

The two apps never talk to each other directly. They share state through the
backend services: the console changes the world, the services react, and the
dashboard shows the result on its next poll.

```
 Simulation console ──► console backend ──► Demand       (zone targets, generator)
                                       ├──► Rebalancing  (traffic scenarios)
                                       ├──► Kubernetes   (scale rental workload, read HPA metrics)
                                       └──► Fleet/Demand/Rebalancing/Dispatch (read-only observations)

 Operator dashboard ──► nginx ──► Fleet, Demand, Rebalancing, Dispatch, Facilities
```

The console backend is a separate Python app. It does not import `ev_fleet`
and has its own pinned dependencies. It finds the services through
`FLEET_URL`, `DEMAND_URL`, `REBALANCING_URL` and `DISPATCH_URL`.

## Typical demo flow

1. **Console, Demand:** pause automatic demand generation for stable values, set
   a current target in every zone, then raise one zone (for example CBD to 12
   desired EVs). CRDS needs a fresh target in every zone.
2. **Dashboard, Dispatch:** confirm Fleet, Demand and Facilities data is
   available. The heat map shows the new shortage. Select the zone and request a
   recommendation; for queued requests, select **View request** or open
   **Recommendations**.
3. **Dashboard:** CRDS answers MOVE, HOLD or DEFER. Check **Evidence** for HOLD
   or DEFER. For a MOVE, review the EV, parking destination, arrival battery and
   road route, then click **Review dispatch** and **Approve**.
4. **Dashboard, Relocations and Map:** follow the acknowledgement, reported
   movement, arrival, battery use and claim release. Bulk jobs show queued,
   admitted and arrived counts. If an approval's outcome is unclear, check or
   retry the original request ID instead of approving again.
5. **Console, Traffic or Rentals:** add a road closure on that route, or start
   customer rentals. The dashboard picks up the traffic overlay, the rented
   cars and the change in supply.

## Demand control

The console reads and writes demand through the Demand service:

- `PUT /api/probe/v1/zones/{zone_id}/demand` sets `desired_available_vehicles` (0–10000) for a zone.
- `POST /api/probe/v1/demand-simulation/start` and `/pause` control Demand's built-in generator.

While the generator is paused, manual targets stay valid until changed, but a
generated value left from before the pause still expires after
`DEMAND_MAX_AGE_SECONDS` (300 s). While it runs, a manual edit holds for
`DEMAND_MANUAL_HOLD_SECONDS` (60 s). See [demand and heat map](demand-heatmap.md).

## Customer rentals

### Rental lifecycle in Fleet

Fleet supports simulated free-floating rentals:

- `POST /api/v1/vehicles/{vehicle_id}/rentals` starts a rental with a rental
  UUID, an idempotency request UUID, the latest `expected_version`, an
  `expected_end_at` (at most 30 days ahead) and an `expected_return_zone_id`.
- `POST /api/v1/vehicles/{vehicle_id}/rentals/{rental_id}/return` confirms the
  return.

Rental start and vehicle claims share the Fleet lock, so a rented car can never
be dispatched, even if its simulator still reports it as available. A return is
only accepted with new telemetry since the rental began, from the current
session, with no claim or relocation in progress. That telemetry decides the
return position and SOC. Retries with the same body return the original
receipt; conflicting IDs or stale versions are 409. Rentals are saved with the
vehicle in Fleet's snapshot.

### Customer workload

`ev_fleet.simulations.customer` runs six extra rental-capable EVs
(`customer-ev-01` to `customer-ev-06`). It never takes over the `ev-moving-*`
simulators, which would create two telemetry owners for one car.

- Seeded weights pick an available car and a different destination zone.
  Mornings favour residential pickups going to the CBD; evenings reverse that.
  These are synthetic assumptions, not Singapore ridership data.
- Fleet's rental API starts each trip. The owning process alone moves the car
  and drains its battery, using the same motion model and time scale as
  relocation. Customer travel is not a Dispatch relocation.
- At rental start the car frees its car park bay through Facilities. On arrival
  it sends fresh telemetry, confirms the return with Fleet, and only then
  reports itself available again.
- Between rentals the cars keep polling Dispatch, so they can be relocated too.

Kubernetes has two forms. `customer-rental-demo` is a Deployment installed at
**zero replicas** that keeps its cars online after the trips. `customer-job.yaml`
is a one-shot Job. They use the same vehicle IDs, so they must never run
together.

The console's **Start** and **Stop** buttons scale the Deployment between 0 and
1 using a ServiceAccount that may only scale that Deployment and read the Job.
Start refuses while the Job is active. Stop refuses with 409 while any customer
car has an active rental or relocation, because stopping the only simulator for
a rented car would leave nobody to record its return.

### Return forecasts

Each rental records where and when the customer is expected to return. The
console shows active rentals and an advisory 30-minute supply comparison, and
the dashboard shows possible inbound returns beside the projected shortage.
CRDS builds its own futures from these rentals (assuming at least 30% SOC or the
configured reserve on return) and also tests cases where returns are late or
never happen. A forecast never makes a rented car eligible to move.

## Traffic scenarios

The console's **Traffic Simulation Controller** injects synthetic road events
onto an approved route:

1. The operator picks an approved Dispatch route, a segment index on its
   geometry, a kind (`CONGESTION`, `ROADWORKS` or `ROAD_CLOSED`), an optional
   speed cap (3–80 km/h) and a duration (1–120 minutes).
2. The console backend forwards it to Rebalancing with the server-side
   `TRAFFIC_SIMULATION_CONTROL_KEY` (at least 16 characters). The browser never
   sees the key.
3. Rebalancing fetches that dispatch itself and checks the geometry, segment
   bounds, Singapore coordinates, a 3 km maximum segment length and the
   scenario version. Hand-drawn lines are rejected.
4. `GET /api/v1/traffic/conditions` returns the synthetic events and, when LTA
   traffic is enabled, live observations as a separate layer. The dashboard
   polls it every five seconds and labels the layers `SIMULATED` and
   `LTA observed`.
5. The same events feed recommendations and the approval-time route check. A
   closure can make a route ineligible, and roadworks add delay and risk.
6. Simulators poll the small `/api/v1/traffic/simulation-events` feed every two
   seconds. On an affected segment they apply the speed cap, and on a closure
   they stop (0 km/h) until the event is cleared or expires. They do not
   reroute a journey already under way.

Scenarios expire on their own, can be removed or cleared, and live only in the
Rebalancing process memory, so a restart clears them. Traffic simulation is off
by default (`TRAFFIC_SIMULATION_ENABLED=false`). See
[traffic-aware routing](traffic-aware-routing.md) for the live LTA side.

## Performance analytics

The console can generate HTTP load against the dashboard and record how the
dashboard's HorizontalPodAutoscaler reacts. It measures:

- CPU against the HPA target, current and desired replicas, and per-Pod CPU and memory from the Metrics API,
- request attempts, successes, failures, throughput and p95 latency,
- replica changes between five-second samples, and comparisons across saved runs.

| Preset | EV-equivalent count | Update frequency | Max GET rate |
|---|---:|---:|---:|
| Normal | 10 | 0.2 Hz | 2/s |
| High | 50 | 1 Hz | 50/s |
| Extreme | 100 | 2 Hz | 200/s |

The load is GET requests to the fixed internal `http://dashboard/` Service only.
It never calls Fleet, Demand, Rebalancing or Dispatch, and adds, moves or
charges no vehicles, so the presets are workload equivalents, not real EV
counts. Runs last 30–120 seconds, only one runs at a time, and concurrency is
capped at 12 requests.

`ANALYTICS_LOAD_ENABLED` turns on the load generator and `ANALYTICS_K8S_READ`
turns on read-only cluster metrics; both are on in the local Kubernetes
manifest. Samples and CSV exports are stored under `ANALYTICS_DATA_DIR` on the
256 MiB `simulation-analytics-data` volume. A run cut short by a Pod restart is
listed as INTERRUPTED.

When reading the results:

- HPA percentages are relative to the dashboard container's CPU request, not the host.
- More requests do not guarantee scale-out: nginx serves static files cheaply and HPA samples periodically.
- Docker Desktop is a single node, so extra replicas add no physical capacity.
- The load generator shares the host with everything else.

## Security

The console is a local, unauthenticated admin tool. It holds the traffic
control key and Kubernetes permissions, so it must only be reached through a
localhost port-forward and never exposed publicly.

## Why a recommendation may not appear

| What you see | Meaning |
|---|---|
| `ROUTING_NOT_CONFIGURED` | `ROUTING_PROVIDER=SIMULATED`; configure OneMap. |
| `ROUTING_ACCESS_UNAVAILABLE` | OneMap token missing, expired or rejected. |
| `DEMAND_UNKNOWN_OR_STALE` | A zone has no current demand target. Set every zone or start the generator. |
| `STALE_TELEMETRY` / Fleet unavailable | Simulator telemetry is missing or old. |
| `NO_SUITABLE_DESTINATION`, `PARKING_FULL`, `CHARGING_FULL` | No compatible parking or charging capacity at the target. |
| `ROUTE_BELOW_OPERATIONAL_RESERVE` | The road journey would use too much battery. |
| `NO_USEFUL_MOVE` | CRDS finds the move does not reduce fleet-wide shortage enough. |
| `DEFER` | CRDS cannot confirm the decision because an input is missing. |
