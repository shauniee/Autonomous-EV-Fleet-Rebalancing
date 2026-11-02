# Demand and heat-map verification

## Results

- Full suite: **108 passed, 1 warning**, in 37.62 seconds on Python 3.10.1.
- The existing 63 service and dispatch tests are unchanged; 45 cases were added.
- `python -m pip check`: no broken requirements.
- `docker compose --profile relocation config --quiet`: passed, with a local Docker
  config access warning.
- `git diff --check`: passed, with normal Windows LF/CRLF conversion notices.
- No Fleet, Rebalancing or Dispatch implementation was changed.

Raw test report: [demand-heatmap-tests.xml](evidence/demand-heatmap-tests.xml).

The warning is the pre-existing Starlette TestClient deprecation for its HTTPX
adapter. An initial full regression attempt hit a read timeout in the existing
process-level relocation test. The unchanged test passed on an isolated rerun,
and the final complete 108-test run passed. No timeout or dispatch safeguard was
weakened to obtain that result.

## Behaviour verified

- Current shortage/surplus, zero-demand handling, equality and classification boundaries.
- Projected supply counts confirmed inbound motion without hiding current shortage.
- Initial coordinate fallback, radius edge, tie resolution and unassigned vehicles.
- Claimed, stale/offline and below-reserve EVs excluded from available capacity.
- Pending, arrived-pending, failed, mismatched and old-Fleet-epoch claims do not
  masquerade as confirmed inbound supply; no supply double counting.
- Seeded bounded sequences repeat with the same inputs and change across ticks.
- Disabled simulation leaves state untouched; manual holds expire at their boundary.
- Concurrent manual/simulator writes preserve versions; a simulation tick presents
  one consistent set of zone versions.
- Missing/stale demand stays distinguishable from zero demand.
- Fleet timeout, unavailable service, malformed/duplicate-ID responses and stale
  snapshots produce partial responses with null capacity and UNKNOWN severity.
- A slow Fleet request does not hold Demand's write lock or mix demand versions
  inside one heat-map snapshot.
- All six zones are represented; the legacy raw-demand hotspot endpoint is unchanged.
- The enabled background simulator advances and is cancelled at service shutdown.

## Real HTTP relocation integration

The added process-level test launches four independent Uvicorn services and two
separate moving EV processes, using loopback HTTP and temporary ports. It verifies:

1. Initial seeded-position EVs count as available capacity before their first arrival.
2. CBD demand is manually set to four vehicles.
3. A recommendation is explicitly approved through the existing dispatch API.
4. The relocating EV counts as one inbound vehicle: current shortage four,
   projected shortage three.
5. Demand changes to seven while relocation is active; the command remains unchanged.
6. After confirmed arrival, CBD has one available EV, no inbound EV for that journey,
   and current shortage six.

Additional HTTP tests confirm that demand changes before approval still produce
TARGET_CHANGED, and concurrent demand/telemetry updates do not alter an active command.
The process fixtures stop the services and EV processes they start.

## Docker runtime status

The successful dispatch and relocation Docker run remains historical evidence for
that capability. During this demand and heat-map run, Docker's Linux engine pipe was
unavailable outside the sandbox (`dockerDesktopLinuxEngine`: file not found). Therefore
the demand image build, container startup, service-network execution and enabled simulation
inside containers remain **unverified**. Compose configuration validation and local
HTTP execution are reported separately, not as container execution.

When Docker is available, enable DEMAND_SIMULATION_ENABLED and use an isolated
Compose project with unused host ports. Follow [the demand and heat-map guide](demand-heatmap.md)
to check changing demand, manual holds, heat-map counts and an approved relocation.
Test Fleet unavailability only against that isolated project's Fleet service; the
Demand API should remain ready and return partial/unknown capacity while it is down.

## Scope limits

There is no frontend, ML, external map API, persistence or new service. Zone circles
are synthetic Singapore catchments. Demand is desired capacity, not a booking rate.
Projected supply is conditional, with no arrival-time prediction. Determinism
requires the same ordered inputs; snapshots across Demand and Fleet are not a
distributed transaction. Existing dispatch approval/version checks remain intact.
