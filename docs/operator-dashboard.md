# Operator dashboard

React/TypeScript/Vite renders the existing six Singapore zones with Leaflet. The UI is a client of the existing services. It does not calculate shortage, severity, eligibility, ranking, route energy or dispatch transitions. EV movement and charging are simulated. Facilities and routes can use configured
provider observations.

## Run locally

Start the four existing backend services and moving EV nodes using the dispatch and relocation instructions. In a second terminal:

```powershell
cd dashboard
npm.cmd ci
npm.cmd run dev
```

Open http://127.0.0.1:5173. Vite proxies `/api/fleet`, `/api/demand`, `/api/rebalancing` and `/api/dispatch` to ports 8001–8004, rewriting the prefix to `/api/v1`. Override targets with `FLEET_URL`, `DEMAND_URL`, `REBALANCING_URL`, and `DISPATCH_URL` in the Vite process environment or `dashboard/.env.local`. No CORS backend changes are needed. Use a Node version supported by the locked Vite release (Node 22.12+ is suitable).

## Docker demonstration

```powershell
$env:DEMAND_SIMULATION_ENABLED = 'true'
docker compose --profile relocation up --build dashboard ev-moving-01 ev-moving-02
```

Open http://127.0.0.1:8080 (`DASHBOARD_PORT` changes the host port). The dashboard's Compose dependencies start Fleet, Demand, Rebalancing, Dispatch and Facilities; Rebalancing starts Redis and the traffic collector. The collector fetches enabled LTA feeds without exposing the account key to the browser. The explicit moving-EV service names avoid starting the stationary initial service foundation EVs, which do not consume dispatch commands. Do not run stationary and moving processes with the same vehicle ID. Existing default Compose behaviour is retained; dashboard is in the `relocation` profile.

The opt-in environment variable enables demand and heat-map demand simulation. Without simulation or manual demand updates, unknown demand is correctly shown as Unknown. New simulator observations follow the configured interval (default 30 seconds).

The dashboard has a Node build stage and an Nginx static runtime. Nginx resolves internal service names per request using Docker DNS, so a backend outage does not prevent the dashboard itself loading. Its health endpoint checks static-serving availability, not backend health. Backend panels independently report API failures.

## Operator workflow

1. Select a zone on the map or in the keyboard-accessible zone list.
2. Read current and projected shortage, demand, supply and freshness.
3. Request a recommendation. When the [CRDS](crds.md) queue is enabled, the
   rebalancing request is evaluated in the background. Select **View request**
   or open **Recommendations** to inspect it.
   Other requests use the synchronous recommendation endpoints.
4. Inspect the decision and **Evidence**, including the reason, relevant EV and
   facility exclusions, route estimates and modeled shortage effects when available.
   Legacy recommendations show trip scores instead.
5. Review an approvable plan and explicitly approve it. Open **Relocations** to
   inspect approval decisions and journey status.
6. Follow backend acknowledgement, lifecycle, distance travelled and SOC under Relocations. On arrival, Fleet updates the destination and availability.

Recommendations expire according to backend `expires_at` (default 120 seconds). Target changes clear recommendations. Changes to the zone definition or Demand instance can cause `TARGET_CHANGED`.
Ordinary demand updates can change CRDS benefit during revalidation. If approval
is refused, inspect the reason and request a new recommendation. The UI never silently substitutes another EV or automatically dispatches.

The map colours `hotspot_severity` as green NORMAL, amber WARNING, red CRITICAL and grey UNKNOWN. Raw `demand_level` is separately displayed; HIGH demand is not itself CRITICAL. Backend nulls display Unknown, not zero. The map plots approximate zone centres, not invented administrative boundaries or routes.

## APIs and ownership

| Service | Endpoint | Purpose |
|---|---|---|
| Demand | GET `/api/v1/heat-map` | Combined zone state and source freshness |
| Fleet | GET `/api/v1/vehicles` | Fleet-owned telemetry snapshot |
| Fleet | GET `/api/v1/vehicles/{vehicle_id}/claims/{dispatch_id}` | Dispatch origin receipt |
| Rebalancing | POST `/api/v1/recommendation-requests` | Queue a CRDS plan when enabled |
| Rebalancing | GET `/api/v1/recommendation-requests` or `/api/v1/recommendation-requests/{request_id}` | List and inspect queued assessments |
| Rebalancing | POST `/api/v1/recommendations` or `/api/v1/batch-recommendations` | Synchronous recommendations |
| Dispatch | POST `/api/v1/dispatches` or `/api/v1/batches` | Explicit single or batch approval with stable request ID |
| Dispatch | POST `/api/v1/batch-approval-groups` | Group review and approval when enabled |
| Dispatch | GET `/api/v1/dispatches` | Backend-reconciled lifecycle and outcomes |

The UI displays backend decisions and keeps response snapshots and operator interaction state. Origin before approval is labelled as the latest fleet snapshot; after approval it comes from the claim receipt. Recommendation SOC estimates remain labelled separately from live SOC. Progress uses backend distance and lifecycle, with no invented ETA or percentage.

## Refresh and failures

Heat-map, fleet and dispatch polls run independently every 2 seconds after completion, with no overlapping requests per resource. Failed polls back off to at most 10 seconds; requests time out after 8 seconds. Focus and Refresh now trigger fresh requests, cancelling obsolete polls. Claim receipts refresh every 10 seconds. Snapshots across services are not globally atomic; source times remain visible. Last successful responses stay visible with an unavailable/last-known warning when refresh fails.

Partial heat-map data retains demand and displays backend issues. Fresh fleet and heat-map fetches are required for new recommendations/approvals; the backend still owns final eligibility and validation. EV stale/offline flags and dispatch UNKNOWN/timeouts are displayed, never interpreted as claim release. Loss of Demand, Fleet or Dispatch does not erase healthy panels.

Approval creates one UUID only after explicit confirmation. Double submission is blocked synchronously. A failed/ambiguous network response retains the same approval payload in session storage and disables new approvals. **Retry same approval** sends the same ID; **Reconcile status** checks the dispatch list for that ID. A reload resumes reconciliation. This storage is only a pending operator intent, not durable backend state. If browser storage is blocked, in-page retries still work but reload recovery is unavailable. Restart retention depends on whether backend snapshot persistence is configured.

Public OSM raster tiles require network access. Attribution remains visible, browser cache behaviour is retained, and no prefetch/offline download feature is included. `VITE_TILE_URL` can select another compatible tile source at build/dev time (update attribution appropriately if changing providers). Tile failure shows a warning; the zone list and service panels keep working.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pytest -q
cd dashboard
npm.cmd test
npm.cmd run build
npx.cmd playwright install chromium
npm.cmd run test:e2e
```

Playwright starts isolated real services on ports 18001–18004, a readiness server on 18005, Vite on 15173 and two moving EV processes. It refuses occupied ports and does not reuse an existing backend. Demand is deterministic/manual; no frontend simulation is substituted. Public tiles are stubbed in browser tests, while Leaflet and zone/vehicle markers run normally. Tests cover explicit approval through real relocation/arrival and tile/Demand outage rendering. Backend logs and screenshots are written to `dashboard/test-results/`; failed browser tests retain traces.

For an occasional real-map smoke check, set `$env:REAL_MAP = '1'` before running the browser suite, then remove that environment variable afterward. The journey then requires successful real tile responses. Keep the default stubbed mode for routine tests.

Frontend unit/component tests cover semantic states, selection races, expiry, explanations, confirmation, ambiguous retry/reload, partial responses and backend lifecycle display. The existing Python regression suite is unchanged. See `operator-dashboard-verification.md` for observed results and runtime limitations.
