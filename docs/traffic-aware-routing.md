# Traffic-aware EV selection and road-disruption routing

Live LTA traffic is optional and requires `TRAFFIC_SOURCE=LTA_DATAMALL`.
The [simulation console's traffic controller](simulation-console.md#traffic-scenarios) can provide
synthetic disruptions. All examples below use synthetic
fixtures unless a valid LTA DataMall AccountKey is explicitly configured.

## 1. LTA feeds and dashboard

The dedicated single-replica collector retrieves three independent backend-only
LTA HTTPS feeds and publishes validated compressed observations to Redis:
- `v4/TrafficSpeedBands` (5-minute refresh): link endpoints and min/max speed;
- `TrafficIncidents` (90-second refresh): geolocated incident types/messages;
- `RoadWorks` (24-hour refresh): road names and approved start/end dates.

Rebalancing uses a locally cached, background-refreshed Redis read model,
never making LTA calls on a request path. See
[Shared traffic cache](#shared-traffic-cache) for consistency and failure
handling.

Source: LTA DataMall API User Guide, Traffic sections 2.16, 2.18, 2.19.
A valid `LTA_DATAMALL_ACCOUNT_KEY` is needed. DataMall pagination is bounded
to 400 pages of 500 entries *per feed*; reaching the limit fails that feed
instead of silently declaring coverage complete. Missing/stale feeds are
reported as unavailable/partial and are **not** treated as clear roads.
Cached results expire independently. A known stale speed reading cannot
be used indefinitely. `GET /api/v1/traffic/conditions` is a **bounded map display
projection**. Dispatch uses read-only `POST /api/v1/traffic/assess-route`
with actual road geometry, length and ETA to score against the **full**
indexed Speed Bands snapshot. `GET /api/v1/traffic/cache-status` reports
per-feed freshness and error metadata without downloading rows.
This route assessor checks fresh observed closures even while synthetic
traffic scenarios are active. Unknown feeds remain explicitly unknown.
Dashboard at http://127.0.0.1:8080 shows source, freshness, incidents,
active roadworks, observed speed segments and caveats. A traffic overlay
is optional and bounded to 800 spatially sampled speed links / 100 incident markers.

LTA approved roadworks specify a **road name, not an entrance, precise
geometry or a verified closure**. Roadworks add delay/risk to routes
associated with observed speed links on the same road; they do NOT automatically
close that road. The only hard block in the current implementation is a
geolocated LTA incident whose type explicitly says ROAD BLOCK or DIVERSION and
falls within ~120 m of a tested route's geometry.

## 2. Traffic-aware recommendation scoring

[CRDS](crds.md) applies traffic to verified OneMap geometry. It screens
blocked routes and uses travel time to project when the EV becomes usable.
It still ranks by modeled shortage reduction.

The **legacy** single and facility/batch paths use a deterministic cost
when route-overlapping traffic data is fresh:

`cost = .45*normalised_ETA + .25*normalised_kWh + .20*disruption_risk + .10*origin_supply_penalty`

The legacy callers use a zero origin supply penalty. Cost input
normalisers: ETA cap 60 minutes; kWh cap 15; disruption risk cap 1.
No claims of calibrated research weights. If no matching speed observation
exists, current distance/energy ranking is retained: UNKNOWN != free-flow.
The legacy preview uses approximate point-to-point corridors. Dispatch
checks actual road geometry at approval and can reject the selected move.
LTA speed coverage is incomplete and does not reveal every traffic event.
API output includes its traffic status and per-assignment annotations.

The existing Fleet freshness, rental, claim, charging-task, parking capacity
and minimum SOC safety checks are unchanged.

## 3. Dynamic route choice and road safety

In OneMap mode Dispatch fetches a fresh OneMap road route when the operator
approves. If traffic is enabled, Dispatch checks its actual geometry against
LTA observations and, when `OSRM_ROUTING_URL` is configured, requests up to
three alternative driving routes from the *server-owned* OSRM instance.
It chooses the least disrupted valid candidate, which must have:
- road-aligned origin within **5 m** (no start teleport or connector);
- road endpoint close to facility centre (150 m access screening);
- no explicit geolocated road block/diversion crossing the candidate;
- a valid road route and energy reserve rechecked by Dispatch and Fleet;
- the original 50 m arrival check against its final drivable endpoint.

Without an available valid alternative, a blocked route is **rejected
before dispatch**, not magically drawn around a closure. OSRM is an
optional external/self-hosted routing engine: this repository does not
ship a preprocessed Singapore road graph. Configure an accessible
`OSRM_ROUTING_URL` such as `http://osrm:5000` only after you provision
the engine. Do not use a public demo server for production.
OSRM's `alternatives` option does not guarantee distinct alternatives.
An OSRM alternate can be used for legacy approval. CRDS planning and
benefit revalidation require a OneMap route, so a final route marked
`OSRM` cannot pass CRDS revalidation.

Once a journey is issued, its route and command are deliberately immutable.
Dispatch exposes a read-only
`GET /api/v1/dispatches/{id}/traffic-check`, which examines the
remaining route and flags `BLOCKED_AHEAD` or `CONGESTED_AHEAD` for
operator review. **No in-flight route mutation or remote stop command
is implemented**; rerouting during a trip would need a future
acknowledged, versioned safe-command protocol to prevent EV teleporting
and stale optimistic-lock claims. Do not advertise the active-journey
warning as executed rerouting.

## 4. Reproducible evaluation

Run from the repository root with dependencies installed:

```powershell
python -m experiments.traffic.evaluate
python -m pytest tests/unit/test_traffic_routing.py -q
```

The evaluator compares baseline distance selection with traffic cost
under normal, congested, roadworks and roadblock synthetic inputs; outputs
JSON annotated SYNTHETIC_OFFLINE_COMPARISON. Unit tests cover feed parsing,
cache TTL, safe route exclusions, ranked EV changes and alternate road
geometry. These are demonstrations, **not real research measurements**.

## Configuration

```dotenv
TRAFFIC_SOURCE=LTA_DATAMALL
LTA_DATAMALL_ACCOUNT_KEY=your-lta-key
ROUTING_PROVIDER=ONEMAP
ONEMAP_ACCESS_TOKEN=your-onemap-token
OSRM_ROUTING_URL=http://osrm:5000
```

The last setting is optional; without OSRM, unsafe blocked routes fail closed.
With `TRAFFIC_SOURCE=DISABLED` (default), live LTA observations are absent.
CRDS can still use OneMap route evidence and any enabled simulated traffic.

Compose reads .env directly. On Windows Docker Desktop Kubernetes,
read .env and install/update the existing backend-only secrets securely:

```powershell
python scripts/render_facilities_secret.py | kubectl -n ev-fleet apply -f -
python scripts/render_facilities_secret.py --routing | kubectl -n ev-fleet apply -f -
```

The facilities secret is read by both Facilities and Rebalancing; the routing
secret by Dispatch. Do not commit, print or copy secret contents into chat.
Changing a Secret alone will not update running environment variables;
redeploy safely **only after** there are no active rentals, charging
operations or relocations. The Windows startup script has safeguards and
versioned images. The real integration needs a live LTA key, OneMap token
and (for alternative selection) a separately hosted, Singapore-loaded OSRM
instance. Those integrations are *not* live-verified by offline CI.

## Shared traffic cache

Live LTA feeds are fetched by one process and shared through Redis, so neither
Rebalancing nor the simulators call DataMall themselves:

- A single `traffic-collector` Deployment (`python -m ev_fleet.rebalancing.shared_traffic`,
  `Recreate` strategy) fetches and normalises the feeds. It writes the
  compressed data to Redis first and then swaps a small metadata pointer, so a
  reader never sees half an update.
- Rebalancing refreshes in-process copies from Redis about every 10 seconds in
  the background. API requests read those copies and never wait on Redis or LTA.
- Simulators read only the small `/api/v1/traffic/simulation-events` feed.
- Only the collector receives the LTA AccountKey. The browser never sees it.

| LTA feed | Checked every | Maximum usable age |
|---|---:|---:|
| Traffic Speed Bands (v4) | 300 s | 900 s |
| Traffic Incidents | 90 s | 300 s |
| Approved Road Works | 86,400 s | 172,800 s |

A SHA-256 digest of the normalised rows is the content version, so unchanged
data is not decoded again. Freshness comes from the last successful LTA fetch,
never from the time it was read out of Redis. After the maximum age, rows are
withheld and marked `STALE`. A missing, evicted or malformed cache reports
`CACHE_UNAVAILABLE`, `CACHE_PENDING` or `CACHE_INVALID`, and roads are never
assumed clear. Speed Bands pagination is capped at 400 pages and a 40 MiB
payload; going over fails the feed instead of accepting partial data.

Redis is ephemeral here (256 MiB limit, `noeviction`), so display-cache writes
cannot silently push out traffic data. With `TRAFFIC_SOURCE=DISABLED` the
collector stays idle. A collector crash does not affect Rebalancing; it only
makes live observations go stale.
