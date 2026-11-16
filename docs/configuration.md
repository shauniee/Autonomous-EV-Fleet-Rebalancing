# Configuration and credentials

Settings live in a `.env` file at the repository root. Copy `.env.example` to
`.env` and keep it out of version control. Its values need no credentials: mock
facilities, no live traffic, generated demand and
[offline demo routing](#offline-demo-mode-no-credentials).

The **recommended** setup adds two credentials:

- an **LTA DataMall Account Key**, for live car park, charger and traffic data;
- a **OneMap REST access token**, so approved relocations follow real Singapore roads.

Without a OneMap token, relocations cannot be approved. If you cannot get one,
use the [offline demo mode](#offline-demo-mode-no-credentials).

## LTA DataMall Account Key

[Request LTA DataMall API access](https://datamall.lta.gov.sg/content/datamall/en/request-for-api.html).
LTA emails you an **Account Key**.

```dotenv
LTA_DATAMALL_ACCOUNT_KEY=your-emailed-account-key
FACILITIES_SOURCE=LTA_DATAMALL
TRAFFIC_SOURCE=LTA_DATAMALL
```

Both feeds use the same key. `FACILITIES_SOURCE=LTA_DATAMALL` enables live
parking and charging data, and `TRAFFIC_SOURCE=LTA_DATAMALL` enables live road
traffic. To enable just one, leave the other at its default (`MOCK` for
facilities, `DISABLED` for traffic). See [LTA DataMall](lta-datamall.md).

## OneMap REST access token

[Register for a OneMap API account](https://www.onemap.gov.sg/apidocs/register).
Each token lasts **3 days**.

1. Send a `POST` to `https://www.onemap.gov.sg/api/auth/post/getToken` with
   `Content-Type: application/json` and a JSON body containing your OneMap
   `email` and `password` (see
   [OneMap authentication](https://www.onemap.gov.sg/apidocs/authentication)).
2. Copy the returned `access_token` into `.env`:

   ```dotenv
   ROUTING_PROVIDER=ONEMAP
   ONEMAP_ACCESS_TOKEN=your-onemap-rest-access-token
   ```

3. Request a new token before it expires and [apply the change](#apply-env-changes).

See [OneMap routing](onemap-routing.md) for details.

## Offline demo mode (no credentials)

```dotenv
ROUTING_PROVIDER=OFFLINE_DEMO
```

This runs the full recommend, approve and move flow without credentials, with a
reduced experience:

- Routes are straight lines, so EVs appear to cross buildings, parks and water.
- Distances and travel times are straight-line estimates at 30 km/h.
- There are no turn-by-turn instructions.
- The dashboard shows an "Offline demo routes" banner, and every route and
  approval is labelled `OFFLINE_DEMO`, never mistaken for a OneMap route.

Facilities and traffic stay on mock or disabled data unless you also add an LTA key.

## Apply `.env` changes

Wait until no relocations, rentals or charging tasks are active, then run the
normal startup again:

- Windows: `powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1`
- macOS: `bash scripts/start-kubernetes.sh`

Connect-only mode only reopens the dashboards and does not reload `.env`.

## Other settings

Every setting maps to an environment variable with the field name in upper case,
grouped by prefix (`CRDS_*`, `DEMAND_*`, `SIMULATOR_RECOVERY_*`, and so on).
`.env.example` lists them all. In Kubernetes, non-secret values come from the
`ev-fleet-config` ConfigMap, and `scripts/render_facilities_secret.py` turns
only whitelisted `.env` values into Secrets. See [deployment](deployment.md).

Useful switches:

| Variable | In `.env.example` | Effect |
|---|---|---|
| `CRDS_MODE` | `ACTIVE` | `ADVISORY` shows CRDS results but blocks dispatch; `DISABLED` uses the legacy weighted policy |
| `CRDS_EXECUTION_MODE` | `LOCAL` | `HTTP` sends scoring to the optional CRDS workers |
| `DEMAND_SIMULATION_ENABLED` | `true` | Runs Demand's automatic generator (the console can pause it) |
| `SIMULATION_TIME_SCALE` | `10` | Simulated seconds per real second |
| `TRAFFIC_SIMULATION_ENABLED` | `false` | Allows synthetic traffic scenarios from the simulation console |
