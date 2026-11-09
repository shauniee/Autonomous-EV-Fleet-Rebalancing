# Local demo readiness: Windows laptop and desktop

This is an operator checklist for the same CSC3104 codebase on **each** machine.
The fleet and EV movement are simulated. Never point a local demonstration or test
at a real fleet, production PostgreSQL database, or existing Kubernetes deployment.

## 1. Prepare each computer independently

- Install Windows PowerShell, Python 3.12+, Node.js 22+, Docker Desktop using Linux containers, and `kubectl`.
- Enable Docker Desktop Kubernetes and check that `kubectl config current-context` is `docker-desktop` and `kubectl get nodes` reports a Ready node. Confirm ports **8080** and **8090** are available locally.
- Clone or pull the **same reviewed commit** on each computer. Keep `.env` untracked and private; a token/configuration from one computer does not automatically exist on the other.
- For a real road-following relocation demo, set `ROUTING_PROVIDER=ONEMAP` and `ONEMAP_ACCESS_TOKEN=<your current REST token>` in each computer's `.env`. Token validity is checked by OneMap when a route is requested; a saved token alone is **not** proof of reachability.
- `ROUTING_PROVIDER=SIMULATED` is a **read-only routing mode for the current dispatch path**. You can see fleet/demand/maps, but CRDS cannot create an approvable road route. An offline **test-only** route fixture does not change that production rule. The `OSRM_ROUTING_URL` setting is currently an optional traffic-blocked *alternate*, not a substitute for an initial verified OneMap route.

See [OneMap routing](onemap-routing.md) for the provider configuration and safety limits. Never commit provider tokens or paste them into screenshots.

## 2. Check and start without deleting fleet data

From the repository root in PowerShell:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

The test runner must report PASS for Python, dashboard Vitest/build and simulation console build **on each computer**. Do not count a static build as a passing browser test. For infra changes, optionally run the disposable `test-local.ps1 -Integration` workflow after installing `act`, as described in [Local testing](local-testing.md). Report integration as NOT VERIFIED if Docker or `act` is missing; never point it at existing data.

The normal startup rebuilds images, updates the local Kubernetes deployment, and opens/reuses the two dashboard tunnels. It **refuses to redeploy while an active rental, relocation or charging operation is in progress**. Do not use `-FreshBoot` as a casual troubleshooting step: it replaces project Pods but preserves PostgreSQL PVC data and still requires a quiescent simulation. `-ConnectOnly` only reconnects: it does **not** reload updated `.env` routing settings.

Open:

- Operator map/dispatch dashboard: <http://127.0.0.1:8080>
- Simulation Console: <http://127.0.0.1:8090>

## 3. Demonstrate demand → recommendation → human approval

1. **Simulation Console / Demand:** Pause automatic demand generation if you want stable manual values. Initialise fresh observations for **every** zone by selecting all zones and applying `0`. Then select one destination zone (for example CBD), set a higher desired available EV supply (for example `12`), and apply. This is a demo scenario, not real demand. CRDS uses the entire fleet and requires fresh demand in *all* zones, not only the selected high-demand destination.
2. **Operator dashboard / Dispatch:** Confirm the map has a current demand snapshot, Fleet observations are fresh, and Facilities are available. Check the road-routing setup notice. If routing is not configured, correct `.env` and restart the local deployment before attempting a dispatch.
3. Select the high-demand destination. Request a recommendation. A **MOVE** only appears if an available EV, suitable destination space, sufficient battery reserve, verified road route and a net fleet-wide shortage reduction are all present. If **HOLD** or **DEFER** appears, inspect **Evidence** for the exact reason; do not force a move.
4. Review the proposed EV, target, SOC, road-distance and assumptions. Only then explicitly **Approve**. The backend rechecks current state and may legitimately reject a proposal that became stale.
5. Open **Relocations** and **Map** to track command acknowledgement, a confirmed OneMap road path and reported EV movement. Map markers follow backend telemetry, not front-end animation. If an outcome is unconfirmed, **reconcile the same approval/request ID** instead of creating duplicate commands.
6. Show **Charging** and **Facilities** with their own observed/unknown states. Not every vehicle or facility must be eligible. The Simulation Console's Traffic and Rentals tools can demonstrate other scenarios separately, but rentals and charging may temporarily reduce eligible EVs.

**Important:** Paused demand stays fixed but can become too old for CRDS (the local default is `DEMAND_MAX_AGE_SECONDS=300`). Refresh the manual observations before expiry. Automatic generation has a manual hold (`DEMAND_MANUAL_HOLD_SECONDS=60` by default); if resumed, it can later replace your chosen scenario.

## 4. Diagnose a missing recommendation without bypassing safety

| What you see | Meaning / recovery |
|---|---|
| Road routing not configured / `ROUTING_NOT_CONFIGURED` | `ROUTING_PROVIDER=SIMULATED`; configure a valid OneMap token on this computer and redeploy. |
| `ROUTING_ACCESS_UNAVAILABLE` | Token absent, expired or rejected; recheck credentials and OneMap availability. Do not log the token. |
| `DEMAND_UNKNOWN_OR_STALE` | At least one zone has no current demand observation. Reapply fresh demand to every zone or start the generator. |
| `STALE_TELEMETRY` / Fleet unavailable | Wait for live simulator telemetry, investigate disconnected/failed simulator Pods, and retry. |
| `NO_SUITABLE_DESTINATION`, `PARKING_FULL`, `CHARGING_FULL` | There is no verified compatible parking/charging capacity at the target. Choose another available destination; do not invent capacity. |
| `ROUTE_BELOW_OPERATIONAL_RESERVE` | The checked road journey consumes too much battery. Choose another eligible EV or a nearer compatible site. |
| `NO_USEFUL_MOVE` | CRDS estimates that relocating would not sufficiently reduce **fleet-wide** shortage under its defined scenarios. High demand in one zone does not automatically make the transfer beneficial; inspect the origin-zone impact. |
| `DEFER` / route lookup incomplete | The system cannot confirm the decision. Restore the missing dependency and request a fresh assessment; do not approve an uncertain plan. |
| Review disabled / recommendation expired | Wait for current fleet/demand/facility snapshots and request a new recommendation. |
| Approval unknown | Check and reconcile the original request; reuse its ID for a retry rather than issue a different approval. |

If one machine behaves differently, compare the **commit SHA, `.env` routing provider presence (never reveal tokens), Docker/Kubernetes contexts, local Pod readiness, and timestamps**. Do not reset the PostgreSQL PVC to conceal inconsistent state.

## 5. Verification boundary

The default source-level tests use deterministic, labelled, synthetic route fixtures; those are **not** evidence that live OneMap can route or that a real Singapore road is safe. Complete a live local demonstration of OneMap routing, vehicle movement, and both dashboards on **each** computer before claiming end-to-end readiness. Record any blocked step and its error rather than reporting the full demonstration as passed.
