# Legacy Parking Road-Access Repair Design

## Intent

Repair legacy occupied parking holds that predate persisted `access_position`, so an idle restarted EV is placed on a road before it publishes telemetry or accepts a route.

## Scope

- Only an `OCCUPIED` hold whose `ParkingHoldRequest.access_position` is missing is repairable.
- The simulator road-matches the restored parked position once during safe idle startup.
- Facilities validates the matched coordinate remains within the existing 150-metre facility road-access limit, persists it in the hold, and returns the repaired parking state.
- A hold that already has an access coordinate cannot be rewritten by this repair path.
- Active journeys remain ineligible for startup reconstruction.
- The Dispatch route-origin safety limit remains exactly five metres.

## Approach

Expose an explicit `access_position_missing` flag in `VehicleParking`. On first simulator initialisation, an occupied legacy hold restores the facility-centre fallback, is road-matched through Dispatch, and is sent to a Facilities repair endpoint. Facilities performs an atomic compare-and-set under its existing lock: it confirms the occupied hold still exists, confirms the coordinate is within the facility access radius, fills only a missing `access_position`, increments the snapshot version, and persists before returning. The simulator then adopts the returned persisted access point before opening a telemetry session.

This makes the repair one-time and restart-safe. Subsequent restarts read the persisted access coordinate and make no road-match or repair request.

## Failure behavior

Road-match, validation, persistence, identity, or response-shape failure aborts registration before telemetry. Concurrent repairs are idempotent only when they submit the same coordinate; a different coordinate is rejected once the hold has been repaired.

## Verification

Tests will prove repair persistence across store recreation, distance and overwrite rejection, startup repair before telemetry, and no second road-match after the repaired state is persisted. Existing strict routing tests must continue to prove that an origin more than five metres from the route is rejected.
