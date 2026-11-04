# Facilities Availability Grace Design

## Intent

Prevent Fleet Overview from flickering between `AVAILABLE` and `UNAVAILABLE` when an otherwise healthy EV encounters a brief timeout while refreshing parking information from the Facilities service.

## Behaviour

- After a successful Facilities response, retain the last verified parking and charging state through transient refresh failures for up to 10 seconds.
- If no successful Facilities response has ever been received, continue to report the EV as unavailable.
- If Facilities remains unreachable beyond 10 seconds, clear the retained parking authority and report the EV as unavailable until a refresh succeeds.
- Valid Facilities responses apply immediately. Pending parking, charging, relocation, rentals, claims, stale telemetry, and other existing causes of unavailability receive no grace period.
- A successful response resets the failure/grace window.

## Approaches considered

- **Time-based grace period (selected):** predictable safety boundary independent of retry timing.
- **Consecutive-failure counter:** simpler, but the effective grace duration changes with polling and backoff.
- **Immediate retry:** increases load during the resource contention that causes the timeout.

## Scope and constraints

The change is limited to the moving simulator's Facilities refresh handling and its configuration/tests. It does not weaken Fleet's telemetry staleness checks or change dispatch, rental, charging-task, or relocation eligibility rules. Tests will cover startup fail-closed behaviour, recovery within grace, expiry after 10 seconds, and reset after a successful refresh.
