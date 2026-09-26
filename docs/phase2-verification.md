# Phase 2 verification

## Regression and new behaviour

The final local regression run completed with **63 passed, 1 warning** on Python
3.10.1. All 37 Phase 1 test functions/parameter cases remain unchanged. The warning
is the existing Starlette TestClient HTTPX-adapter deprecation, not a failure.
`python -m pip check` reports no broken requirements.

New tests establish:

- Recommendations have no claim or movement side effect; approval is separate.
- Conflicting approvals cannot acquire two claims for one EV.
- Request, command, acknowledgement, claim and release retries are idempotent.
- Approval rejects stale/offline vehicles, insufficient SOC, busy vehicles, expired
  recommendations and changed targets. Fleet repeats energy checks atomically.
- No-ACK and never-polled-command timeouts retain ownership.
- Lost claim, acknowledgement and release responses can be reconciled.
- Matching telemetry recovers a lost-ACK or temporarily disconnected journey.
- A new node cannot overwrite an unresolved journey with CLI defaults.
- Gradual movement consumes energy by distance, cannot overshoot, and does not
  restart on duplicate commands. Battery depletion holds the failed vehicle's claim.
- Physical simulation continues during injected network outages and reports arrival
  after reconnection.
- Four real Uvicorn processes and two separate moving-node processes complete
  an approved relocation, with multiple intermediate positions and return to availability.

One fast-telemetry outage test initially hit the intended vehicle-version conflict
during approval. Its setup now establishes the claim before starting frequent
telemetry; dedicated concurrency tests cover that race. No production safety check
was relaxed to make that test pass.

## Docker execution

Docker became reachable outside the sandbox during Phase 2 (Engine 29.8.0). Initial
default-port startup collided with existing services. Only the isolated project
`evfleet-phase2-check` was subsequently used on host ports 18001..18004; existing
services were left untouched.

The Python 3.12 images built successfully. Four backend containers passed readiness
checks and two separate moving-node containers registered through Compose DNS/networking.

The recovery smoke run explicitly approved `ev-moving-01` from Bishan to CBD and
temporarily disconnected **only that test container** from its Compose network.
Fleet became stale/offline; Dispatch recorded UNKNOWN with its claim retained.
After reconnection, arrival was validated and the claim released. Recorded evidence:

- 9.823527 simulated km travelled.
- SOC 90.000000% -> 87.052942%.
- Eight observed distinct positions.
- 171 raw API samples, including 78 UNKNOWN and 49 OFFLINE observations.
- Final SUCCEEDED dispatch, CBD zone assignment, no active claim, AVAILABLE EV.

Raw recovery evidence: [phase2-container-smoke.json](evidence/phase2-container-smoke.json).
This run preceded the final additive response-schema and never-polled timeout changes;
a final-image smoke result is recorded separately in
[phase2-container-final.json](evidence/phase2-container-final.json).

The final-image smoke also passed: ten observed distinct positions, the same
9.823527 km trip and 90% -> 87.052942% SOC, with ARRIVAL_PENDING followed by
SUCCEEDED and AVAILABLE. [Container health](evidence/phase2-container-health.json)
records four healthy backend services and two running simulator processes.
`git diff --check` and source parsing passed as well.

## Reproduction

```powershell
.\.venv\Scripts\python -m pytest -q
.\.venv\Scripts\python -m pip check
docker compose --profile relocation config --quiet
docker compose --profile relocation up --build -d --wait fleet demand rebalancing dispatch ev-moving-01 ev-moving-02
.\.venv\Scripts\python scripts/phase2_smoke.py --base-port 8001 --output docs/evidence/repeat-smoke.json
```

Use alternate port variables and a distinct Compose project name when other
services already occupy 8001..8004. The smoke script explicitly approves one
simulated relocation. For the recovery check, disconnect/reconnect only the moving
EV in an isolated test project; do not stop unrelated services or reset Fleet memory.

## Limits

Container execution is now verified for this four-service simulation. Durable crash
recovery, multi-replica shared state, authentication, real Singapore road routing,
real vehicles and Kubernetes remain outside this phase. State and deduplication are
in memory. FAILED/unknown ownership is not automatically force-released.
