# API contracts

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

Recommendation fields: recommendation_id, created_at, expires_at, policy_version,
policy_settings, target, fleet_snapshot, selected_vehicle_id, ranked_candidates,
excluded_candidates, decision_latency_ms, explanation and assumptions.

Candidate fields include vehicle_id, state_version, eligible and exclusion_reasons.
When telemetry exists, distance_km, energy_required_kwh, current_soc_pct and
predicted_arrival_soc_pct are populated; otherwise they are null. Eligible candidates also include utilities,
weighted_components, score and charging_assessment. No eligible EV is a successful
201 with selected_vehicle_id=null, not an upstream error. Repeated POST creates a
fresh advisory recommendation; it has no dispatch side effect.

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

## Implemented Phase 2 endpoints

POST /dispatches approves a recommendation; GET /dispatches/{id} reports outcome.
EVs poll GET /vehicles/{id}/commands and POST acknowledgements/events under the
dispatch ID. These operations are now implemented by the separate Dispatch Service
and Fleet claim API. See [Phase 2 contracts and lifecycle](phase2-dispatch.md).
