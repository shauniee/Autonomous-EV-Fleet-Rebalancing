# Traffic-aware EV selection and road-disruption routing — one PR, four phases

This feature expands PR #13. **It is optional** and disabled unless
`TRAFFIC_SOURCE=LTA_DATAMALL` is set. All examples below use synthetic
fixtures unless a valid LTA DataMall AccountKey is explicitly configured.

## 1. LTA feeds and dashboard

The Rebalancing Service caches independent, backend-only LTA HTTPS feeds:
- `v3/TrafficSpeedBands` (5-minute cache): link endpoints and min/max speed;
- `TrafficIncidents` (90-second cache): geolocated incident types/messages;
- `RoadWorks` (24-hour cache): road names and approved start/end dates.

Source: LTA DataMall API User Guide, Traffic sections 2.16, 2.18, 2.19.
A valid `LTA_DATAMALL_ACCOUNT_KEY` is needed. DataMall pagination is bounded
to 20 pages of 500 entries *per feed*; reaching the limit fails that feed
instead of silently declaring coverage complete. Missing/stale feeds are
reported as unavailable/partial and are **not** treated as clear roads.
Cached results expire independently. A known stale speed reading cannot
be used indefinitely. API: `GET /api/v1/traffic/conditions` on Rebalancing.
Dashboard at http://127.0.0.1:8080 shows source, freshness, incidents,
active roadworks, observed speed segments and caveats. A traffic overlay
is optional and bounded to 500 speed links / 100 incident markers.

LTA approved roadworks specify a **road name, not an entrance, precise
geometry or a verified closure**. Roadworks add delay/risk to routes
associated with observed speed links on the same road; they do NOT automatically
close that road. The only hard block in the current implementation is a
geolocated LTA incident whose type explicitly says ROAD BLOCK or DIVERSION and
falls within ~120 m of a tested route's geometry.

## 2. Traffic-aware recommendation scoring

Both single-EV and facility/batch recommendations use a
deterministic, inspectable cost when route-overlapping traffic data exists:

`cost = .45*normalised_ETA + .25*normalised_kWh + .20*disruption_risk + .10*origin_supply_penalty`

The current supply penalty is 0 (not yet estimated from demand). Cost input
normalisers: ETA cap 60 minutes; kWh cap 15; disruption risk cap 1.
No claims of calibrated research weights. If no matching speed observation
exists, current distance/energy ranking is retained: UNKNOWN != free-flow.
The initial ranking uses **approximate direct point-to-point corridors**,
not OneMap geometry, which is available at dispatch approval. Consequently
an initial recommendation may be rejected or re-ordered at final road check.
LTA speed coverage is incomplete and does not reveal every traffic event.
API output includes its traffic status and per-assignment annotations.

The existing Fleet freshness, rental, claim, charging-task, parking capacity
and minimum SOC safety checks are unchanged.

## 3. Dynamic route choice and road safety

In OneMap mode Dispatch fetches a fresh OneMap road route when the operator
approves. If traffic is enabled, Dispatch checks its actual geometry against
LTA observations, and—when `OSRM_ROUTING_URL` is configured—requests up to
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
No live, official detour guarantees exist.

Once a journey is issued, its route and command are deliberately immutable.
Dispatch exposes a read-only
`GET /api/v1/dispatches/{id}/traffic-check`, which examines the
remaining route and flags `BLOCKED_AHEAD` or `CONGESTED_AHEAD` for
operator review. **No in-flight route mutation or remote stop command
is implemented**; seamless mid-trip rerouting requires a future
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
For the final report, also collect repeated, independently timestamped
live-feed experiments, route latency, simulator arrivals, SOC consumption,
unknown-coverage incidence and provider/API costs. Test at least five
trials per scenario on an idle Kubernetes cluster.

## Configuration

```dotenv
TRAFFIC_SOURCE=LTA_DATAMALL
LTA_DATAMALL_ACCOUNT_KEY=your-lta-key
ROUTING_PROVIDER=ONEMAP
ONEMAP_ACCESS_TOKEN=your-onemap-token
OSRM_ROUTING_URL=http://osrm:5000
```

The last setting is optional; without OSRM, unsafe blocked routes fail closed.
With `TRAFFIC_SOURCE=DISABLED` (default) the previous route/recommendation
behaviour remains and dashboard labels the traffic feed disabled.

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
