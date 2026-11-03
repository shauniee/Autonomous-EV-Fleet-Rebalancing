# Shared Redis cache for live LTA traffic (local demo)

## Why this change

Previously the Rebalancing API process polled full LTA Traffic Speed Bands,
Traffic Incidents and Approved Road Works itself, while moving EV simulators
also fetched large traffic responses every two seconds. Under the 3 November
2026 Docker Desktop workload, liveness checks timed out and Kubernetes
repeatedly restarted Rebalancing. That observation motivates the design but
**does not prove that LTA polling was the only source of CPU contention**.

This change separates responsibility:

- A **single** `traffic-collector` Deployment fetches/normalises the LTA feeds.
- Collector writes independently timestamped, compressed data into the
  existing Redis instance and publishes metadata atomically after the data.
- The Rebalancing API reads only local in-process snapshots, refreshed from
  Redis in the background about every **10 seconds**. It does not make an
  upstream LTA request and it does not perform Redis I/O on each incoming API
  request.
- The 20 EV simulators continue to use the lightweight
  `/api/v1/traffic/simulation-events` endpoint introduced in PR #25, rather
  than downloading any live LTA feed.
- Facilities still has **independent parking/charging polling**. No Facilities,
  Fleet or Dispatch persistence structures are changed.

## Refresh policy

| LTA feed | External check period | Maximum usable observation age |
|---|---:|---:|
| v4 Traffic Speed Bands | 300 seconds | 900 seconds |
| Traffic Incidents | 90 seconds | 300 seconds |
| Approved Road Works | 86,400 seconds | 172,800 seconds |

Checks occur whether or not the provider's data changed, because there is no
assumed supported upstream push/change API. After retrieval, a SHA-256 digest of
normalised rows acts as the **content version**. When the digest is unchanged,
Rebalancing preserves the in-memory decoded rows instead of decompressing and
parsing a large payload again. Last-success time is still advanced only after
an actual successful LTA fetch; merely reading Redis cannot renew freshness.

Speed Bands are *observed changing traffic speeds*, not legal speed limits.
The v4 feed is paginated at up to 500 records per request. This collector
retains a bounded pagination limit (60 pages / up to 30,000 records) and a
12 MiB maximum normalised payload. If the provider exceeds those boundaries,
the feed fails explicitly instead of silently accepting an incomplete
dataset. The collector's logs identify safe error codes
(`ENDPOINT_NOT_FOUND`, `AUTH_FAILED`, `RATE_LIMITED`,
`UPSTREAM_UNAVAILABLE`, `FETCH_FAILED`, `PAGINATION_LIMIT`)
without printing API keys or full requests.

## Cache consistency and failure cases

- A dataset is never considered fresh simply because it was cached. Each
  feed's age is computed from the **last LTA success timestamp**.
- Upstream failures preserve the previous successfully collected timestamp;
  after its maximum age, rows are withheld and marked `STALE`.
- Redis restart, cache eviction, missing digest, malformed payload, or
  unavailable metadata gives explicit `CACHE_UNAVAILABLE` /
  `CACHE_PENDING` / `CACHE_INVALID`; routes are **never assumed clear**.
- A collector crash cannot kill Rebalancing. Conversely, a working API is not
  evidence that live LTA observations are available.
- Redis is **ephemeral** in this local demo (64 MiB max memory, LRU). Data may
  need another successful refresh after a Redis restart; no persistence
  guarantee or HA ownership is claimed.
- Collector runs as a single replica with `Recreate` strategy. Do not scale
  it horizontally without a leader-election / lease design.
- Under `TRAFFIC_SOURCE=DISABLED` the collector stays idle and all live
  traffic remains explicitly disabled. Synthetic operator road disruptions
  are independent and remain in the single Rebalancing instance, expiring
  or clearing as previously documented.

## Config and deployment

Kubernetes ConfigMap sets `TRAFFIC_CACHE_BACKEND=REDIS`, and the dedicated
collector reads the backend-only LTA AccountKey from the existing
`ev-fleet-facilities` Secret. The Rebalancing container **no longer receives
the LTA AccountKey** in the Kubernetes manifests. A valid key is needed only
when `TRAFFIC_SOURCE=LTA_DATAMALL`.

The local Compose stack now includes a Redis and traffic-collector service.
It also selects `TRAFFIC_CACHE_BACKEND=REDIS` for Rebalancing.

**Important:** This PR modifies several Kubernetes deployments. Only
redeploy after finishing and reconciling active rentals, relocations and
charging tasks. Keep PostgreSQL and its PVC intact, and preserve your
existing local `.env` credentials. Do not publish them.

To verify locally from the repository root:

```powershell
python -m pytest tests/unit/test_shared_traffic_cache.py tests/unit/test_traffic_routing.py tests/unit/test_traffic_scenarios.py -q
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
kubectl kustomize .\k8s
docker compose config
```

After controlled deployment with the repository's normal
`scripts/start-kubernetes.ps1` flow, inspect:

```powershell
kubectl -n ev-fleet get deployments traffic-collector rebalancing redis
kubectl -n ev-fleet logs deployment/traffic-collector --tail=60
kubectl -n ev-fleet logs deployment/rebalancing --tail=60
kubectl -n ev-fleet get pods -l app.kubernetes.io/name=rebalancing
kubectl -n ev-fleet top pods
```

Use the Operator Dashboard to inspect **each** feed's `status`,
`observed_at`, `error`, and row count. Check that Rebalancing restart
counts stop increasing and that requests/health checks remain responsive
with traffic enabled, including after cache loss and provider errors.
Speed Bands v4 must be **verified against the currently accessible LTA
endpoint** using a live account; offline unit fixtures cannot guarantee
provider availability or dataset size.

For research measurements, save before/after observations on the **same**
machine with identical numbers of EVs, stable workload, and repeated trials.
This change reduces repeated work but should not be claimed as a verified
performance improvement until it has been measured.
