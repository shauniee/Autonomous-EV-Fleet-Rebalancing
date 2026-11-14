# Rental return outlook, charging operations, and local Kubernetes reliability

This branch adds **simulation-only** workflows. It does not create real rentals,
send commands to physical vehicles, dispatch ground staff, or operate real chargers.

## Local deployment

Use `scripts/start-kubernetes.ps1` with Docker Desktop/Kubeadm as before.
The manifests now install `customer-rental-demo` **paused at zero replicas** and
a scoped Kubernetes ServiceAccount for the Simulation Console. Open
http://127.0.0.1:8090 and start the optional six-car rental workload there.

The console ServiceAccount can only read/update the scale of
`customer-rental-demo` and read the optional rental Job's status. It cannot
create or delete other deployments. The backend refuses to stop the simulation
while the Fleet API reports an active `customer-ev-*` rental. The Job and
Deployment must never run together because they own the same six telemetry IDs.

**Security:** The console is a local administrative demo with no user login.
Port-forward to localhost only, never expose it to the internet or give
this ServiceAccount broader access.

## Predicted customer returns

Rental start events carry `expected_return_zone_id` and a simulated
wall-clock `expected_end_at`. The **actual return position and SOC** are
still determined from fresh ordered Fleet telemetry. A return forecast
never causes rental completion or makes a currently rented EV dispatchable.

The Simulation Console shows the active rentals and an advisory 30-minute
supply comparison using eligible-looking future arrivals. The Operator Dashboard
shows possible inbound returns beside the backend projected shortage.

These are **what-if estimates**, not Demand or Rebalancing inputs. The console
uses the current approximate zone and energy assumptions; the operator outlook
uses a simpler current-SOC filter. Neither promises available charging bays,
arrival punctuality or sufficient battery after the trip. The existing
backend recommendation/approval flow is unchanged.

True pre-booked rentals *before* customer pickup, economic incentives for
customer destinations, probabilistic ETA confidence and automatic integration
with the rebalancing formula are **not implemented** in this iteration.

## Battery alerts and charging operations

Operator Dashboard lists fresh online EVs with SOC at most 30% and labels
those under 20% as critical. The dispatcher must explicitly queue a task.
Fleet rejects queued tasks for rented, claimed, stale, busy or sufficiently
charged EVs, and prevents tasked EVs from being selected for new rentals
or relocation.

Task lifecycle:
`PENDING -> ASSIGNED -> IN_PROGRESS -> COMPLETED`.
Pending/assigned tasks can instead be cancelled.
A staff assignment/start-of-work is an *operator record*, not evidence of
actual charging. **Completion fails closed** unless fresh telemetry reports
`charging_state=COMPLETE` and SOC at least 50%.
Task requests are version-checked and idempotent, with the task queue saved in
the existing Fleet PostgreSQL snapshot. This remains a single-writer Fleet
process, consistent with existing state ownership.

The manual assistance task queue remains separate from an approved charger
relocation and does not command an EV to move. The moving simulators now support
an allocation-bound [human cable handoff](charging-cable-handoff.md): wait after
arrival, charge only after connection confirmation, and stay unavailable until
disconnection confirmation and fresh telemetry. Ground-team confirmations are
simulation records; they do not operate physical chargers. No task or cable
action bypasses the Dispatch approval API.

## Deployment reliability

- PostgreSQL volume init now has `CHOWN`, `DAC_OVERRIDE` and `FOWNER`
  for Docker Desktop hostpath permission repair. It runs as UID 0 but other
  app containers remain non-root. No PVC deletion is required.
- All 20 EV simulators have a five-second startup/liveness exec timeout
  (five consecutive failures for liveness) and eight-second readiness timeout.
- PowerShell startup checks the Simulation Console at `/health/live`,
  matching its actual endpoint.
- For an existing cluster whose Pods have old StatefulSet settings, allow
  Kubernetes to complete the rolling update before judging stability.
  Do not reset storage to resolve probe errors.

## Suggested verification after a pull

Run these from the repository root:

```powershell
python -m pytest tests/unit/test_charging_tasks.py tests/unit/test_rentals.py -q
cd dashboard; npm.cmd ci; npm.cmd run build; npm.cmd test -- --run
cd ..\\simulation-console\\frontend; npm.cmd ci; npm.cmd run build
cd ../..; kubectl kustomize k8s
```

Then deploy on Docker Desktop, verify PostgreSQL is `1/1`, all 20 base
simulators are `1/1`, the customer workload is initially paused, and
use the console to start the rentals. Test alert creation for an EV below
30% SOC and confirm that the resulting task blocks relocation. Verify
that stopping an active rental returns HTTP 409 and a completed/non-active
scenario can stop safely.

**Limitations:** The expected destination is a simulator assumption; late
returns, predicted-to-actual arrival quality, and charging staff capacity
need separate experiment instrumentation. No live-cluster verification is
claimed solely from source edits or CI checks.
