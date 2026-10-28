# Implementation roadmap

## Map icons and overlap — 28 October 2026

Distinct SVG EV, parking and charging icons use the existing map palette. One
shared Leaflet marker cluster groups nearby items across all three types; click
to zoom or spread co-located markers. Selected-EV groups are highlighted. Existing
filters and route selection remain intact. Marker instances survive polling;
only reported position changes move them, preserving expanded stationary pins.
Added `leaflet.markercluster` and its TypeScript types. TypeScript, production
build (env-file loading disabled) and whitespace checks passed. No test cases or
`.env` access; live deployment verification remains pending.

Area filtering now covers all map icons and counts: EVs use reported coordinates
and the nearest operating-zone centre (matching LTA facility assignment), while
facilities use their assigned zone. Only the selected demand marker is shown;
routes target that zone and outside endpoint icons are hidden. Route lines retain
cross-zone context. All zones is an explicit override, reset by selecting another
zone. Follow/fit controls respect icon scope; relocation links select their target
zone. Display-only changes, with no backend or dispatch-policy changes.

## Optional OneMap routing scaffold — 28 October 2026

Implemented opt-in approval-time routes, road-distance reserve checks, polyline
movement and map directions. Backend-only configuration uses `.env.example`;
default remains SIMULATED. See [setup and limits](onemap-routing.md).
Live verification awaits a token. Build/static checks passed; tests were not run.
Scope review removed unused metadata and redundant calculations/documentation.

UI follow-up: selected journey, highlighted road/EV/endpoints, Fit route and
telemetry-based Follow EV controls, shared progress/directions, and relocation
card links. Reuses the existing road-following simulator and polling APIs; no new
backend, dependencies or navigation engine. TypeScript/build and whitespace checks
passed; tests were not run and `.env` was not accessed. Live verification pending.

Facility-selection clarification: existing LTA matching already chooses charging
below 35% predicted arrival SOC and parking otherwise, with reserve checks.
Updated request/review labels to expose this automatic behaviour and the assumed
ground-team cable connection. No backend policy or charging-state changes.

## LTA DataMall integration — 28 October 2026

Frontend follow-up: facility cards use client-side pagination (12 per page),
Previous/Next and direct page selection in the existing dashboard style. Pages
reset on zone changes, survive refreshes and clamp when results shrink. Map and
recommendation inputs retain the full dataset. No tests rerun or `.env` accessed.
Production build/TypeScript and whitespace checks passed.
UI refinement: numbered pages with ellipses and an active state, a styled page
jump, and labeled relocation dropdowns with helper text and mobile stacking.
Selection and dispatch behavior remain unchanged; no tests rerun or `.env` access.
Map follow-up: accessible layer toggles/counts for zones, EVs, parking and charging;
reset-view control, lighter markers and Canvas rendering. Separate layer updates
avoid redrawing facilities on each telemetry refresh. Display filters do not
change planning or allocations. No tests rerun or `.env` access.
Map facilities now default to the selected operating zone, with an All zones
override. Parking/charging counts follow this filter; EVs and zones stay visible.

Implemented real LTA destinations, charging requests and vehicle selection,
allocated parking telemetry and simulated charging to 80%. macOS startup supports
native images, tunnel reuse and connection-only mode. See [LTA setup](lta-datamall.md).
Live parsing, connection-only startup, build and syntax checks passed; the updated
charging cycle awaits user-run deployment and verification. Tests were not rerun.
The roadmap below is historical.

1. **Backend foundation (implemented):** contracts, Fleet, Demand, Rebalancing, stationary
   simulators, health, unit/API/real HTTP tests and Compose definition. Exit evidence:
   nodes register independently, fresh telemetry ranks correctly, stale nodes are
   excluded, a farther energy-suitable EV can win, tests pass, container smoke test
   succeeds or is explicitly pending on environment availability.
2. **Approved relocation (implemented):** Dispatch service, atomic Fleet claims, idempotent commands,
   polling/acknowledgement, simulated movement and energy consumption, arrival,
   disconnection and ambiguous-delivery reconciliation. No real-road driving.
   See [Phase 2 guide](phase2-dispatch.md) and [verification](phase2-verification.md).
3. **Phase 3A dynamic demand and heat-map backend (current):** seeded optional demand
   simulation, manual holds, Fleet API composition, current/projected shortages,
   semantic severity and partial-data handling. See [Phase 3A guide](phase3a-demand-heatmap.md).
   **Phase 3B dashboard (later):** Singapore demand heat map, markers, selection,
   recommendation explanations, explicit approval and relocation progress.
4. **Durability and deployment:** persistent state, durable command recovery,
   dispatcher/device authentication, then Kubernetes. No multiple in-memory replicas.
5. **Phase 4 experimental evaluation:** nearest eligible baseline, reproducible paired
   scenarios, isolated trial state, raw JSON/CSV, confidence intervals, graphs and analysis
   of distance, SOC, arrivals, demand coverage and failure handling. Product policy weights
   remain fixed and the application default remains energy-aware.

Six-person allocation: Fleet/concurrency; Demand/charging; selection/metrics;
simulator/telemetry; HTTP integration/failures and later Dispatch; deployment/logging/
documentation. Agree contracts jointly and pair-review service boundaries.

Phase 1 deliberately excludes frontend, animation, dispatch execution, movement,
ML prediction, maintenance prediction, reservation handling, real charger APIs,
database, Kubernetes manifests and experiment framework.

Major risks: ephemeral state, clock skew, simplified geography/energy, correlated
score factors, stale charger assumptions, simultaneous recommendations choosing the
same EV, unbounded long-running fleet/session identity history, and later cross-service
command ambiguity. Keep demonstration populations finite; solve durable ownership
before claiming production recovery or horizontal scaling.
