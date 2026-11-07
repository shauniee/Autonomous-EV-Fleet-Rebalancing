# Operational Snapshot Stability Design

## Intent

Stop the operational snapshot from flickering when a live poll briefly fails, while still refusing to present old values as current indefinitely.

## Scope

- A valid service response updates its displayed count immediately.
- A failed or missing response retains that service's last valid count for less than five minutes from its recorded `updatedAt` time.
- At five minutes old, or when no valid timestamp/data exists, the count becomes an em dash.
- Partial heat-map responses remain unavailable rather than being presented as valid zeroes or counts.

## Approach

Use the existing per-resource `data` and `updatedAt` fields as the last-valid observation. Increase the shared freshness window from ten seconds to five minutes; keep the existing timestamp validation and strict age boundary. A transient error changes the description to "Last confirmed observation" but does not erase still-current data.

## Verification

Component tests will cover a value just inside the five-minute window, the exact five-minute expiry boundary, immediate replacement by a newer valid value, and existing zero/partial-data behavior.
