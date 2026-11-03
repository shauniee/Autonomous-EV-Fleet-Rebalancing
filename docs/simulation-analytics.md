# Simulation analytics and HPA experiments

The Simulation Console at <http://127.0.0.1:8090> includes **Performance analytics**
for the *local Docker Desktop Kubernetes demo only*.

## What it measures

- Actual CPU utilisation against the Dashboard HPA CPU target, its current and desired
  replicas, plus namespaced Dashboard Pod CPU/memory samples from the Metrics API.
- Synthetic HTTP request attempts, successful requests, failures, observed average
  throughput, and p95 request latency for the locally generated load.
- Replica-count changes detected between five-second samples.
- Comparisons across saved runs: mean throughput, peak observed CPU utilisation,
  and peak observed Dashboard replica count.

The dashboard HPA percentage is relative to the Dashboard container's **CPU request**,
not the Docker Desktop host's CPU capacity. Metrics Server reports near-real-time
resource metrics but is not a long-term metrics database. Our sample files are the
history used for charts and CSVs. Missing, stale or unavailable readings are shown as
unavailable or blank, never silently replaced with zero. The sample period is
approximately 5 seconds **after the previous request completes**, not a guaranteed
high-resolution timeline.

## Run a safe experiment

1. Start the application's [local Kubernetes stack](kubernetes-local.md). Confirm
   `kubectl top pods -n ev-fleet` and `kubectl get hpa dashboard -n ev-fleet`.
2. Open <http://127.0.0.1:8090> and find **Performance analytics**.
3. Choose **Normal**, **High** or **Extreme**, and a 30–120 second duration.
4. Watch HPA CPU percentage, current and desired replicas, actual Pod usage,
   synthetic throughput and request errors. If the CPU target is not reached,
   there may be **no** autoscaling. This is valid experimental evidence.
5. Stop manually, or allow the time limit to end. Export the corresponding CSV
   under Recorded experiments, and compare against a Normal baseline.
6. Repeat on an otherwise idle machine with multiple trials. Note workload
   configuration, Docker Desktop resources, background activity and any failures.

Preset load limits:

| Preset | EV-equivalent count | Equivalent update frequency | Max dashboard GET rate |
|---|---:|---:|---:|
| Normal | 10 | 0.2 Hz | 2/s |
| High | 50 | 1 Hz | 50/s |
| Extreme | 100 | 2 Hz | 200/s |

**These are workload equivalents, not injected EV telemetry.** The backend makes
bounded, asynchronous **GET-only** requests to the fixed internal Dashboard
service endpoint. No calls are made to Fleet, Demand, Rebalancing or Dispatch
mutation endpoints. No vehicles are added, moved, assigned or charged. Preset
values must not be reported as real simulation vehicle counts or telemetry
processing capacity. The demo is not an EV telematics benchmark.

Only one local experiment can run at a time, within one Simulation Console
replica. A preset has a maximum runtime of 120 seconds, a maximum target rate
of 200 requests/s, a limit of 12 concurrent active requests, and bounded
in-flight tasks. Traffic is never sent to an arbitrary URL supplied by an
end user. Stop early from the console if the machine becomes unresponsive.

## Implementation and security

- The console's FastAPI analytics router is under `/api/probe/v1/analytics`.
- The collector uses its Kubernetes ServiceAccount and read-only RBAC to fetch
  the named Dashboard HPA, Dashboard Pods and their resource metrics.
- HPA settings, Pod replica counts and Kubernetes manifests are never changed
  by this feature. No new HPA is installed for state-owning Fleet or Dispatch
  services.
- The console's local-only load generator requires
  `ANALYTICS_LOAD_ENABLED=true`, enabled in the supplied local Kubernetes
  manifest. `ANALYTICS_K8S_READ=true` independently enables read-only cluster
  observation. Leave both false for any exposed/shared deployment.
- Fixed destination `ANALYTICS_DASHBOARD_URL=http://dashboard/` points to
  the internal Kubernetes Service; only trusted operators may edit this server
  configuration. The console is already a **privileged, local-only tool** and
  must never be exposed to the public Internet without authentication.
- Sample data is stored at `ANALYTICS_DATA_DIR=/data/analytics` on the
  `simulation-analytics-data` PersistentVolumeClaim (256 MiB).
  Use `kubectl get pvc -n ev-fleet` to confirm binding. Records survive
  Console Pod restarts while the claim remains available. If the console
  runs outside Kubernetes with no configured path, the default is ephemeral
  `/tmp/ev-fleet-analytics`.
- An experiment interrupted by Pod restart is listed as **INTERRUPTED** on
  restart rather than falsely marked complete. The history view covers recent
  runs; CSV can be fetched by run identifier as long as its file remains.
- CSV contains timestamps, CPU measurements, desired/current replicas, ready
  Pods, observed memory, request counters, and p95 request latency.
  A missing measurement produces an empty field.

## Interpretation and limitations

A rise in HTTP request rate may increase the Dashboard's CPU load but **does
not guarantee HPA scale-out**. Nginx may serve static responses cheaply,
HPA samples are periodic, scale-up decisions use the requested CPU threshold,
and the cluster may have insufficient capacity. The local Docker Desktop
Kubernetes environment is normally a **single-node cluster**: increasing
replicas does not create additional physical compute capacity.

The load generator is inside the Simulation Console Pod, so the two services
share host CPU and memory. This may affect timing and comparisons. Do not
claim that HPA improves fleet telemetry performance based solely on Dashboard
HTTP load. To evaluate real telemetry throughput or autoscaling of a fleet
service, design a separate isolated workload and verify that scaling does not
violate state ownership or atomic dispatch invariants.

If Kubernetes RBAC or Metrics Server are unavailable, the observability API
provides partial results and error details. Deployment and load controls can
be used only when their underlying prerequisites work.

## Checks

Run local Python tests and the frontend build through the project's test
script:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
```

For a deployment-sensitive check on a disposable environment:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1 -Integration
kubectl get hpa,pods,pvc -n ev-fleet
kubectl top pods -n ev-fleet
```

Avoid running the load tests concurrently with active demonstrations of
simulated rentals, relocation or charging. Use the output as research data,
not a performance claim until replicated under controlled conditions.
