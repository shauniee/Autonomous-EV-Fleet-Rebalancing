# Bulk relocation checkpoint storage

Fleet and Dispatch remain single-writer services. Frequent updates now write
individual vehicle/dispatch records in `snapshot_records`, plus compact metadata
in `service_snapshots`. Metadata, changed records, deletions and telemetry history
commit in one atomic PostgreSQL statement. A response confirms commit; ambiguous
writes fence authority until durable state is reloaded. Fleet retains its existing
supervised uncertainty exit; Dispatch retains its existing fail-closed policy.

The last confirmed Fleet read model shares unchanged frozen records. Single-EV
status reads can return that committed view while a writer runs. Claim receipts,
claim mutations and ownership operations retain their existing barriers. Dispatch
observations collect overlapping updates for 2 ms and share one durable frame.
Every caller, including duplicate ACK/event requests, waits for commit. They share
frozen metadata only for existing rows in the same planning epoch; new records and lifecycle/ownership changes use the full-frame path.

`sniffio` is an explicit runtime dependency. HTTPcore 1.0.9 otherwise retries a
failed optional import on request synchronization, causing repeated filesystem
lookups; the 50-client profile spent most of its time there on a Windows bind
mount. See the upstream implementation:
https://github.com/encode/httpcore/blob/1.0.9/httpcore/_synchronization.py

Non-charging Fleet checkpoints and lease renewals use a bounded group writer: at most 1,024 queued
requests, 64 different vehicles per transaction and a 2 ms collection window.
Each item is validated under the Fleet lock. All accepted vehicle records and
history samples commit together before publication or receipts. Invalid items
are isolated; same-vehicle requests preserve order. A disconnected caller cannot
cancel the shared transaction, while writer cancellation fences the service.
Charging checkpoints retain their existing external-evidence flow. Lease acquisition
and coordinated restore also retain their direct paths. Exact renewal retries return
the original expiry; they do not extend ownership.

## Isolated verification

The opt-in PostgreSQL tests require a disposable loopback `twin_history_test`
database. They cover promotion, deletion/order, corrupt manifests, namespace
isolation, rollback of metadata/rows/history, lost response after commit,
cancellation after commit, and fail-closed old readers.

`tests/integration/test_bulk_checkpoint_load.py` compares a 50-writer burst.
`tests/integration/test_paced_bulk_load.py` runs 50 actual guarded simulators across
five driver processes against separate Fleet, Demand, Facilities, Rebalancing and
Dispatch HTTP processes. One explicit operator approval queues all 50 feasible
assignments; the default four-slot admission policy limits active work.

The fixture uses deterministic offline road geometry and synthetic parking
capacity, with 30x simulated travel. Two-second simulator request deadlines and
30-second ownership leases are unchanged. All 50 completed; two transient retries
recovered automatically with no manual retry or RECOVERY_WAIT. This replaces the
superseded simultaneous-50 acceptance expectation after the user chose pacing.
See [paced bulk relocation](paced-bulk-relocation.md) for results and limits.

## Upgrade

This changes the durable storage format. Prepare an operationally safe Fleet and
Dispatch rollout, with no new approval submissions during the brief update. Keep
simulator controllers, their arguments and replica counts unchanged. Back up the
existing authoritative state before an authorized upgrade.

Each service loads its legacy state and atomically promotes every record before
readiness. Instance IDs, command IDs, ownership generations, physical checkpoints,
request receipts, parking/charging state and history are retained. A failed
promotion cannot publish a partially upgraded store. Namespace-by-namespace
promotion supports updating Dispatch and Fleet separately.

After startup, confirm service readiness, active command/claim identities and
positions/SOC, then inspect command/checkpoint latency. A journey whose lease
expired during maintenance requires the existing assessed operator recovery
workflow; a restart must never silently clear its claim.

## Rollback boundary

An older image cannot read the new format and fails startup rather than using a
stale legacy snapshot. Never roll back only the image after promotion. Under a
separately authorized maintenance window, quiesce ALL writers and export the
LATEST committed state using RecordSnapshotRepository.load, then write that full
state using SnapshotRepository.save before starting the compatible old image.
Preserve current IDs, receipts, checkpoints and physical state. A pre-upgrade
backup must not rewind work that progressed after it was taken. Rollback is a
maintenance operation, not an automatic fallback. The existing database was unchanged during implementation and isolated tests.
The later, explicitly authorized local rollout promoted both namespaces and
verified their persisted outcomes. Once paced jobs have been created,
older images also lack that protocol: see the additional downgrade constraints in
[the local upgrade proposal](paced-local-upgrade.md).
