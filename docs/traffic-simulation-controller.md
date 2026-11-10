# Traffic Simulation Controller and Operator Map Overlay

This PR adds an **optional, operator-controlled simulation**. It is not
live Singapore traffic, a traffic light simulator, physical fleet control
or an automatic mid-journey rerouting system.

## One shared data source

1. Approve a real, OneMap/OSRM road-only relocation with normal Dispatch checks.
2. Open the Simulation Console at localhost:8090. In **Traffic Simulation
   Controller**, choose a real Dispatch route, select an exact geometry
   segment index, choose CONGESTION, ROADWORKS or ROAD_CLOSED and duration.
3. The console POSTs to the Rebalancing Service using a **server-held
   secret**, not a browser credential. Rebalancing independently fetches the
   referenced Dispatch and checks the approved geometry, segment bounds,
   Singapore coordinates, maximum 3 km segment length and optimistic
   scenario version. Arbitrary user-drawn connector lines are rejected.
4. Rebalancing's read-only `/api/v1/traffic/conditions` returns fresh synthetic
   events and their exact segment endpoints. When LTA traffic is enabled, its
   independently refreshed observations remain in a separate
   `live_observations` snapshot. The Operator Dashboard polls every five seconds
   and renders both layers with explicit `SIMULATED` and `LTA observed` labels.
5. The *same* events feed the single and batch recommendation assessment,
   and Dispatch rechecks the actual road route geometry at operator approval.
   Confirmed synthetic closures can make a route ineligible. Legacy approval
   can consider an OSRM alternate. CRDS requires a final OneMap route.
   Roadworks contribute delay and risk. Assessment evidence can show traffic
   ETA and predicted arrival SOC.
6. A scenario expires automatically (1–120 minutes), can be removed or
   cleared explicitly, and uses version-checked writes. The source reverts
   to live LTA observations or DISABLED when no simulated scenarios remain.

**Accuracy boundary:** [CRDS](crds.md) matches traffic to OneMap road geometry
during planning and checks the final route again at approval. Its objective
remains fleet-wide shortage reduction. Legacy previews screen approximate
point-to-point corridors and may apply the traffic cost. Modeled ETA is an
estimate, and simulator movement applies segment speed caps and closures
to the issued route.

## Safe optional setup (local Kubernetes)

Traffic simulation is **disabled by default**. Do not enable this on an
internet-facing Simulation Console. The backend-only control key must be
at least 16 characters and is never given to the browser.

On Windows, the normal `scripts/start-kubernetes.ps1` deployment validates the
whitelisted traffic values in root `.env`, pipes the three backend Secrets
directly to Kubernetes, patches only the non-secret traffic flags, and restarts
their consumers. `-ConnectOnly` deliberately does not read `.env` or change the
cluster. Configure both live observations and local simulation with:

```dotenv
TRAFFIC_SOURCE=LTA_DATAMALL
LTA_DATAMALL_ACCOUNT_KEY=<your backend-only LTA key>
TRAFFIC_SIMULATION_ENABLED=true
TRAFFIC_SIMULATION_CONTROL_KEY=<at least 16 random characters>
```

Use a randomly generated operator secret, stored as a Kubernetes Secret
(not in Git); example, run in PowerShell with a locally set environment
variable and **do not paste it into chat or share shell history**:

```powershell
# Generate/store an operator key securely in your local environment first.
kubectl -n ev-fleet create secret generic ev-fleet-traffic-control `
  --from-literal=TRAFFIC_SIMULATION_CONTROL_KEY="$env:TRAFFIC_SIMULATION_CONTROL_KEY"
kubectl -n ev-fleet patch configmap ev-fleet-config `
  --type merge -p '{"data":{"TRAFFIC_SIMULATION_ENABLED":"true"}}'
```

The Rebalancing and Simulation Console deployments read
`ev-fleet-traffic-control`; Dispatch receives only the **enable flag**
from `ev-fleet-config`. The Rebalancing service needs access to Dispatch
at `http://dispatch:8000`. Rebuild/redeploy safely **only while no
rentals/relocations/charging operations are active**. Patching ConfigMaps
does not hot-reload existing process environments. OneMap routing must
be configured for real road geometries.

For Compose, set (locally, never commit credentials):

```dotenv
TRAFFIC_SIMULATION_ENABLED=true
TRAFFIC_SIMULATION_CONTROL_KEY=<a long random key>
ROUTING_PROVIDER=ONEMAP
```

There is no Compose Simulation Console profile in this change; the
privileged controller UI is provided by the Kubernetes Simulation Console.
The Rebalancing backend's mutation endpoints require the same key.
Do not expose the mutation API directly to unauthorised networks.

## Limitations and validation

- The 20 moving EV simulators now poll the same simulation snapshot
  every two real seconds when this opt-in mode is enabled. Their existing
  road-following physics loop applies the configured **speed cap** when
  traversing the exact affected OneMap/OSRM segment; at ROAD_CLOSED it
  holds the EV on its road position (0 km/h) until the scenario is cleared
  or expires. Fleet continues receiving current zero-speed observations.
  This does **not** automatically reroute an EV already moving: the
  immutable Dispatch command can trigger operator review, but safe
  detours require a separately acknowledged versioned pause/replan
  protocol. On network outage, the simulator keeps its last scenario
  observation until the event's stated expiry.
- The simulated ETA heuristic can exaggerate a tiny affected segment:
  it uses a journey-wide speed factor instead of microscopic segment
  travel-time accumulation. It is an explicit teaching demonstration,
  not calibrated transport prediction.
- Simulation scenarios are intentionally **ephemeral** in one Rebalancing
  replica. They are cleared on restart; this avoids persistent fictional
  road closures silently surviving deployment. Not a distributed
  multi-replica traffic-control authority.
- While a synthetic scenario is active, it remains the decision source for
  simulation-aware ranking and movement. Live LTA observations continue to be
  fetched and displayed as a separate evidence layer; they are not silently
  flattened together with fictional events.
- Live LTA Approved Road Works have road names without verified drivable
  segment geometry; the dashboard continues to list them, without
  guessing road segments.
- With no usable traffic feed, the system reports unknown conditions;
  it never assumes roads are safe because no incidents were returned.
- The user must perform a controlled live Kubernetes + OneMap smoke test.
  Offline unit/frontend/Compose tests do not prove actual Singapore road
  access, real traffic coverage, EV motion or measured time savings.

## Troubleshooting: dashboard traffic feed HTTP 502

The operator dashboard calls `/api/rebalancing/traffic/conditions` through its
Nginx reverse proxy. The Rebalancing endpoint reads **cached** DataMall traffic
snapshots; the upstream polling loop normally reports `PARTIAL`, `STALE`,
`MISSING_KEY` or a specific feed error as JSON without turning a missing LTA
reading into a successful clear-road observation. Thus a browser-side
`HTTP 502` by itself is **not** proof the LTA AccountKey is bad; it often
means Nginx cannot reach Rebalancing or its upstream response failed.

On the local Docker Desktop Kubernetes cluster, inspect:

```powershell
kubectl -n ev-fleet get pods -l app.kubernetes.io/name=rebalancing
kubectl -n ev-fleet get endpointslices -l kubernetes.io/service-name=rebalancing
kubectl -n ev-fleet logs deployment/rebalancing --tail=100
kubectl -n ev-fleet logs deployment/dashboard --tail=100
kubectl -n ev-fleet port-forward service/rebalancing 18003:8000
```

In a **second PowerShell terminal** while the port-forward runs:

```powershell
Invoke-RestMethod http://127.0.0.1:18003/api/v1/traffic/conditions |
  ConvertTo-Json -Depth 8
```

- Direct endpoint **HTTP 200** with `PARTIAL` or `STALE` feed entries:
  the Rebalancing API is reachable, but some live LTA data are not fresh.
  Check each entry's `error` and `observed_at`, then check the
  configured traffic source and whether the backend secret was loaded.
- Direct endpoint **HTTP 200** but browser still sees **502**:
  inspect Dashboard Nginx proxy logs and the Service/EndpointSlice.
- Direct endpoint fails or the Rebalancing Pod restarts:
  investigate Rebalancing startup/readiness and backend logs before
  changing any credentials or traffic-assessment rules.

The frontend's cached condition warning is intentional. Never treat
unknown conditions as an unobstructed road or disable fail-closed route
checks to suppress a 502. Never post real API keys in logs or screenshots.


### Diagnosed from the 3 November 2026 local Kubernetes logs

The operator-supplied logs showed `rebalancing-6464858fd8-zbhqt` in
`CrashLoopBackOff` with 17 restarts, while Nginx logged `connect() failed
(111: Connection refused)` for **both** traffic conditions and charging advice.
Before shutdown, Rebalancing returned 200 for traffic, but some calls and
even health requests took over 8 seconds. This supports upstream
unavailability as the proxied HTTP 502 cause; it **does not** establish
whether the termination was due to liveness failures, OOMKill or another
reason.

The default Rebalancing liveness probe allows 2 seconds per check,
failure threshold 3. Capture Pod termination status and probe events
**before another redeploy**:

```powershell
kubectl -n ev-fleet describe pod rebalancing-6464858fd8-zbhqt
kubectl -n ev-fleet logs rebalancing-6464858fd8-zbhqt --previous --tail=100
kubectl -n ev-fleet get events --sort-by=.lastTimestamp
```

With traffic simulation enabled, each of 20 moving EV simulators previously
downloaded `/api/v1/traffic/conditions` every two seconds. This can include
large live LTA speed-band payloads irrelevant to their simulated movement.
The simulator now requests the small, bounded
`/api/v1/traffic/simulation-events` projection containing only approved,
unexpired simulated road conditions. The dashboard's full live
`/api/v1/traffic/conditions` remains unchanged, and the movement module's
expiry checks remain active.

This removes avoidable high-frequency response generation and transfer;
it is **a targeted load reduction, not a demonstrated CrashLoopBackOff cure**.
Verify locally with the old and new workloads, check liveness events and
CPU metrics, and keep any unknown route conditions unknown. Do not paper
over the issue by disabling readiness/liveness or replacing missing LTA
reports with clear-road assumptions.
