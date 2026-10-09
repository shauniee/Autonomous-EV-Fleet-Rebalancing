# Implementation roadmap

1. **Backend foundation (implemented):** contracts, Fleet, Demand, Rebalancing, stationary
   simulators, health, unit/API/real HTTP tests and Compose definition. Exit evidence:
   nodes register independently, fresh telemetry ranks correctly, stale nodes are
   excluded, a farther energy-suitable EV can win, tests pass, container smoke test
   succeeds or is explicitly pending on environment availability.
2. **Approved relocation (implemented):** Dispatch service, atomic Fleet claims, idempotent commands,
   polling/acknowledgement, simulated movement and energy consumption, arrival,
   disconnection and ambiguous-delivery reconciliation. No real-road driving.
   See [Phase 2 guide](phase2-dispatch.md) and [verification](phase2-verification.md).
3. **Phase 3A dynamic demand and heat-map backend (current):** seeded optional demand
   simulation, manual holds, Fleet API composition, current/projected shortages,
   semantic severity and partial-data handling. See [Phase 3A guide](phase3a-demand-heatmap.md).
   **Phase 3B dashboard (later):** Singapore demand heat map, markers, selection,
   recommendation explanations, explicit approval and relocation progress.
4. **Durability and deployment:** persistent state, durable command recovery,
   dispatcher/device authentication, then Kubernetes. No multiple in-memory replicas.
5. **Phase 4 experimental evaluation:** nearest eligible baseline, reproducible paired
   scenarios, isolated trial state, raw JSON/CSV, confidence intervals, graphs and analysis
   of distance, SOC, arrivals, demand coverage and failure handling. Product policy weights
   remain fixed and the application default remains energy-aware.

Six-person allocation: Fleet/concurrency; Demand/charging; selection/metrics;
simulator/telemetry; HTTP integration/failures and later Dispatch; deployment/logging/
documentation. Agree contracts jointly and pair-review service boundaries.

Phase 1 deliberately excludes frontend, animation, dispatch execution, movement,
ML prediction, maintenance prediction, reservation handling, real charger APIs,
database, Kubernetes manifests and experiment framework.

Major risks: ephemeral state, clock skew, simplified geography/energy, correlated
score factors, stale charger assumptions, simultaneous recommendations choosing the
same EV, unbounded long-running fleet/session identity history, and later cross-service
command ambiguity. Keep demonstration populations finite; solve durable ownership
before claiming production recovery or horizontal scaling.
