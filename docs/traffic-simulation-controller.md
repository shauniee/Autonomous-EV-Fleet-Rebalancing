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
4. Rebalancing's read-only `/api/v1/traffic/conditions` returns **mode:
   SIMULATION**, fresh synthetic events and their exact segment endpoints.
   The Operator Dashboard polls every five seconds, plots orange congestion,
   amber roadworks and dashed red closures, and lets dispatchers inspect
   the affected segment. It separately labels live LTA data when active.
5. The *same* events feed the single and batch recommendation assessment,
   and Dispatch rechecks the actual road route geometry at operator approval.
   Confirmed synthetic closures reject an affected route if no valid
   alternative road geometry is available. Roadworks are delay/risk
   events, **not** hard closures. Recommendations include traffic ETA
   versus the synthetic 30 km/h baseline, plus predicted arrival SOC.
6. A scenario expires automatically (1–120 minutes), can be removed or
   cleared explicitly, and uses version-checked writes. The source reverts
   to live LTA observations or DISABLED when no simulated scenarios remain.

**Accuracy boundary:** matching during initial recommendation uses approximate
point-to-point candidate corridors, not actual routes; road geometry is
checked again at Dispatch approval. The illustrative scoring formula
still has an uncalibrated origin-supply term. A short simulated segment can
be treated as a journey-wide delay in ETA screening; do not interpret its
numeric ETA as verified street-level trip time.

## Safe optional setup (local Kubernetes)

Traffic simulation is **disabled by default**. Do not enable this on an
internet-facing Simulation Console. The backend-only control key must be
at least 16 characters and is never given to the browser.

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

- This PR does **not** modify simulator vehicle speed or automatically
  reroute an EV already moving. The immutable Dispatch route can trigger
  operator review, but in-flight replanning requires a versioned and
  acknowledged pause/replan command protocol. The map shows explicit
  changes in simulated conditions, not fabricated movements.
- Simulation scenarios are intentionally **ephemeral** in one Rebalancing
  replica. They are cleared on restart; this avoids persistent fictional
  road closures silently surviving deployment. Not a distributed
  multi-replica traffic-control authority.
- Live LTA Approved Road Works have road names without verified drivable
  segment geometry; the dashboard continues to list them, without
  guessing road segments.
- With no usable traffic feed, the system reports unknown conditions;
  it never assumes roads are safe because no incidents were returned.
- The user must perform a controlled live Kubernetes + OneMap smoke test.
  Offline unit/frontend/Compose tests do not prove actual Singapore road
  access, real traffic coverage, EV motion or measured time savings.
