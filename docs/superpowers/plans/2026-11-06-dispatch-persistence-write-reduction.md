# Dispatch Persistence Write Reduction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop unchanged Dispatch polling and per-record reconciliation from rewriting the full PostgreSQL snapshot.

**Architecture:** Cache the frozen snapshot from the last successful save and compare the complete current state against it under the persistence lock. Persist changed reads before returning, let unchanged reads cross the same fail-closed barrier without writing, and save changed background records once per reconciliation cycle.

**Tech Stack:** Python, asyncio, pytest, FastAPI service layer

**Spec:** `docs/superpowers/specs/2026-11-06-dispatch-persistence-write-reduction-design.md`

## Global Constraints

- Preserve the snapshot schema and fail-closed persistence behavior.
- Preserve command delivery and mutation endpoint behavior.
- Do not access or mutate production data.

## Review Focus

- Empty command polls must not save.
- Unchanged dispatch reads must not save.
- A read that changes durable state must still save.
- A concurrent unchanged read must wait for an in-flight save and propagate its failure.
- Queued unchanged reads must not repeat a completed save.
- Multiple changed records in one reconciliation cycle must cause exactly one save.
- A cycle with no durable changes must not save.

---

### Task 1: Add regression coverage

**Files:**
- Modify: `tests/unit/test_persistence.py`

**Interfaces:**
- Consumes: `DispatchService.get`, `DispatchService.commands`, and one reconciliation-cycle operation.
- Produces: observable repository save counts for unchanged and changed state.

- [x] Add focused tests for unchanged reads, changed reads, concurrent durability, and batched reconciliation.
- [x] Run the focused tests and confirm they fail for the current unconditional-write behavior.

### Task 2: Reduce Dispatch snapshot writes

**Files:**
- Modify: `src/ev_fleet/dispatch/service.py`
- Test: `tests/unit/test_persistence.py`

**Interfaces:**
- Produces: `DispatchService.reconcile_once() -> None` and durable-state comparison used by polling paths.

- [x] Cache the exact frozen snapshot produced by each successful save.
- [x] Compare reads against that cache under the persistence lock so skipped writes retain the fail-closed barrier.
- [x] Keep per-record deep checkpoints inside record locks for reconciliation change detection.
- [x] Persist `get()` and `commands()` only when those fields change.
- [x] Add `reconcile_once()` that processes eligible records and persists once only when a record changed.
- [x] Make `reconcile_loop()` call `reconcile_once()` once per interval.
- [x] Run the focused tests until green.

### Task 3: Verify the repository

**Files:**
- No additional files.

**Interfaces:**
- Consumes: repository test scripts described in `docs/local-testing.md`.

- [x] Run `.\scripts\test-local.ps1`.
- [ ] Run `.\scripts\test-local.ps1 -Integration` because this is database and Kubernetes sensitive. **NOT VERIFIED:** the script stops because `act` is not installed.
- [x] Inspect the final diff and report exact verification results without committing unrelated user changes.
