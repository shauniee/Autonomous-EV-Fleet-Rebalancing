# API contracts

demand and heat-map adds read-only `/api/v1/heat-map`, `/api/v1/heat-map/hotspots`, and
`/api/v1/demand-simulation` to Demand. See [heat-map contracts and calculations](demand-heatmap.md).
Existing zone, manual-demand and raw-demand hotspot contracts below remain unchanged.

OpenAPI is served at `/openapi.json` and interactive documentation at `/docs` on
each service. All timestamps must include a timezone. UUIDs are strings on the wire.
Extra fields and non-finite numbers are rejected. Domain routes use `/api/v1`.

## Fleet (8001)

| Method and path | Input / result |
|---|---|
| POST /vehicles | vehicle_id, battery_capacity_kwh, consumption_kwh_per_km; 201 VehicleState; identical metadata is idempotent |
| GET /vehicles | FleetSnapshot: instance_id, snapshot_version, snapshot_at, vehicles |
| GET /vehicles/{id} | VehicleState or 404 |
| POST /vehicles/{id}/telemetry-sessions | request_id UUID; returns session_id; same active request is idempotent |
| POST /vehicles/{id}/telemetry | session_id, sequence, observed_at, position, soc_pct, availability, relocation_state, optional active_dispatch_id |
| POST /vehicles/{id}/simulator-sessions | Checkpoint-enabled simulator ownership: ACQUIRE, RENEW, coordinated RESTORE, or guarded RESTORE_IDLE; returns SimulatorSession |

`RESTORE_IDLE` supplies a request UUID, expected Fleet instance/control version,
owner generation/revision, checkpoint revision, and simulation configuration.
It accepts no physical state. Fleet requires an expired owner, stale telemetry,
a matching saved idle/noncharging checkpoint, no active claim/rental/charging task,
and a successful Dispatch read with only succeeded/rejected, released journeys
and no pending recovery. An exact retry returns the original receipt. Conflicting
evidence returns 409; unavailable/invalid Dispatch evidence returns 503.
The new session fences the previous owner and leaves telemetry timestamps
unchanged. Only a new simulator checkpoint can establish freshness.

Telemetry returns accepted, duplicate and vehicle. VehicleState contains registration,
state_version, nested last telemetry, received_at, telemetry_age_seconds, stale and
connectivity. Position contains latitude/longitude. Availability is AVAILABLE or
UNAVAILABLE; relocation_state is IDLE, DISPATCHED, RELOCATING, ARRIVED or FAILED.
The latter states can be reported for exclusion tests; there is no command execution.
Registration requires a path-safe alphanumeric/hyphen/underscore ID, at most 64 chars.
Battery capacity must be >0 and <=300 kWh; consumption >0 and <=2 kWh/km.

## Demand (8002)

| Method and path | Input / result |
|---|---|
| POST /zones | zone_id, name, position; creates ZoneState |
| GET /zones | snapshot_at, instance_id, zones |
| GET /zones/{id} | ZoneState |
| PUT /zones/{id}/demand | expected_version, observed_at, desired_available_vehicles |
| PUT /zones/{id}/charging | expected_version, observed_at, available_slots |
| GET /hotspots?threshold=5 | zones with desired_available_vehicles >= threshold; threshold >=1 |

ZoneState includes instance_id, version, demand_version, charging_version, demand and
charging observations. Missing observations are null. Demand and charging versions
start at 0, and each update increments only its own version plus the zone version.
PUT uses that observation's expected_version. A lost PUT response should be resolved
by GET before retrying; blindly repeating the old version returns 409. Stale demand
is displayed with its timestamp, not silently presented as a prediction.

## Rebalancing (8003)

| Method and path | Input / result |
|---|---|
| POST /recommendations | target_zone_id; 201 recommendation |
| GET /recommendations/{id} | live recommendation; 404 if missing or expired |
| POST /batch-recommendations | target_zone_id, quantity (1–20), optional purpose and vehicle/facility selection; 201 batch preview |
| GET /batch-recommendations/{batch_id} | live batch preview; 404 if missing or expired |
| POST /recommendation-requests | request_id, target_zone_id, quantity and optional vehicle selection; 202 queue receipt |
| GET /recommendation-requests | Queue availability, mode and paginated requests, with optional status filter |
| GET /recommendation-requests/{request_id} | Assessment status and batch preview when available |
| POST /crds/revalidate | Internal Dispatch call with the exact route, recommendation and planning revision |

Recommendation fields: recommendation_id, created_at, expires_at, policy_version,
policy_settings, target, fleet_snapshot, selected_vehicle_id, ranked_candidates,
excluded_candidates, decision_latency_ms, explanation and assumptions.

CRDS single recommendations and batch children use policy version `crds-v1`.
Single and batch previews include a `crds` object with the decision, reason,
approval flag, scenario parameters, source references and evidence. Ranked
candidates and batch assignments include `crds_gain` in place of the legacy weighted
score. Candidate details can include vehicle identity, distance, energy,
predicted arrival SOC and traffic assessment. CRDS publishes per-EV exclusions
in decision evidence. `HOLD` and `DEFER` previews have no selected EV or batch
assignments. An upstream failure can still return an HTTP error.

The synchronous endpoints create a new preview for each POST. The queued
endpoint uses a caller-generated request UUID. Retry the same UUID with the
same payload after an ambiguous response. A single `vehicle_id` requires
quantity one. A queued `vehicle_ids` shortlist requires unique IDs and a
quantity equal to its length. The queue requires configured persistence and
CRDS in `ACTIVE` or `ADVISORY` mode. Recommendation requests do not reserve
resources or dispatch vehicles. See [CRDS decisions and execution](crds.md).

## Errors and health

Errors use `{"error":{"code":"...","request_id":"..."}}`, with validation details
for invalid inputs. The response `X-Request-ID` matches the error request ID.
404 means unknown resource, 409 means identity/version/sequence conflict, 422 means
invalid input or timestamp, 503 means dependency/capacity failure, and 504 means
dependency timeout. Input validation details omit the submitted raw payload.

Each service provides `/health/live` and `/health/ready`. Readiness checks local
initialization, not transitive dependencies. The OpenAPI request contracts are
authoritative for exact field constraints. Fleet, zone detail and recommendation
response schemas are also typed. Inapplicable candidate score fields are null.

## Implemented dispatch and relocation endpoints

POST /dispatches approves a recommendation; GET /dispatches/{id} reports outcome.
POST /batches approves a batch preview, with a stable request UUID. Dispatch
exposes internal GET /dispatch-planning-state and POST /route-estimates for CRDS.
EVs poll GET /vehicles/{id}/commands and POST acknowledgements/events under the
dispatch ID. These operations are now implemented by the separate Dispatch Service
and Fleet claim API. See [dispatch and relocation contracts and lifecycle](dispatch-relocation.md).
