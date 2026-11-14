# Counterfactual Rebalancing under Delayed State (CRDS)

CRDS is the default policy for simulated demand rebalancing. The operator chooses a destination zone. CRDS decides whether relocating an eligible EV there would reduce unmet demand for usable vehicles across the modeled fleet, including the zone that gives up the car. Dispatch still requires explicit approval.

## How it differs from the weighted policy

The basic legacy selector (`ev-aware-v1`) ranks eligible EVs by distance utility (35%), current state of charge (20%), predicted arrival reserve (35%) and charging suitability (10%). The legacy facility path adds parking suitability, and fresh overlapping traffic evidence can replace the weighted score with a traffic cost. These are measures of trip suitability. CRDS instead compares shortage with and without a move in each declared future. It can prefer a farther EV from a zone with spare stock, or recommend waiting even when a trip is physically feasible.

| Mode | Rebalancing behavior |
|---|---|
| `CRDS_MODE=ACTIVE` (default) | Evaluate with CRDS. An unexpired `MOVE` can proceed to operator approval and revalidation. |
| `CRDS_MODE=ADVISORY` | Evaluate with CRDS and expose the evidence. Dispatch refuses approval of these recommendations. |
| `CRDS_MODE=DISABLED` | Use the legacy recommendation paths and disable the CRDS request queue. |

Explicit `CHARGE` batch requests use the existing facility-aware charging path in every mode. Dispatch starts from a verified OneMap road route for both policies. A configured OSRM alternate can pass legacy approval. CRDS requires the final route to be OneMap for benefit revalidation.

## Build the baseline and verify candidates

1. Rebalancing collects Fleet telemetry, demand targets for all zones, Facilities data and Dispatch planning state. It uses the latest available traffic snapshot. Reads are bounded and versioned snapshots remain independent across services. Fleet and Dispatch planning snapshots have a default maximum age of 5 seconds. The default telemetry freshness limit is 15 seconds. Dynamic demand must be younger than `DEMAND_MAX_AGE_SECONDS` (300 seconds by default). Manually set targets remain valid while demand generation is paused. Other observations still follow their age checks.
2. Usable stock comes from fresh eligible Fleet observations and zone placement, including confirmed parking where applicable. Active rentals and claims are excluded from current usable stock. CRDS can project future supply from eligible expected rental returns and from consistent, already committed inbound journeys. Unresolved relocation evidence can exclude an affected EV or facility rather than count uncertain supply.
3. CRDS considers fresh, open destination facilities with compatible space. It picks the nearest passing facility by approximate distance per EV, with facility ID as a tie-break, then requests a road estimate from Dispatch. It does not search every EV-to-facility road route or automatically retry a second facility when that road check fails. An EV in a zone with positive nominal demand must leave at least one usable EV behind. Where nominal demand is zero, the candidate may be the last EV. An EV outside all modeled zones contributes no donor-zone stock.
4. Dispatch verifies the candidate's identity, position and OneMap geometry. Rebalancing screens that geometry for known road blocks and checks road-distance energy and predicted arrival charge. The default energy margin is 15%, and the minimum arrival reserve is 20%. Below 35% predicted arrival charge, a compatible charger is required and the EV only becomes usable after modeled charging to 80%. Missing traffic detail can leave the base road estimate in use. Missing required routing evidence can prevent a decision.

See [input collection and isolation](../src/ev_fleet/rebalancing/crds_inputs.py), [candidate planning](../src/ev_fleet/rebalancing/crds_service.py) and [OneMap routing](onemap-routing.md).

## Evaluate the same futures with and without the move

The default horizon is 30 wall-clock minutes in one-minute slots. Route and charge durations are converted using the configured shared simulation time scale (default 10). The scenario model starts with six profiles.

| Future | Change from nominal |
|---|---|
| `nominal` | Current desired stock, expected usable rental returns and verified inbound projections. |
| `demand_shift` | Lower the requested zone's target by one and raise each other zone's target by one, clamping targets at zero. |
| `late_returns` | Delay expected rental returns by 10 minutes. |
| `unavailable_returns` | Omit expected rental returns. |
| `delayed_inbound` | Delay committed inbound journeys and the candidate journey by 5 minutes. |
| `combined` | Apply the demand shift and inbound delay, and omit rental returns. |

These values are defaults. With age expansion enabled, the oldest still-valid dynamic demand observation adds one more demand-shift profile per 30 seconds of age, up to three. Each successive profile applies a larger shift. Fixed manual targets are exempt from the **age** profiles. The six base profiles still apply. Desired stock stays constant within each modeled future, while projected usable stock changes as vehicles become available.

A candidate leaves its origin at the start of the horizon and joins destination stock at the first slot boundary at or after travel and any charging. It contributes no destination stock if it becomes usable at or beyond the end of the horizon. For each zone and slot, the service computes:

```text
raw shortage     = max(0, desired stock âˆ’ usable stock)
priority target  = ceil(0.20 Ã— desired stock)
weighted shortage = raw shortage + max(0, priority target âˆ’ usable stock)
```

The total multiplies each slot's shortage by the slot length, then sums over all zones and slots. One EV missing for ten minutes is ten raw vehicle-minutes. Minutes below the priority target also count toward the extra term. The total is a stock-coverage metric.

```text
gain in a future = weighted shortage without the move âˆ’ weighted shortage with the move
candidate gain   = minimum of that gain across the declared futures
```

CRDS picks the largest candidate gain, then prefers lower worst-case energy use, then vehicle ID and facility ID. The default minimum useful gain is 1 weighted vehicle-minute. A candidate must **exceed** it. The minimum is calculated from paired comparisons within each future, preserving both the destination benefit and the donor cost. See [scenario construction](../src/ev_fleet/rebalancing/crds_futures.py), [scoring](../src/ev_fleet/rebalancing/crds.py) and [configuration defaults](../src/ev_fleet/common/settings.py).

### Worked example

Suppose CBD wants five usable EVs and has one. The near donor has three and wants two. The farther donor has four and wants two. Both candidates are eligible, no charging is needed, and there are no existing returns or inbound trips. The nearer EV has the higher basic weighted score.

| Future | Near donor | Farther donor |
|---|---:|---:|
| Nominal. Cars become usable after 5 and 10 minutes respectively. | +25 | +20 |
| Combined. Donor targets rise to three, CBD's falls to four, and trips take 5 minutes longer. | 20 saved in CBD âˆ’ 30 added at origin = **âˆ’10** | 15 saved in CBD âˆ’ 0 added at origin = **+15** |

Under these illustrative inputs, the combined case gives the minimum for both candidates, and the priority term contributes zero. CRDS selects the farther EV. If only the near EV were available and evaluation completed, CRDS would return `HOLD`.

## Decisions, batches and approval

- **MOVE** means an assessed candidate clears the gain threshold. A result can carry partial route coverage when a verified candidate clears the threshold and another candidate's route evidence was unavailable.
- **HOLD** means the assessment found no eligible move (`NO_ELIGIBLE_RELOCATIONS`) or no move that clears the useful-gain threshold (`NO_USEFUL_MOVE`). **DEFER** means necessary evidence or computation was incomplete, such as stale inputs, unavailable routes or a decision deadline. Upstream reads can also fail as HTTP errors. Inspect the reason and per-EV evidence before retrying.
- **Batches** accept a requested count of 1â€“20. The planner applies each selected move to every future, reduces the simulated capacity available for subsequent assignments, and recalculates marginal gains. It can stop early. This is a greedy search. The reported whole-batch gain compares the initial and final plans directly within each future, then takes the minimum. Individual step minima can come from different futures, so summing them does not give the reported batch gain.
- **Approval** is a separate Dispatch operation. Recommendations expire after 120 seconds by default. Dispatch serializes admission, rechecks the exact EV and facility against current observations and the final route, and asks Rebalancing to re-evaluate benefit against the current planning revision. Only after validation does acquisition of the parking hold and Fleet claim proceed. A prior recommendation can fail if demand, capacity, route or existing commitments changed. Approval outcomes and relocation progress have separate evidence.

`crds` metadata records the decision, reason, parameters, scenario model version/count, source references and assessment evidence. A `MOVE` can include the limiting future, weighted gain and per-zone shortage changes. Positive per-zone changes mean added shortage. Negative changes mean relief. The per-zone changes are raw vehicle-minutes, so they need not sum to the weighted gain. The ETA range describes modeled travel arrival. The stock calculation also accounts for charging delay.

## Service execution and evidence

`CRDS_EXECUTION_MODE=LOCAL` runs the scoring in the Rebalancing process. Optional `HTTP` execution partitions futures across configured workers, verifies reply identity and coverage, then combines their minima. An incomplete set of worker results defers the decision. A default 30-second planning budget covers collection, route evidence and scoring. Worker mode changes execution, not the objective.

The dashboard uses the single-writer recommendation-request queue for rebalancing when `DATABASE_URL` is configured and CRDS is enabled. Queue capacity defaults to 20 pending or evaluating requests. A queued request that reaches the default 300-second wait limit before evaluation defers. Queued requests are separate from approval and report statuses such as `READY`, `HOLD`, `DEFERRED`, `ADVISORY` and `EXPIRED`. Synchronous single and batch recommendation endpoints are also available. See [API contracts](api-contracts.md) and the [operator workflow](operator-dashboard.md).

The futures are synthetic stress cases rather than probability-calibrated forecasts. The gain describes only those cases, within the horizon and candidate pool. The [archived selector evaluation](strategy-evaluation-verification.md) compares legacy energy-aware ranking with nearest eligible EV. Its numbers apply to those two policies. It provides no head-to-head CRDS measurement.

`tests/unit/test_crds_gain.py` checks that the fast prefix-count scorer in `evaluate_moves` gives the same worst-case gain, worst future and per-zone shortage change as recomputing `shortage_by_zone` with and without the move. It runs 300 seeded random cases covering out-of-zone, same-zone and other-zone origins, part-slot arrivals, charging delays and arrivals past the horizon, plus the `HOLD` threshold boundary.

The dashboard's **Planning mode** selector chooses between two modes for rebalancing requests. **Optimise coverage** (`OPTIMIZE_COVERAGE`, the default) lets CRDS return `HOLD` and keeps donor-zone protection. **Fulfil exact quantity** (`FULFIL_REQUEST`) is the dispatcher override. Operator-requested quantity plans use `FULFIL_REQUEST`: physically feasible relocations may proceed even when the modeled coverage change is negative. Coverage trade-offs remain visible for human approval; routing, reserve, capacity, ownership and freshness checks still apply. Queued recommendation requests carry this planning mode and support up to 50 selected EVs. This recommendation queue is separate from the [durable dispatch queue](paced-bulk-relocation.md), which paces approved starts and automatically retries transient failures. Multi-plan approval uses the same global dispatch capacity limit.
