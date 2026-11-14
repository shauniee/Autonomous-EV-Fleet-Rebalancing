# Bulk approval during vehicle movement

Accepted fix: allow eligible batch recommendations to be approved while another
EV reports ordinary progress. Preserve human approval and genuine safety
rejections; physical or evidence constraints can still prevent full fulfilment.

The previous global planning revision advanced on every distance update, from
both simulator progress events and Fleet reconciliation. CRDS checks the revision
across approval revalidation and retries once. Normal ongoing movement could
therefore invalidate both attempts for every batch child.

The revision now describes admitted commitments and lifecycle changes. Routine
distance updates still appear in fresh planning snapshots and are persisted,
without changing that epoch. Arrival, failure, claims, parking, recovery and
resolution retain their existing revision changes. Rebalancing continues to
calculate benefit from current observations, and Fleet atomically rechecks the
selected EV's eligibility, route origin and battery before claiming it. Parking
capacity, telemetry freshness, recommendation expiry and human approval checks
remain in force. An expired recommendation must be requested again; ambiguous
responses must be reconciled or retried with the same batch request ID.

Each child is assessed against collected observations. Arrival-time forecasts can
change as other EVs move across timing slots; this is not an atomic guarantee of
fleet benefit over continuously changing telemetry.

Regression coverage includes six routes longer than 5 km with real in-process
progress events or Fleet reconciliation interleaved with every approval,
idempotent retry, lifecycle invalidation, an EV becoming depleted during approval,
and expired batches. These are isolated fixture-route tests, not live OneMap or
Kubernetes fleet dispatches. No approval deadlines or distance limits are raised.

Run from the repository root:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests/unit/test_batch_approval_progress.py tests/unit/test_batch_dispatch.py tests/unit/test_crds_available_fleet.py
```

## Operator-requested quantities

The dashboard sends `planning_mode=FULFIL_REQUEST` for facility-based Rebalance
requests. The batch continues beyond the useful-gain threshold and may use the
last available EV at a donor zone, with a coverage warning rather than an economic
policy rejection. It never subtracts stock that does not exist. Cars already
assigned to the destination are excluded from the incoming quantity.

A capacity-aware matching step considers compatible destination choices rather
than sending every EV toward its closest full car park. Ordinary and charger
bays are matched separately, preserving chargers for EVs needing charge. Road
routes and arrival energy are then verified for matched destinations within the
existing deadline. Route failures or changed observations can still leave a
partial/unavailable assessment; this is not an exhaustive road-route search of
every vehicle/facility pair.

Each saved child carries the planning mode; approval reads that server-owned
intent and repeats the physical checks without requiring a positive coverage
gain. The UI displays signed coverage change and warns if any selected step does
not exceed the useful-gain threshold, even when the total plan has positive gain.
Approval remains an explicit human action. Existing API requests default to
`OPTIMIZE_COVERAGE`, and old saved recommendations keep that default.

New isolated regression coverage:
`tests/unit/test_requested_quantity.py` and
`dashboard/tests/RequestedQuantity.test.tsx`.

## Startup acknowledgements are observations of an existing commitment

A later live batch exposed the same revision race during command delivery,
accepted acknowledgement, and first movement. These normal transitions describe
an EV whose destination, capacity hold and exclusive claim are already admitted;
they must not repeatedly invalidate the next batch child.

The global revision now stays stable across PENDING/SENT/ACKNOWLEDGED/IN_PROGRESS
only when the record holds a confirmed claim and command, has no failure/release
reason and no stop intent. The observations and receipts still update and persist.
Entry to UNKNOWN, recovery from UNKNOWN, arrival, failure, new admission, claim
and parking changes still invalidate assessments. The revision checks and bounded
retry remain in place.

Isolated regression tests interleave actual sibling delivery, acknowledgement
and first movement with each batch revalidation, through both event and telemetry
reconciliation paths. Existing arrival/failure and genuine revision churn tests
remain blocking checks.

Validation for the startup race: 78 planning/quantity regressions and 36
lifecycle, failure, resolution, persistence and Dispatch API checks passed.
Both event-driven and Fleet-reconciliation startup tests failed before the fix
and passed afterward. A separate read-only review found no actionable issue.
Python editor/Pyright diagnostics remain unavailable. This fix is local only;
no deployment, live dispatch, recovery operation or commit was performed.


## Command polling and durable journal load

A subsequent seven-EV batch completed its server response in 44.8 seconds.
Its command polls reached 4.9 seconds while simulator HTTP calls use a two-second
timeout. Rejected EV logs showed checkpoint ReadTimeout pauses; runtime guards
then made them temporarily ineligible. Raising timeouts or ignoring runtime
eligibility would conceal that failure.

Unchanged command/status reads now compare only the observed dispatch records
with the last committed snapshot, under the existing commit barrier. They no
longer serialize unrelated history or commit another request's changes merely
because an idle EV polled. Changed observations and delivery receipts still
become durable before response; uncertain commits continue to block reads.
Dispatch encodes its frozen snapshots in a worker thread. Repeated observations
of the same lifecycle state/reason leave updated_at stable, while new telemetry,
progress events and genuine transitions still update it.

The new isolated regressions cover durability during delivery, failed concurrent
commits, unsaved reconciliation changes, stable polls, and genuine progress.
A disposable PostgreSQL check verifies 100 idle polls preserve 200 historical
journeys without increasing the journal revision and that a new Dispatch process
restores the records. Existing fleet data is never used for these tests.
