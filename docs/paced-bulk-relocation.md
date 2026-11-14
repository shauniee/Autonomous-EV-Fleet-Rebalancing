# Human-approved bulk relocation jobs

A bulk approval creates a durable job. Dispatch starts the reviewed EV-to-facility
assignments gradually instead of keeping the browser request open while every
vehicle is admitted. The API accepts up to 50 requested EVs; actual eligibility and
parking/charging capacity can still leave part of a request unfulfilled.

The operator approves the exact assignments while the recommendation is fresh.
That authorization lasts 30 minutes. Each admission rechecks current vehicle,
route, energy reserve, ownership, facility capacity and configured CRDS policy.
Dispatch never substitutes another EV or destination. Single-EV approvals retain
the original recommendation expiry rules.

The default global limit is four relocating or uncertain resource intents, with
at least one second between new batch admission attempts. Existing single and
manual admissions occupy slots too. Configuration keys are
DISPATCH_IN_FLIGHT_LIMIT, DISPATCH_ADMISSION_INTERVAL_SECONDS and
BATCH_AUTHORIZATION_SECONDS (maximum 1800). Use measurements before increasing
concurrency; Kubernetes API replicas do not remove Fleet's single-writer limit.

The dashboard separates accepted authorization, queued/waiting items, admitted
claims, command delivery and arrived vehicles. Transient dependency, routing,
rate-limit and planning-timeout failures retry automatically with bounded backoff.
Permanent safety/policy failures remain visible. A simulator whose ownership lease
expired still needs assessed operator recovery; the queue cannot resume it.

Stable child request/claim IDs prevent duplicate vehicles or parking holds across
HTTP retries and restarts. Frozen approval intent and retry metadata persist per
dispatch record. Ambiguous mutations reconcile exact requests. After authorization
expires, no new claims or holds may be created; known effects still reconcile and
clean up safely. Read-only status polling never advances admissions.

Queued unstarted items can be closed. An item with a possible remote mutation
must first establish whether a claim/hold exists; cancellation cannot simply erase
it. The existing journey recovery/end workflow remains required for active work.
Charging cable connection and disconnection remain human tasks.

## Verification and upgrade status

Implementation checks and isolated capacity measurements are recorded in
[the plan](superpowers/plans/2026-11-14-paced-bulk-relocation.md). The earlier
50-simultaneous workload failed and is not a supported-capacity claim. The paced
50-EV completion test passed: all 50 arrived with at most four active at once,
acceptance took 1.006 seconds and completion took 231.46 seconds at 30x simulated
travel. Two transient retries recovered automatically, with no manual recovery.
The fixture used offline 300-point road geometry and synthetic parking capacity,
five EV-driver processes and five services sharing a 4-CPU/2-GiB container, plus
a separate 2-CPU/1-GiB PostgreSQL container. Simulator RPC deadlines remained
2 seconds and leases 30 seconds. This does not establish capacity with live
OneMap/CRDS workloads or on every host.

The explicitly authorized local update is complete for Fleet, Dispatch,
Rebalancing and the dashboard. Simulator controllers and arguments were preserved.
All seven existing paused journeys resumed in two waves, completed and released
their claims. PostgreSQL confirms those outcomes are durable. See the
[completed local upgrade](paced-local-upgrade.md). The storage-format promotion and
rollback boundary are described in [bulk checkpoint storage](bulk-checkpoint-storage.md).
