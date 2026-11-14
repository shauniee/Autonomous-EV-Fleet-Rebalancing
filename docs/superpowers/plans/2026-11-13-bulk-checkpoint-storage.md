# Bulk checkpoint storage implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove fleet-size/journal-size serialization from individual durable updates and validate a 50-EV load.
**Architecture:** A partitioned SnapshotRepository subclass preserves the existing transaction/fault policy while writing per-record rows; immutable read caches share unchanged records.
**Tech Stack:** Python, FastAPI, asyncpg, PostgreSQL, pytest, Docker.
**Spec:** docs/superpowers/specs/2026-11-13-bulk-checkpoint-storage-design.md

## Constraints

Keep all admission, ownership, charging and durability guards. Existing uncommitted
work stays intact. Do not stage, commit, push, migrate the existing database or
roll active live services during implementation. Tests use disposable resources.

## Tasks

- [x] 1. Add an isolated synthetic 50-record baseline/profiling fixture; capture existing checkpoint and dispatch write/copy costs. Confirm the unchanged-record regressions fail on the existing implementation.
- [x] 2. Implement common/record_persistence.py: partitioned storage, atomic legacy promotion, exact ordered restore, changed/deleted rows, atomic history and permanent uncertain-write fencing. Unit tests exercise real transaction boundaries; disposable PostgreSQL verifies durable behavior.
- [x] 3. Integrate Fleet: default repository partitions vehicles; persist computes changed keys from the committed read model; SimulatorRecovery.commit writes its one staged record and publishes only that record after successful commit. Test isolation from later in-place mutations, cancellation, failed save, reads during blocked save, charging/recovery/ownership regressions.
- [x] 4. Integrate Dispatch: default repository partitions records; freeze only changed records while preserving the existing persistence lock/uncertainty barrier. Test event/receipt durability, unchanged history, metadata changes, concurrent failures, recovered store and approval regressions.
- [ ] 5. Run the full backend suite and disposable PostgreSQL checks, then the 50-EV checkpoint/progress exercise. Record resource limits and timing distributions. Repeat only after changes/failures. Obtain independent safety review and resolve findings.
- [ ] 6. Prepare the local upgrade/rollback instructions and concrete rollout result for approval. Existing active journeys and database format changes need an operationally safe authorized rollout; no live test dispatches.

## Review focus

- Partial commit / lost response must fence writes and never publish staged state.
- First promotion must copy every legacy record even when the dirty hint names one.
- Deletion and namespace separation must restore the exact record set and order.
- Reused frozen read records must never reference mutable live state.
- Large histories and 50 overlapping writers must be measured; do not extrapolate idle traffic to active bulk capacity.

## Ledger

Implementation is inline. Record storage replaces repeated full snapshot writes;
CPU/memory limits alone are not considered the fix. Rollout remains separate
because promotion changes the durable storage format.


### Implementation decisions and evidence

- Per-record writes use one data-modifying SQL statement, including optional
  history. PostgreSQL's implicit transaction and asyncpg ReadyForQuery response
  retain the commit barrier without multiple request round trips.
- Startup explicitly promotes legacy snapshots before lifespan readiness; two
  real PostgreSQL startup tests failed before this and passed after.
- Vehicle status reads use the last committed read model during writes. Claim
  receipts and control/ownership mutations retain their barriers.
- Dispatch steady observations reuse committed metadata and update only observed
  known records. New IDs or a changed planning epoch fall back to a coherent full
  frame. Tests prohibit scanning unrelated histories and leaking pending indexes.
- The first full backend run passed 579 tests; application changes afterward
  require a fresh full run. Ten storage safety checks and 44 targeted checks pass.
- Early in-process/cold-start load fixtures hit deadlines. Real HTTP probes exposed
  client overhead; cProfile found 45.6 of 56.6 seconds in filesystem scans from
  HTTPcore repeatedly importing absent sniffio. Declare sniffio 1.3.1 explicitly.
  Also warm the 50 clients before bulk admission, reuse the load driver's trust
  context, keep live backend logs on local container disk, and bound task cleanup.
  Multiple hung disposable drivers were stopped; final capacity runs are serial.
  No timeout, ownership or physical safety rule was relaxed.
- Source reviews found no blocking transaction/publication/ownership issue.
  Capacity and live rollout remain pending until measurements finish.


Ruling: add bounded checkpoint group commits before capacity sign-off. Per-record
storage removed serialization amplification, but sustained real HTTP work still
queues behind individual durable commits near 37 active EVs. Stage independent
requests under the existing Fleet lock, commit all rows/history atomically, and
publish only after success. Do not change timeouts, safety thresholds or charging
verification. Add blocked-save visibility, cancelled-caller retry, invalid-item
isolation and ambiguous group-commit tests before implementation.
