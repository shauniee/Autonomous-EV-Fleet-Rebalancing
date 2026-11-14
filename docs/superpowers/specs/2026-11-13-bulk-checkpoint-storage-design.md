# Bulk checkpoint storage

Goal: support 50 concurrent relocating EVs without the whole-fleet/journal write
amplification that caused checkpoint deadlines, RETRYING and rejected admission.
Human approval, two-second simulator deadlines, ownership generations, battery,
route, freshness and cable checks retain their meaning. No existing live journey
is restarted, cancelled or approved as a test.

## Approaches and decision

Resource increases relieved throttling but retained O(fleet/journal size) work
per checkpoint. Batching would reduce commits but introduces a new admission
queue, cancellation and per-item error semantics. Use per-record durable storage
instead: one shared PostgreSQL snapshot_records table keyed by namespace and
record_key, with compact service metadata in service_snapshots. Fleet partitions
vehicles; Dispatch partitions records. This is an implementation choice made to
preserve current single-writer state machines while removing repeated unrelated
serialization. It is not cross-replica coordination.

Each transaction writes metadata, changed records and (for Fleet) telemetry
history atomically. Read models publish only after commit. Fleet reuses frozen
unchanged read records; Dispatch freezes only changed records. Uncertain or
cancelled writes fence the repository and supervised process exactly as today.

Legacy snapshots load unchanged. The first successful save atomically promotes
the namespace to partitioned storage, copying all legacy records. The format
marker makes an old reader fail closed; downgrade requires an explicit tested
export/rollback, never starting an old image against stale snapshot data.
Record-key ordering is preserved in the metadata. Restore rejects missing,
extra or malformed record sets. No schema/data change to the existing database
occurs until a separately reviewed local rollout is authorized.

## Verification

Tests must prove dirty-record persistence, deleted records, migration/restore,
metadata-record-history atomicity, response loss/cancellation fencing, and frozen
read visibility during another blocked write. Use synthetic data only. A
repeatable disposable PostgreSQL exercise drives 50 concurrent valid checkpoints
and 50 active dispatch updates with substantial route/history payloads. Report
p50/p95/p99/max and every timeout/error under documented resource limits. Passing
that scenario is not a claim of capacity on every machine or with stalled
storage. No deadline or eligibility guard is relaxed to make the exercise pass.


## Measured extension: bounded checkpoint group commits

The normalized 50-writer burst is below one second, but a sustained real-HTTP
50-EV workload reaches the high thirties before checkpoint queues approach two
seconds. A separate durable transaction per checkpoint is still a bottleneck.
Add a bounded (1,024 pending), short-window (2 ms), at-most-64-vehicle checkpoint
writer for durable RecordSnapshotRepository-backed Fleet stores. Keep at most
one request per vehicle in each group. Charging evidence paths retain their
existing nonce-bound flow; if that requirement changes while queued, reject the
request for reassessment rather than bypassing it.

The writer acquires the existing Fleet lock, validates/stages each independent
request without publishing, writes the accepted rows plus ALL history samples
in one atomic statement, then publishes the full committed read frame and
releases receipts. Invalid items do not poison valid ones. Request cancellation
must not cancel a shared group; the writer retains authority and same-ID retries
receive the committed receipt. Cancellation or uncertainty of the writer itself
fences the service. Graceful close drains pending work. Ephemeral stores retain
the direct path because they have no disk-commit cost.


## Dispatch observation groups

Known-record observations collect for 2 ms before the existing persistence lock.
One caller commits overlapping dirty rows; every caller retains its own observed
records and checks them against the confirmed frame under that lock. Idle reads
do not drain unrelated pending rows. Duplicate ACK/event responses cross the same
barrier. Changed planning epochs and new records still persist a coherent complete
frame; steady progress reuses committed metadata to exclude pending admissions.
A failed commit fences every waiter rather than returning an optimistic receipt.


## Lease renewal groups

The phase trace found checkpoint groups waiting behind unbatched lease renewals
on the Fleet lock: group p99 was 3,389 ms, versus 665 ms for an individual save.
The same bounded writer now accepts durable RENEW requests. ACQUIRE, RESTORE and
RESTORE_IDLE keep their direct/coordinated paths. Extracted session staging retains
all owner/generation/revision/control/claim checks, including exact retry receipt
and original expiry semantics. At most one request of either kind per vehicle
is staged in a group; mixed same-vehicle operations preserve FIFO ordering.
All accepted rows/history commit together before owner or checkpoint publication.
Tests prove 50 renewals use one transaction, blocked receipts, cancellation/replay,
ambiguous-response fencing, expired owners and mixed renewal/checkpoint ordering.
