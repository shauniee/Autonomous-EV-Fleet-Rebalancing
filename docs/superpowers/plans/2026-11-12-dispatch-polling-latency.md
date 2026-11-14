# Dispatch polling latency fix

Goal: reduce approval-to-command delay without changing operator approval, physical eligibility or durability semantics.

Evidence: the latest seven-EV batch took 44.8 seconds; command polls reached 4.9 seconds, exceeding the simulator's two-second HTTP timeout. Dispatch stores roughly 945 KB of historical state. Unchanged polls currently compare/save the whole journal, including unrelated mutations, and Dispatch snapshot JSON is encoded on the request loop.

- [x] Prove idle/global-scan and encoding failures with isolated tests.
- [x] Compare only observed dispatch records under the existing persistence lock; never skip the commit/uncertainty barrier.
- [x] Offload encoding of Dispatch's frozen snapshots to a worker thread.
- [x] Verify changed command delivery remains durable before response, and concurrent failed commits still block reads.
- [x] Run persistence, lifecycle, batch and recovery regressions, then isolated PostgreSQL checks.
- [x] Review safety and deploy the narrow service change locally only when operationally safe; preserve all existing journeys.
- [x] Measure deployed command-poll timings without creating any test dispatch in the existing fleet.

No timeout, safety threshold, human approval, stored schema, commit or push changes are included.


Additional regression: unchanged active/stale observations used to update the
same lifecycle timestamp and force journal writes. Transition is now idempotent
for the same state/reason; new telemetry contact, increasing distance and new
accepted events still update the timestamp. Four isolated cases failed before
this change and passed afterward, together with genuine progress timestamp
coverage. A separate read-only review found no actionable issues.

Current verification: 36 focused polling/persistence/batch checks and two
checks against a newly created disposable PostgreSQL 17 instance passed.
The full backend suite passed: 573 passed, two PostgreSQL-only checks skipped
in the default run (both passed separately against the disposable database).
The final backend image built successfully. A separate disposable Compose fleet
using that image approved two synthetic EVs, delivered both commands, received
acknowledgements and recorded movement. Its containers and network were removed.
Patch formatting passed. Pylance/Pyright is unavailable; editor diagnostics were
not verified. No frontend code changed in this polling fix.

Local rollout completed after the user explicitly approved a Docker Desktop
restart to restore its failed Kubernetes runtime. Kubernetes is Ready again.
The restart restarted the local containers; no existing journey was resumed.
Only the Dispatch deployment image changed, to
`csc3104/ev-fleet-backend:local-dispatch-latency-20261113020216`.

Read-only observation after rollout: 1,272 naturally occurring command polls
all returned 200 (median 1.33 ms, p95 45.55 ms, maximum 170.59 ms). All 265
checkpoint requests returned 200 (median 317.75 ms, p95 1,262.69 ms, maximum
1,720.39 ms). This was normal idle/paused-fleet traffic, not a controlled
seven-EV batch benchmark; no test dispatch was created in the existing fleet.

All five backend services report readiness. The dashboard and simulation console
return HTTP 200 on localhost:8080 and localhost:8090; dashboard Fleet and Dispatch
proxies return 20 vehicles and 23 preserved dispatch records. Fleet reports
16 online and 15 available EVs. Four existing journeys remain in RECOVERY_WAIT
with their claims and saved distances retained (extra-16, extra-14, extra-0,
ev-moving-02). Their recovery was not authorized or performed. No commit or
push was performed. Request a fresh recommendation before retrying approval.


## Follow-up: resolving retained journeys

The user subsequently authorized resolving the four paused journeys. Each fresh
recovery assessment validated saved positions/SOC and had no blockers. Coordinated
resume operations were submitted, retaining original commands and checkpoints.
Active movement exposed further Fleet checkpoint queueing: a normal idle polling
window is not sufficient evidence of healthy multi-EV relocation throughput.
Checkpoint responses reached about 2.4 seconds and caused guarded retries/lease
expiry. All four eventually recovered, but later leases expired again; completion
has not been established and the normal deployment guard must stay enabled.

Measured resource pressure: PostgreSQL at a 500m CPU limit accumulated about
20.7 seconds of throttling across a 75-second sample. Dispatch also accumulated
substantial throttling at its 500m limit. In-place pod resource resizing increased
PostgreSQL to 2 CPUs and Dispatch to 1 CPU, both requesting 250m. Container IDs and
restart counts were checked; those resizes did not restart either process.
Checkpoint median fell to about 0.7 seconds, but tails still exceeded two seconds.
PostgreSQL also repeatedly hit its 384Mi memory limit (cgroup memory.max events
increased); its memory allocation was increased in place to 1Gi, requesting 256Mi.
The same corrections were saved in the Kubernetes manifests for the next rollout.

A read-only code review confirmed full Fleet snapshot encoding and committed-cache
copying under its single writer, plus multiple durable checkpoints per event,
remain an amplification source. Simulator motion deliberately pauses during
network work/retries. No timeouts, positions, clocks, claims or safety guards were
bypassed. The four original relocations are not yet complete. Read-only safe-end
assessments report can_end=true for all four; ending their remaining travel would
require explicit operator intent to cancel those relocations.
