# Operational Snapshot Stability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Keep the last valid operational snapshot visible for up to five minutes, then show an em dash.

**Architecture:** Reuse the existing resource timestamp as the freshness authority and apply one explicit five-minute limit in `FleetPulse`.

**Tech Stack:** React, TypeScript, Vitest, Testing Library

**Spec:** `docs/superpowers/specs/2026-11-06-operational-snapshot-stability-design.md`

## Global Constraints

- Never convert unavailable data into zero.
- Preserve immediate updates from valid observations.
- Preserve partial heat-map rejection.
- Do not overwrite unrelated uncommitted dashboard changes.

## Review Focus

- A transient error before five minutes retains the last count.
- Exactly five minutes old is unavailable.
- A newer valid observation replaces the retained value immediately.

### Task 1: Lock the five-minute contract with tests

**Files:**
- Modify: `dashboard/tests/FleetPulse.test.tsx`

**Interfaces:**
- Consumes: `FleetPulse` resource `data`, `error`, and `updatedAt` inputs.
- Produces: regression coverage for retention, expiry, and immediate replacement.

- [x] Add boundary and update tests.
- [x] Run `npm.cmd test -- FleetPulse.test.tsx` from `dashboard` and confirm the new retention case fails.

### Task 2: Apply the display freshness window

**Files:**
- Modify: `dashboard/src/components/FleetPulse.tsx`
- Test: `dashboard/tests/FleetPulse.test.tsx`

**Interfaces:**
- Produces: five-minute maximum age used by all four snapshot metrics.

- [x] Replace the ten-second default freshness with five minutes.
- [x] Run the focused component tests until green.

### Task 3: Verify locally

**Files:**
- No additional files.

**Interfaces:**
- Consumes: the repository's required local test script.

- [x] Run `.\scripts\test-local.ps1` after both approved changes are implemented.
