# Multi-EV relocation and mock parking allocation

This capability builds on the parking/charging catalogue and rental lifecycle. It adds no customer-driving simulator.

## Overview

The existing legacy single-EV recommendation and dispatch APIs remain intact.
A new parking-aware path works for **1 to 20 EVs** per batch.

1. The dispatcher selects a target demand zone and requested EV count.
2. Rebalancing reads Fleet, Demand, and current MOCK Facilities snapshots.
3. For each eligible EV and open mock parking location in that zone, it
   recalculates the existing energy-aware score and predicted arrival SOC
   using that location's coordinates. An EV predicted to arrive below **35%**
   SOC needs an available mock charging bay.
4. Pair utility uses **85% of the existing energy-aware score** plus
   **15% of the facility's available-parking ratio**. A deterministic
   **global-greedy heuristic** selects the highest-scoring remaining EV/facility
   combinations while accounting for facility capacity and charger allocations.
   This is NOT an exact or globally proven optimal matching algorithm.
5. The preview contains separate EV–facility destinations, scores, SOC and
   distances, plus explicit unfulfilled count. No mutation happens on preview.
6. Operator approval creates one child dispatch per proposed assignment,
   each with its own immutable identity. The Facilities Service atomically
   reserves a MOCK space before Fleet's atomic EV claim. The final eligibility,
   position, SOC, charging and available-capacity checks are revalidated.
7. Definitively rejected claims release known parking reservations; ambiguous
   outcomes retain holds pending reconciliation. Verified arrival turns a
   reservation into occupied capacity, which does not become free automatically.

## APIs

- `POST /api/v1/batch-recommendations` in Rebalancing:
  `{"target_zone_id": "cbd", "quantity": 3}`.
- `GET /api/v1/batch-recommendations/{batch_id}` for the matching snapshot.
- `POST /api/v1/batches` in Dispatch:
  `{"request_id": "<UUID>", "batch_recommendation_id": "<UUID>"}`.
  Returns per-child outcomes; may be PARTIAL if some EVs become busy.
- `GET /api/v1/batches/{request_id}` for a saved batch receipt.
- `GET /api/v1/facilities` includes remaining mock capacities.
- `POST /api/v1/facilities/{facility_id}/holds`,
  `GET /api/v1/holds/{dispatch_id}`,
  `POST /api/v1/holds/{dispatch_id}/complete`, and
  `POST /api/v1/holds/{dispatch_id}/release` (internal coordinator endpoints).

The old `POST /api/v1/recommendations` and
`POST /api/v1/dispatches` remain available for zone-only relocations.

## Important limitations

- All facilities and available-space figures are **fictional**.
- Simulated occupancy/parking reservations are held within **one**
  Facilities process. This prevents concurrent overbooking **within
  that process**; it does not provide cross-replica transaction safety.
- Fleet, Dispatch, recommendation, rental and parking state are in memory
  and lost on process/Pod restart. No multi-replica state owners.
- No real road routing, customer parking rights, user authentication,
  durable outbox, cross-service two-phase commit or public bay reservation.
- Approval can be partial, and individual dispatch statuses must be monitored.
  Lost HTTP responses require retrying the **same** batch request ID.
- An occupied simulated space remains occupied; the future rental departure
  lifecycle must explicitly vacate it. Do not equate the current mock
  `parking_available` values to actual, guaranteed public bay availability.
- Uncertain parking hold delivery or reconciliation requires additional
  hardening with PostgreSQL, durable event records, and periodic recovery.
- The existing strategy-evaluation outputs do not validate this capability's
  new greedy pair-selection weights; a separate evaluation is required.

## Local verification

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_batch_dispatch.py -q
.\.venv\Scripts\python.exe -m pytest -q
Push-Location dashboard
npm.cmd test -- --run
npm.cmd run build
Pop-Location
docker compose --profile relocation config --quiet
docker compose --profile relocation up --build -d --wait `
  fleet demand rebalancing dispatch facilities ev-moving-01 ev-moving-02 dashboard
```

Use `http://127.0.0.1:8080`, select CBD, and choose the EV count in the
**Relocation request** panel below the map. Request the recommendation, inspect
every proposed assignment, select **Review dispatch**, and approve the batch
explicitly. A quantity of one keeps the single-EV approval path.

There is no evidence here of successful container execution; run these checks
on a Docker-capable machine before merging.
