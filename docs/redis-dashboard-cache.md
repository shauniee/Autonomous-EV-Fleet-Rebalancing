# Optional Redis display cache

## Purpose and isolation

Redis is **not** a database, an allocation ledger, a message bus, or the source of
truth for Fleet, Demand or Facilities. Only three read-only dashboard endpoints
use Redis cache-aside with bounded expiries; all ranking, approvals, rental
changes, vehicle and parking claims, and dispatch reconciliation use original
authoritative APIs.

| Owner | Dashboard-only path | TTL | Cache key version |
|---|---|---|---|
| Fleet | `/api/v1/dashboard/vehicles` | 1 s | Fleet instance + mutation version |
| Demand | `/api/v1/dashboard/heat-map` | 1 s | Demand instance + mutation version |
| Facilities | `/api/v1/dashboard/facilities` | 2 s | Facilities instance + mutation version |

The original `/api/v1/vehicles`, `/api/v1/heat-map` and
`/api/v1/facilities` remain uncached. Redis keys are scoped by per-process
instance UUID so a Pod restart cannot serve a different process's old cache.

Requests return `X-Dashboard-Cache: HIT`, `MISS`, or `BYPASS`. A cache
outage activates a short circuit breaker (5 s) and the owner recomputes a
fresh snapshot without Redis. Neither application readiness nor dispatch
authorisation depends on Redis. The cache uses 150 ms per-operation timeouts.
For the tiny current simulator, Redis may not improve latency; use measurements.

## Local Compose

```powershell
docker compose --profile relocation up --build -d --wait `
  redis fleet demand rebalancing dispatch facilities ev-moving-01 ev-moving-02 dashboard
```

See the dashboard at <http://127.0.0.1:8080>. To inspect cache status:

```powershell
curl.exe -i http://127.0.0.1:8080/api/facilities/dashboard/facilities
curl.exe -i http://127.0.0.1:8080/api/facilities/dashboard/facilities
```

The second request should usually be a HIT if it arrives before the 2-second
TTL, with no mock parking mutations between requests.

Test failure fallback without stopping application state:

```powershell
docker compose --profile relocation stop redis
curl.exe -i http://127.0.0.1:8080/api/facilities/dashboard/facilities
docker compose --profile relocation start redis
```

During a Redis outage, requests should remain HTTP 200 and report BYPASS.
To disable caching even when Redis exists, set
`DASHBOARD_CACHE_ENABLED=false` on the relevant service container.

For native Python, leave `REDIS_URL` unset to bypass or set an accessible
`redis://127.0.0.1:6379/0` URL. No Redis port is published to the host by
Compose. `k8s/redis.yaml` provides an ephemeral internal Deployment/Service.

## Evaluation

Compare cached and uncached dashboard read latency using paired runs with
identical EV counts, polling rates, traffic and load, including warm/hit ratios.
Track Redis requests, backend Fleet fetches (for Demand heat-map), p50/p95
latency, fallback correctness and network overhead. Cache hits avoid heat-map
recomputation and some cross-service reads; cold misses can be slower.
No throughput, improvement percentage or production readiness claim is made.

## Limitations and next steps

- Dashboard data can lag by up to the respective TTL; this is display-only,
  and the demand heat-map additionally reads changing Fleet state not part
  of Demand's mutation version. The 1 s TTL bounds this difference.
- This is not reliable cache invalidation across replicas, and does not add
  shared write state. Fleet, Demand, Dispatch, Facilities and simulators
  remain single-replica due to their process-local authoritative state.
- Redis is intentionally ephemeral with no persistence and not publicly
  exposed. Development configuration does not use Redis ACL/auth or TLS.
  Production deployment would require hardened networking and credentials.
- Never use cached snapshots to decide eligibility, bypass optimistic version
  checks, approve an allocation, reserve a parking slot or claim a vehicle.

### Repeatable timing smoke

A paired, interleaved, **development-only** latency script is provided:

```powershell
python scripts/benchmark_dashboard_cache.py --base-url http://127.0.0.1:8080 --requests 100
```

It reports p50/p95 HTTP duration for each authoritative path versus its
cached dashboard counterpart, with observed HIT/MISS/BYPASS distribution.
The CI Compose smoke job prints a smaller 30-pair sample while Redis runs.
Results are environment-dependent and not research conclusions. Measure
multiple trials and steady state before claiming improvements.
