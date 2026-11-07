# Dispatch Persistence Write Reduction Design

## Intent

Keep Dispatch available under simulator and dashboard polling by avoiding full PostgreSQL snapshot writes when durable Dispatch state has not changed.

## Scope

- Read-only dispatch and command polls persist only when their reconciliation work changes durable record state.
- A reconciliation cycle persists at most once, after all eligible records have been processed, and only if at least one record changed.
- Existing snapshot contents, command delivery behavior, mutation endpoint persistence, and fail-closed persistence handling remain unchanged.

## Approach

Cache the exact frozen snapshot from the last successful save. Read paths compare the complete current snapshot with that cache while holding the persistence lock: identical state crosses the durability barrier without a database write, while changed state is saved before the response returns. Reconciliation also compares each record before and after refresh so it can issue at most one conditional save per cycle. This is preferred over a new dirty flag because all existing mutation paths remain authoritative and cannot silently forget to mark themselves dirty.

## Alternatives considered

- Dirty flags: cheaper per poll, but every current and future mutation site would need correct marking.
- Time-based write throttling: reduces load but can knowingly leave acknowledged state outside PostgreSQL for a time window.

## Verification

Regression tests will prove unchanged reads make no repository save, changed reads still save, concurrent reads cannot bypass a failed save, queued reads do not repeat a completed save, and a cycle containing multiple changes produces one save. The repository's required local and integration suites will then be run.
