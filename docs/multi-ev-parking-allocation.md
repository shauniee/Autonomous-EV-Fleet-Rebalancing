# Multi-EV relocation and simulated parking allocation

For demand rebalancing, [CRDS](crds.md) is the default planner for requests
of 1–20 EVs. It chooses a compatible destination per EV, verifies road
evidence, and updates modeled supply and remaining facility capacity after
each proposed move. The preview can contain fewer assignments than requested.
Rebalancing and explicit charging both require human approval and fresh
checks before Dispatch acquires resources.

## Legacy facility-aware ranking

This section describes `CRDS_MODE=DISABLED` rebalancing and the separate
`CHARGE` request path. Facilities can be mock or backed by LTA observations.

1. The dispatcher selects a target demand zone and requested EV count.
2. Rebalancing reads Fleet, Demand and current Facilities snapshots.
3. For each eligible EV and open parking location in that zone, it
   recalculates the existing energy-aware score and predicted arrival SOC
   using that location's coordinates. An EV predicted to arrive below **35%**
   SOC needs an available charging bay. Explicit `CHARGE` requests require
   charging capacity even above that arrival threshold.
4. Pair utility uses **85% of the existing energy-aware score** plus
   **15% of the facility's available-parking ratio**. A deterministic
   **global-greedy heuristic** selects the highest-scoring remaining EV/facility
   combinations while accounting for facility capacity and charger allocations.
   Unknown total parking capacity uses a neutral utility. Fresh overlapping
   traffic can replace the score with a [traffic cost](traffic-aware-routing.md).
5. The preview contains separate EV–facility destinations, scores, SOC and
   distances, plus explicit unfulfilled count. No mutation happens on preview.
6. Operator approval creates one child dispatch per proposed assignment,
   each with its own immutable identity. The Facilities Service atomically
   reserves simulated space before Fleet's atomic EV claim. The final eligibility,
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
- `GET /api/v1/facilities` includes remaining capacity and its source.
- `POST /api/v1/facilities/{facility_id}/holds`,
  `GET /api/v1/holds/{dispatch_id}`,
  `POST /api/v1/holds/{dispatch_id}/complete`, and
  `POST /api/v1/holds/{dispatch_id}/release` (internal coordinator endpoints).

`POST /api/v1/recommendations` provides a synchronous single recommendation.
In CRDS mode it also selects a specific facility. The dashboard uses the
[recommendation queue](api-contracts.md#rebalancing-8003) for rebalancing when
enabled, including quantity one.

## Important limitations

- Mock facilities and their capacities are fictional. LTA observations can
  describe actual facilities, but holds are allocations in the simulation.
- Run the state-owning services with a single writer. Configured PostgreSQL
  snapshots can preserve state across restart. Approval and reconciliation
  still span independent service owners.
- [OneMap](onemap-routing.md) verifies simulated journeys. Parking holds
  confer no real parking or charging rights.
- Approval can be partial, and individual dispatch statuses must be monitored.
  Lost HTTP responses require retrying the **same** batch request ID.
- A simulated allocation remains occupied until the owning lifecycle releases it.
  Consult the [Dispatch lifecycle](dispatch-relocation.md) for uncertain outcomes.
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

For a road-following demo, configure [OneMap](onemap-routing.md) and set valid
demand targets in every zone. Use `http://127.0.0.1:8080`, select CBD, and
choose the EV count. Review the proposed assignments and explicitly approve
the plan when available. Use the [operator guide](operator-dashboard.md) to
inspect approval and relocation outcomes.
