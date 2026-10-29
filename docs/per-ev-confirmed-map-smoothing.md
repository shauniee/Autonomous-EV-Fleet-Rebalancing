# Per-EV smooth map display (display-only)

This is a follow-up PR to the edge digital-twin work merged into `main`.

## What changed

- Dashboard polls **authoritative Fleet state every 1 real second** instead of
  the previous 2-second interval. This matches the moving simulators' usual
  1 Hz telemetry rate, but HTTP network time can still make polling irregular.
- Every EV uses **its own active Dispatch ID** to look up its approved
  OneMap/OSRM road geometry, independently of the selected journey. Missing
  routes never fall back to a straight connector line.
- A new sample is animated only if its **session ID matches**, its sequence
  increases, and its observed timestamp is strictly newer (maximum gap 4s).
  Both observed positions must project close enough to that immutable route
  geometry. Elapsed observed time determines the animation duration:
  90% of the sample interval, bounded to **250–1400 ms**.
- Unrelated zone, traffic, dispatch or dashboard refreshes **do not cancel
  and restart** the in-progress animation for an unchanged observation.
- Once an EV's data is stale/offline, the Fleet polling connection is down,
  route geometry is unavailable, or the observed position is off-route,
  animation stops. Display stays on the **last confirmed location** rather
  than estimating where the EV might have travelled. Missing route geometry
  must be fixed in Dispatch/OneMap, not by relaxing the 50 m arrival rule.
- With an outage longer than 4 seconds, the next confirmed sample is shown
  directly. A visible jump is an *honest correction* after missing data;
  the browser does not extrapolate during the outage.
- On a browser-side Fleet polling failure or stale Fleet HTTP response, the
  map shows a visible warning and mutes EV markers. This does not modify the
  backend's independent 15-second stale / 30-second offline thresholds.

## Source of truth and safety

`MovingNode` remains authoritative for movement and telemetry. Fleet checks
the session, sequence, time and dispatch ownership. Leaflet has **zero**
movement-control authority: it neither writes Fleet state nor commands a
simulator. The local position used for easing always lies between two
accepted readings on the recorded route; displayed progress never extends
beyond the newer accepted reading. This is visual interpolation, not
simulation, GPS prediction, traffic-aware rerouting or actual vehicle control.

## Verification

```bash
cd dashboard
npm ci
npm test -- --run
npm run build
```

The focused `confirmedMotion.test.ts` covers per-vehicle route ownership,
route corners, duration, session/sequence discontinuities, off-route points,
missing geometry, stale/offline vehicles and network observation gaps. CI
also runs the repository backend, Compose and Kubernetes portability checks.
Live browser/OneMap traffic, 20-EV load and reconnect behaviour still require
a controlled Kubernetes smoke test. Safe-activity redeploy guard remains in place.
