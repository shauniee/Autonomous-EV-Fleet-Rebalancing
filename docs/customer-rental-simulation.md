# Customer rental simulation — finite Kubernetes experiment

**Status:** opt-in experiment, not a new cluster or UI. The existing namespace
is `ev-fleet` on the Docker Desktop Kubernetes cluster. Existing demand,
Fleet, Rebalancing, Dispatch, 20 EV simulator Pods and two dashboards are
unchanged. The Job or persistent Deployment adds **six separately owned, rental-capable EV identities**
(`customer-ev-01`...`customer-ev-06`). It does not take control of any existing
`ev-moving-*` simulator, which would create competing telemetry ownership.

## Running

From the checked-out repository root, first deploy the normal application:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

In a **second** terminal, while the backend is Ready, start the customer
experiment (optional, and never automatically run by the normal startup script):

```powershell
kubectl apply -f .\k8s\simulations\customer-job.yaml
kubectl -n ev-fleet logs -f job/customer-rental-simulation
kubectl -n ev-fleet get job customer-rental-simulation
```

To run a new experiment, **first check no customer rental is active** and no
relocation is underway for `customer-ev-*`. Do not delete a running
Job as a reset: that can strand active rentals. A safe completed run can be
recreated with:

```powershell
kubectl -n ev-fleet delete job customer-rental-simulation
kubectl apply -f .\k8s\simulations\customer-job.yaml
```

The Job uses the **existing local backend image** and Kubernetes Service DNS.
After source changes, rerun the repository startup script to rebuild and
redeploy the image before creating the Job.

## Recommended: keep the customer EVs online while relocating

For an interactive demonstration, use the **persistent, single-replica Deployment**
instead of the finite Job:

```powershell
kubectl apply -f .\k8s\simulations\customer-deployment.yaml
kubectl -n ev-fleet logs -f deployment/customer-rental-demo
kubectl -n ev-fleet get deployment customer-rental-demo
```

The Deployment runs the same scheduled rentals and then remains in observation
mode. Its six EVs continue publishing telemetry and polling Dispatch, so you
can approve relocations after rentals have ended. The scenario does **not**
repeat automatically; its demand-generation phase runs once per Pod start.

**Never run the Deployment and Job simultaneously.** Both use the same six
`customer-ev-*` vehicle IDs and would compete for Fleet telemetry sessions.
Do not restart the Deployment during active rentals or relocations. Since the
simulation is opt-in, it is not applied by the base Kustomize startup.

To stop the Deployment, first verify there are no active rentals or relocations
for its cars, then run:

```powershell
kubectl -n ev-fleet delete deployment customer-rental-demo
```

This leaves the six EV identities in Fleet, but they will become stale/offline
once the simulator stops. A future restart reuses their last safe position
when they have no active rental/claim.

## What it does

- A single Job starts six customer-owned `MovingNode` instances. They
  register fresh Fleet telemetry sessions and continue polling Dispatch for
  relocations when not rented, so returned cars can join relocation work.
- Seeded scenario weights choose an eligible car and a different destination
  from the six existing representative zone centres. Morning favours residential
  pickups and CBD destinations; evening reverses the preference. These are
  **synthetic assumptions** — not real Singapore ridership measurements.
- Fleet's existing version-checked rental API starts each rental. The owning
  EV process alone advances physical position and SOC, using the same motion
  model and `SIMULATION_TIME_SCALE` as relocation.
- Fleet maintains `active_rental` and refuses claims while rented. Customer
  telemetry retains relocation state `IDLE` because customer travel is
  *not* a Dispatch relocation. When the car arrives, its node sends fresh
  telemetry, confirms the rental return with Fleet, and only then restores
  the available status.
- The finite Job finishes when scheduled requests and active trips are complete. The persistent Deployment instead retains telemetry and dispatch polling after the generator completes.
  Logs contain customer START/RETURN records and a JSON SUMMARY of attempted,
  returned, unfulfilled and failed requests. The operator Dashboard can show
  the added cars and zone changes.

The customer script does **not** change authoritative Demand zone requirements
or auto-approve relocation. In the Simulation Console, set a repeatable
desired available vehicles target in Punggol/Woodlands to highlight shortages.
Keep `DEMAND_SIMULATION_ENABLED=false` for this controlled run.
Different arrival times and network scheduling can cause different results,
even with the same seed; this is not yet an exact replay evaluator.

## Failure and safety boundaries

- Job is one-shot (`parallelism: 1`, `backoffLimit: 0`). It is
  intentionally not a Deployment that constantly replays bookings.
- The process is the **sole telemetry owner** for its six identities. Crashes,
  forced deletion, lost responses or mid-rental restarts may leave a rental
  active in Fleet. It fails closed rather than overwriting an unresolved trip.
  Inspect the Fleet and rental records before recovery. No automatic rewind,
  refund, or forced return exists.
- Re-running the completed Job reuses each registered car's latest safe idle
  position and SOC. It will not teleport a previously returned car to its
  initial start location. A pristine trial requires a separate reset/isolated
  namespace/database; **do not delete shared database PVCs** just to repeat.
- This simulation produces no real booking, real GPS tracking, road routing,
  customer authentication or verified Singapore mobility data.
- The six new cars increase total fleet supply: compare baselines only after
  controlling for fleet size. For fair with-vs-without-relocation evaluations,
  record the same initial fleet and customer schedule in isolated experiments.
- The finite Job stops publishing telemetry at completion, so its six cars will become stale and cannot be relocated later unless their owning simulator is restarted. Use the persistent Deployment for interactive relocation after returns. The existing simulation console remains unchanged. This is a CLI/Job
  capability, not a new interactive controller.

Future enhancement: coordinate a replayable trip schedule with the Demand
Service's time-based target-availability profile, and give the existing
Simulation Console an optional Job status and scenario selector.
