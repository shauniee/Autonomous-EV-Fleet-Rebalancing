# November 2026: correctness, performance and reliability audit fixes

Branch: `fix/audit-routing-performance-reliability`. **Review and test before merging.**
This is a local Singapore EV simulation, not a vehicle control system.

## Routing correctness

- OneMap remains the required **primary** road-route provider. Optional
  `OSRM_ROUTING_URL` now provides a configured server-side alternative when
  traffic blocks or delays OneMap's path. Dispatch no longer overrides the
  configured URL with an empty string.
- Moving EVs accept explicit `ONEMAP` and `OSRM` road geometries, but still
  reject missing/unsupported route sources and origin points more than 5 m
  from their road starting vertex. OSRM must be properly hosted/configured
  and road-matched; do not invent straight-line connectors.
- Rebalancing's **bounded internal** `POST /api/v1/traffic/assess-route` takes
  `geometry`, `distance_km`, `duration_seconds` and scores the whole
  observed feed. Dispatch uses it for primary/alternative selection and
  journey checks. A failed assessment yields an explicit error rather than
  silently downgrading to an optimistically clear dashboard sample.
- `GET /api/v1/traffic/conditions` remains a small display endpoint with
  at most 800 sampled Speed Bands, **never** an authoritative route scorer.
- A simulated traffic event does **not** suppress fresh live LTA roadblock
  observations on the same route. Live and simulated sources remain distinctly
  labelled. Unknown/stale incidents are not falsely declared cleared.

## Performance

- A 0.01-degree spatial grid indexes **Speed Bands endpoints**, matching the
  previous exact rule: speed links count when either endpoint lies within
  100 m of the assessed road corridor. Route candidate tile search is only a
  prefilter, followed by the unchanged geometric predicate.
- The speed index and small map projection are built **once per changed Redis
  content hash**, in a background thread. Requests to Rebalancing no longer
  re-sample the full ~144k links just to draw the dashboard.
- Fleet's tagged snapshot JSON encoding for telemetry writes is moved off
  the ASGI event loop while the Fleet state lock is held. This improves API
  responsiveness during serialisation; it **does not** remove one PostgreSQL
  snapshot write per telemetry sample. A future normalized/schema-specific
  persistence design needs its own migration and benchmark.
- Facility feed refreshes and capacity holds were **not** refactored here;
  no measurable performance problem was established for them.

## Persistence and failures

- If a PostgreSQL snapshot write fails or is cancelled after a potentially
  ambiguous commit, the repository is marked `faulted`; readiness becomes
  false, normal HTTP endpoints return `503 PERSISTENCE_STATE_UNCERTAIN`,
  and liveness stays observable. New operations must **not** continue using
  potentially uncommitted in-memory claims.
- Recovery requires restarting the affected state-owning service **after
  investigating the database**, then reloading the durable snapshot and
  reconciling active claims/holds. Do not delete/replace PostgreSQL PVCs.
- This fails closed but is not a two-phase commit or a cross-service
  transaction. The durable snapshot is still a single JSONB state document;
  cross-service partial failures require idempotent reconciliation.
- Dashboard distinguishes `REJECTED`, `FAILED`, `UNKNOWN`, and
  unclaimed `PENDING` from a confirmed claim. HTTP 202 alone is not evidence
  of successful vehicle dispatch.

## Redis operations and observability

- Redis now uses a bounded 128 MiB `maxmemory` with `noeviction` (Kubernetes
  container memory limit 256 MiB) so dashboard caching cannot silently evict
  a live traffic snapshot or its metadata pointer.
- Traffic publication commits the compressed payload before updating its
  per-feed metadata pointer. Redis MULTI does not roll back failed execution
  commands; an OOM payload failure must not point metadata at a missing blob.
- `GET /api/v1/traffic/cache-status` reports source, overall status,
  feed-level status, timestamps, counts and error codes **without** transferring
  full datasets. Process liveness alone cannot prove successful LTA polling.
- Redis remains ephemeral; cache writes can fail under pressure. Existing
  reader behaviour withholds stale observations instead of treating unknown
  roads as clear.

## Local verification

Because dashboard Vitest worker initialization was timing out on Windows
before tests could run, **do not count dashboard tests as passed or skipped
silently**. A production build is a useful supplementary check, not a
substitute for execution of the test suite.

```powershell
# From repo root after switching to this branch:
python -m pytest tests/unit/test_route_traffic_api.py tests/unit/test_traffic_routing.py tests/unit/test_traffic_scenarios.py tests/unit/test_shared_traffic_cache.py tests/unit/test_routing_contract_regression.py tests/unit/test_persistence_fail_closed.py -q -x
python -m pytest -q
npm.cmd --prefix dashboard run build
npm.cmd --prefix dashboard test -- --run
npm.cmd --prefix simulation-console/frontend run build
docker compose config
kubectl kustomize .\k8s
```

For local Kubernetes **with no active relocations/rentals/charging**:
- Inspect Rebalancing/Fleet/Dispatch readiness, restarts, liveness and logs.
- Check `/api/v1/traffic/cache-status` with real LTA enabled; confirm each
  feed's `observed_at` advances only on successful retrieval.
- Check route-specific assessments against observed road closures and
  synthetic overlays. Verify OneMap-only primary and optional OSRM alternates.
- Benchmark one vs 20 EVs and ~144k speed records with repeated trials,
  reporting p50/p95 response latency, CPU, memory, feed transfer size,
  PostgreSQL write throughput, Redis used memory, errors and restarts.
- Inject an isolated PostgreSQL write failure only in a disposable test
  environment. Verify readiness changes and recovery from durable snapshot.
- Validate a dashboard approval rejected with HTTP 202 displays **REJECTED**,
  not approved, and `UNKNOWN` retains the original idempotency key.
- Do not merge or deploy based solely on passing Python tests. Vitest remains
  a separate outstanding environment blocker until rerun successfully.

## Unresolved / intentionally deferred

- We did not execute live Windows, Docker Desktop, OneMap, OSRM, Redis or
  Kubernetes experiments through this GitHub-editing session.
- Genuine external OSRM alternatives require a configured, permitted and
  road-loaded routing service; an absent service must never trigger a
  straight-line fallback.
- Large telemetry persistence is still based on a whole-service JSON snapshot
  per mutation. A proper per-vehicle relational/append-only redesign needs
  its own schema migration, consistency analysis and load measurements.
- The simulation console has privileged local-only controls; never publicly
  expose it without authentication and network restrictions.
