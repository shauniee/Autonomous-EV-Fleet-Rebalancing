# Optional OneMap driving routes

Optional road-following simulation using the existing Dispatch, Fleet and map.

## Configure

Copy these entries from `.env.example` into your own `.env` and supply a
**OneMap REST access token** (the LTA AccountKey is unrelated):

```dotenv
ROUTING_PROVIDER=ONEMAP
ONEMAP_ACCESS_TOKEN=your-onemap-access-token
```

OneMap requires registration and token generation through its
[authentication API](https://www.onemap.gov.sg/apidocs/authentication). The REST
documentation states tokens expire after three days; replace expired tokens and
reload configuration. No email or password is stored by this scaffold.

From the repository root, run the full macOS startup between demos:

```bash
bash scripts/start-kubernetes.sh
```

It places routing configuration in `ev-fleet-routing`, referenced only by Dispatch.
The existing renderer accepts `--routing`; `--connect-only` never reads `.env` or
reloads configuration. Compose passes the same two variables only to Dispatch.
For direct Python execution, export the variables yourself. Tokens never enter
browser code, route records or log messages.

Use `ROUTING_PROVIDER=SIMULATED` for the original straight-line model. OneMap mode
does not silently fall back when a token, API request or route is invalid.

## Approved journey

1. Select an LTA parking or charging destination through the usual recommendation
   and explicit approval workflow. Candidate ranking remains the existing
   approximate-distance heuristic; this scaffold does not call routing for every pair.
2. During approval, Dispatch calls the authenticated
   [driving route endpoint](https://www.onemap.gov.sg/apidocs/routing) once. It
   validates/decodes the geometry and instructions, then rechecks the EV's origin,
   current version, battery reserve and destination charging requirement.
3. The command carries the immutable route. Fleet includes road distance in its
   atomic reserve check before claiming the car. No route credential is included.
4. The simulator interpolates along successive road segments, consuming energy
   over road distance and using OneMap's journey duration as its average speed.
   It does not cut a straight line between the origin and destination.
5. The map's **Routes** checkbox controls active route overlays. EV markers use
   existing Fleet telemetry. Expand **OneMap route & directions** in a relocation
   card for the route length, duration, remaining-time estimate and instructions.

## Limits and errors

- Travel times are OneMap estimates. Live-traffic adjustment has not been verified.
  Movement uses a uniform average speed: traffic signals, per-road speed changes,
  live rerouting, charging taper and vehicle dynamics are not simulated.
- The existing 30x clock accelerates journeys. UI durations are simulated journey
  minutes, not a promised wall-clock arrival time. Remaining time is proportional
  to distance and cannot infer waiting time during communication failures.
- Tracking advances when telemetry arrives (five seconds by default, dashboard
  polling every two seconds). It is not continuous GPS tracking.
- OneMap can snap a carpark coordinate to a road. Both route endpoints must fall
  within the configured arrival tolerance (50 m by default). Larger offsets are
  rejected as `ONEMAP_ENDPOINT_TOO_FAR`; no invented access road is added. Arrival
  uses the road endpoint and the existing facility proximity check.
- Missing/expired tokens, quota errors, invalid data and no-route responses produce
  explicit `ONEMAP_*` failure reasons before a new parking hold or vehicle claim.
  Such failures are rejected rather than retried by the reconciliation loop.
  Request a new recommendation after correcting configuration or an outage.
- Once issued, the route is fixed. Existing active-journey restart limitations
  remain; there is no new crash-recovery or production navigation guarantee.

Routing is disabled by default. Live authenticated responses and the full journey
remain unverified until a token is supplied and the app is rebuilt.
