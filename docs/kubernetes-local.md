# Local Kubernetes deployment (Docker Desktop kubeadm)

This deployment runs the complete Singapore EV relocation demonstration on the
single-node Docker Desktop Kubernetes cluster, including the operator dashboard,
simulation console, backend services, and 20 moving EV simulators.

Fleet, Demand, Facilities, Rebalancing and Dispatch write durable service
snapshots to the local PostgreSQL StatefulSet. Redis is a fail-open cache for
display-only snapshots. EV physical journey progress remains process-local, and
the snapshot model is not safe for concurrent service replicas, so state-owning
services and individual simulators still use one replica and `Recreate`.

## Prerequisites

- Docker Desktop is using Linux containers and its `desktop-linux` context.
- Docker Desktop Kubernetes uses the `docker-desktop` context and kubeadm
  provisioner.
- `kubectl get nodes` reports `docker-desktop` as `Ready`.
- Run commands from the repository root in Windows PowerShell.

## Reproducible startup

For macOS and optional real parking/charging observations, see [LTA setup](lta-datamall.md).

Use the repository-owned startup command on both the original machine and a new
laptop:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

The **default mode is Deploy**: a fresh local image tag is created on every run
(`local-<timestamp>-<unique-id>`), the script builds all three images, renders
Kustomize with that tag, applies the resources and waits for the new Pods to
become ready. This prevents mixed old/new backend API versions without requiring
manual `kubectl rollout restart` commands. Because all backends and simulator
Pods use the shared backend image, each deploy updates all of them.

To **only connect** to an existing deployment, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -ConnectOnly
```

`-ConnectOnly` checks the dashboards and opens/reuses local tunnels, without
building, applying YAML, setting up metrics, or restarting Pods. Leave the
terminal open if it creates new tunnels. The flags `-ConnectOnly` and
`-SetupMetricsServer` cannot be combined.

**Safety gate:** Before building and immediately before applying, Deploy mode
reads the authoritative Fleet snapshot and checks the optional rental workload
state. It refuses to deploy while the rental Deployment or Job is active, or
Fleet reports an active rental, dispatch claim, in-flight relocation or charging
session. End all rental/relocation/charging work before redeploying; do not
approve new actions during rollout. If Fleet is unreachable, deployment
**fails closed**. The guard cannot prevent an operator racing to approve work
after the final check, so keep the demo quiescent during deploy.

PostgreSQL PVCs and snapshots are preserved; the script does not reset data.
The remaining manual commands in this document are for diagnostics, not a
second deployment workflow.

Confirm the target runtimes before building:

```powershell
kubectl config use-context docker-desktop
kubectl get nodes -o wide
docker context show
docker version
```

## PostgreSQL bootstrap and safe recovery

The official `postgres:17-alpine` entrypoint creates the database named in
`POSTGRES_DB=ev_fleet` and appends host-password authentication rules when
the data directory is **empty**. If the container stops after `initdb` has
written `PG_VERSION` but before bootstrap finishes, later starts may report
"Skipping initialization" and leave `ev_fleet` absent or `pg_hba.conf`
allowing only localhost. A PostgreSQL TCP socket can still be open, so
`pg_isready` **does not prove the application can connect**.

The Kubernetes configuration now protects slow Windows laptop first starts:

- `startupProbe` uses TCP on `127.0.0.1`, not the temporary local Unix
  socket opened during the Docker image's bootstrap; its 15-minute budget
  keeps liveness disabled during slow database creation
- `POSTGRES_HOST_AUTH_METHOD=scram-sha-256` explicitly enables
  password-authenticated host connections for **new** volumes; no
  unauthenticated `trust` rule is introduced for remote clients
- `readinessProbe` runs `SELECT 1` against the configured `ev_fleet` DB
  through the PostgreSQL Pod IP with the configured password, so both the
  real database and network authentication must work before Fleet uses it
- `livenessProbe` only checks whether PostgreSQL accepts TCP connections;
  an existing recoverable but incomplete PVC will stay unready rather than
  being killed repeatedly

The deployment preflight obtains PostgreSQL's actual Pod IP through the
Kubernetes API rather than relying on container hostname lookup. It uses a
single non-interactive remote shell command (no nested quotes, encoded
pipelines or interactive password prompts) to test authenticated SQL. It prints
underlying SQL/kubectl failures so an unavailable database can be distinguished
from a false-negative preflight.

To test the existing database without rebuilding Docker images, applying
manifests or restarting EV simulator Pods, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1 -CheckPostgresOnly
```

This uses the same non-interactive database authentication check that Deploy
mode runs, but exits immediately after the check succeeds or fails. It will not
modify the database or StatefulSet.

**Important:** A successful manual `psql -W` login only validates the password
typed interactively. The deployment preflight tests the **configured**
`POSTGRES_PASSWORD` environment variable from the Kubernetes Secret. If the
Secret's password has drifted from an existing persistent database user's
password, `-W` can succeed while the script correctly fails. Do not delete
the PVC or print credentials; reconcile the database user's password and
Kubernetes Secret safely instead.

**Existing PVCs are never rewritten.** Before an update, the Windows
`start-kubernetes.ps1` script checks any existing PostgreSQL installation with
the same read-only, authenticated Pod-IP SQL query. If it fails, startup stops
with a diagnostic before changing the application Pods. Your prior manual
`pg_hba.conf` repair on this laptop is preserved.

Inspect a failed database without changing it:

```powershell
kubectl -n ev-fleet get pods
kubectl -n ev-fleet get pvc postgres-data-postgres-0
kubectl -n ev-fleet logs postgres-0 -c postgres --tail=100
kubectl -n ev-fleet exec postgres-0 -c postgres -- psql -U ev_fleet -d template1 -c "SELECT datname FROM pg_database ORDER BY datname;"
kubectl -n ev-fleet exec postgres-0 -c postgres -- psql -U ev_fleet -d template1 -c "SELECT type,database,user_name,address,auth_method,error FROM pg_hba_file_rules;"
```

For an already-incomplete PVC, adding the environment variable or probe **will
not replay first-time initialization**. Back up and examine the data and
catalogue first. Only recover manually after determining whether the old Fleet
snapshots matter. Never delete a PVC as part of routine deployments.
On a completely new empty cluster, use the standard startup script; do not
manually create a database or patch authentication rules.

Because each computer has its own PVC, the application is portable but the
underlying Fleet state is not synchronised between PC and laptop.

## Dashboard Horizontal Pod Autoscaler (HPA)

The repository includes `k8s/hpa-dashboard.yaml` through `k8s/kustomization.yaml`.
It scales **only** the stateless operator Dashboard (1–3 replicas, CPU target
60% of its existing 10m CPU request). Stateful Fleet, Demand, Facilities,
Rebalancing, Dispatch and moving EV services remain single-replica; adding
replicas to them could duplicate commands or cause state divergence.

A fresh Docker Desktop kubeadm cluster needs Metrics Server for CPU-based
HPA. To install the pinned upstream Metrics Server v0.9.0 automatically and
enable the **explicit local-only** kubelet certificate workaround, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\\scripts\\start-kubernetes.ps1 `
  -SetupMetricsServer -AllowInsecureLocalKubeletTls
```

For an already-deployed cluster, install or repair Metrics Server **without
rebuilding the application**:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local-metrics.ps1 `
  -AllowInsecureKubeletTls
```

Do **not** follow this with `kubectl apply -k k8s`, because that would
overwrite the versioned image references with the `k8s-local` template tags.

The installer preserves an existing Metrics Server version and installs
v0.9.0 only if missing. It requires GitHub access to download the release.
`-AllowInsecureLocalKubeletTls` and `-AllowInsecureKubeletTls` explicitly
add `--kubelet-insecure-tls`: this disables kubelet TLS certificate
verification to work around the Docker Desktop node certificate missing
IP SANs. **Never use this workaround on production or shared clusters.**
With valid kubelet certificates, omit that flag. The script refuses any
Kubernetes context other than `docker-desktop`.

Verify metrics, HPA and replicas with:

```powershell
kubectl get apiservice v1beta1.metrics.k8s.io
kubectl top pods -n ev-fleet
kubectl get hpa dashboard -n ev-fleet
kubectl describe hpa dashboard -n ev-fleet
kubectl get deployment dashboard -n ev-fleet
```

An HPA present in the cluster is not evidence of actual scaling. The
Dashboard normally uses very little CPU, so it may remain at one Pod.
Demonstrate scaling separately under safe, controlled **read-only**
Dashboard requests while watching HPA metrics and Pod counts. Avoid load
tests against live Dispatch, Fleet mutations or the EV simulators.
The single-node Docker Desktop cluster cannot survive host-level failure.
If metrics are unavailable, inspect `kubectl -n kube-system logs
deployment/metrics-server --tail=50`.

## Versioned local image build and deployment

The startup script automatically builds the backend, dashboard, and simulation
console images with a unique local tag and updates Kubernetes image references.
`imagePullPolicy: Never` makes Docker Desktop use the local image store.
No container registry, `kind load`, AWS or cloud service is required.

The manifests in `k8s/` retain `k8s-local` as **source templates**; the
deployment script renders them to a temporary YAML file with the new tag and
removes that file after applying it. If you run plain `kubectl apply -k k8s`
**after** deploying, Kubernetes may roll services **back** to the unversioned
template image references. Always use `scripts/start-kubernetes.ps1` for a
normal redeployment, not `kubectl apply -k` or a series of manual restarts.

You can inspect the installed images with:

```powershell
kubectl -n ev-fleet get deployment fleet dashboard simulation-console `
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
kubectl -n ev-fleet get statefulset ev-moving-extra `
  -o custom-columns='NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image'
kubectl -n ev-fleet get pods
```

To check source manifests without changing the cluster:

```powershell
kubectl kustomize .\k8s
```

At baseline there should be ten `1/1` Deployments, a `1/1` PostgreSQL StatefulSet, one
`18/18` EV StatefulSet, 29 running Pods, nine `ClusterIP` Services, and one
headless identity Service. With HPA active, Dashboard may scale to three Pods. The Fleet has
20 moving EV simulators: `ev-moving-01`, `ev-moving-02`, and the stable StatefulSet
identities `ev-moving-extra-0` through `ev-moving-extra-17`.

The extra StatefulSet distributes three EVs to each Singapore zone. Their starting
SOC values are deterministically spread from 65% to 94%. Each StatefulSet Pod name
is also its Fleet vehicle ID, so Pod replacement preserves identity while still
losing any process-local journey state as described above.

StatefulSet scaling creates simulator Pods in parallel. Rolling updates use
`maxUnavailable: 10%`, rounded up by Kubernetes, so independent EV simulators are
replaced in bounded batches rather than one at a time. This updates two Pods at a
time for the default 18 replicas and 20 at a time for 200 replicas. The rollout
still waits for each batch to publish telemetry and pass its Fleet readiness check
before continuing.

To confirm the Pods are using the locally built images:

```powershell
kubectl -n ev-fleet get pods `
  -o custom-columns='POD:.metadata.name,IMAGE:.spec.containers[0].image,IMAGE_ID:.status.containerStatuses[0].imageID'
```

## Open both dashboards

Keep each command running in its own PowerShell terminal:

```powershell
kubectl -n ev-fleet port-forward service/dashboard 8080:80
```

```powershell
kubectl -n ev-fleet port-forward service/simulation-console 8090:8090
```

Open the operator dashboard at <http://127.0.0.1:8080> and the simulation console
at <http://127.0.0.1:8090>. Only the two web interfaces are forwarded. Fleet,
Demand, Rebalancing, Dispatch, and Facilities remain internal `ClusterIP` Services.
Both dashboards reach the same backends through Kubernetes service DNS.

If port 8080 is occupied, use `18080:80` and substitute port 18080 below.

## End-to-end relocation demonstration

Demand simulation is disabled for a deterministic demo. First wait for both
moving EVs to be fresh:

```powershell
Invoke-RestMethod http://127.0.0.1:8080/api/fleet/vehicles |
  ConvertTo-Json -Depth 12
```

Create a current manual CBD demand observation using its actual version:

```powershell
$zone = Invoke-RestMethod http://127.0.0.1:8080/api/demand/zones/cbd
$demand = @{
  expected_version = $zone.demand_version
  observed_at = [DateTime]::UtcNow.ToString('o')
  desired_available_vehicles = 12
} | ConvertTo-Json

Invoke-RestMethod -Method Put `
  -Uri http://127.0.0.1:8080/api/demand/zones/cbd/demand `
  -ContentType application/json `
  -Body $demand
```

Then complete the operator-owned workflow in the dashboard:

1. Select the **CBD CRITICAL** hotspot.
2. Select **Recommend EV** and inspect the backend recommendation.
3. Select **Review dispatch**.
4. Select **Approve Dispatch** before the recommendation expires.
5. Observe acknowledgement, `IN PROGRESS`, incremental position/SOC updates,
   and finally `SUCCEEDED`.
6. Confirm the EV shows `cbd`, `AVAILABLE`, and no active Fleet claim.

The map uses public OpenStreetMap tiles when network access is available. Tile
failure does not block the zone list or relocation workflow.

For repeatable browser verification after seeding CBD demand, run the checked-in
Playwright script. It clicks the same dashboard controls, uses the real deployed
APIs, and waits for confirmed arrival; only public map tiles are stubbed.

```powershell
node .\dashboard\scripts\verify-kubernetes.cjs http://127.0.0.1:8080
```

## Controlled Pod self-healing demonstration

Run this first with only completed relocations. Active-workflow recovery is a
separate failure test because the EV simulator can be at any movement step.

```powershell
$beforePod = kubectl -n ev-fleet get pods `
  -l app.kubernetes.io/name=dispatch `
  -o jsonpath='{.items[0].metadata.name}'
$beforeUid = kubectl -n ev-fleet get pod $beforePod `
  -o jsonpath='{.metadata.uid}'

kubectl -n ev-fleet delete pod $beforePod

kubectl -n ev-fleet wait --for=condition=Ready pod `
  -l app.kubernetes.io/name=dispatch `
  --timeout=120s

$afterPod = kubectl -n ev-fleet get pods `
  -l app.kubernetes.io/name=dispatch `
  -o jsonpath='{.items[0].metadata.name}'
$afterUid = kubectl -n ev-fleet get pod $afterPod `
  -o jsonpath='{.metadata.uid}'

[pscustomobject]@{
  BeforePod = $beforePod
  BeforeUid = $beforeUid
  AfterPod = $afterPod
  AfterUid = $afterUid
  Replaced = $beforeUid -ne $afterUid
}

Invoke-RestMethod http://127.0.0.1:8080/api/dispatch/dispatches |
  ConvertTo-Json -Depth 12
```

Expected result: `Replaced` is `True`, the new Pod is Ready, and the dispatch list
still contains the same records. PostgreSQL restores the Dispatch snapshot while
Kubernetes restores the process.

## Regression checks

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pip check

Push-Location .\dashboard
npm.cmd test -- --run
npm.cmd run build
npm.cmd run test:e2e
Pop-Location

Push-Location .\simulation-console\frontend
npm.cmd ci
npm.cmd run build
Pop-Location

kubectl kustomize .\k8s
kubectl apply --dry-run=client -k .\k8s
git diff --check
git diff --exit-code -- experiments results
```

The strategy evaluation experiment matrix does not need to be rerun merely to deploy its
unchanged application to Kubernetes. Its checked-in raw evidence must remain
byte-for-byte untouched.

## Troubleshooting

Inspect status, events, and logs:

```powershell
kubectl -n ev-fleet get deployments,pods,services,endpointslices -o wide
kubectl -n ev-fleet get events --sort-by='.lastTimestamp'
kubectl -n ev-fleet describe pod <pod-name>
kubectl -n ev-fleet logs deployment/fleet --tail=200
kubectl -n ev-fleet logs deployment/demand --tail=200
kubectl -n ev-fleet logs deployment/rebalancing --tail=200
kubectl -n ev-fleet logs deployment/dispatch --tail=200
kubectl -n ev-fleet logs deployment/simulation-console --tail=200
kubectl -n ev-fleet logs deployment/ev-moving-01 --tail=200
kubectl -n ev-fleet logs ev-moving-extra-0 --tail=200
```

Check internal DNS from an existing backend image:

```powershell
kubectl -n ev-fleet exec deployment/rebalancing -- `
  python -c "import socket; print(socket.gethostbyname('fleet'))"
```

Common symptoms:

- `ErrImageNeverPull`: build both exact image tags in the `desktop-linux`
  context, then recreate the affected Pod.
- Dashboard `502 Bad Gateway`: inspect the target Service endpoints and backend
  Pod logs; the dashboard itself can remain healthy during backend failure.
- Simulator not Ready: inspect its logs and the Fleet endpoint. Dependency
  failures affect readiness but do not trigger dependency-coupled liveness
  restarts.
- Recommendation expired: request a new recommendation and approve it within
  the displayed countdown. Never reuse it with a new request identity.
- Port-forward disconnected after a local interruption: rerun the dashboard
  port-forward command. This does not restart the cluster workload.

For temporary direct API diagnosis, use separate terminals and close the
forwards afterward:

```powershell
kubectl -n ev-fleet port-forward service/fleet 8001:8000
kubectl -n ev-fleet port-forward service/demand 8002:8000
kubectl -n ev-fleet port-forward service/rebalancing 8003:8000
kubectl -n ev-fleet port-forward service/dispatch 8004:8000
```

## Cleanup

This deletes all Kubernetes application resources and all of their ephemeral
state. It does not remove the locally built images.

```powershell
kubectl delete namespace ev-fleet --wait=true
kubectl get namespace ev-fleet
```

Optional image removal, only when the local Kubernetes images are no longer
needed:

```powershell
docker --context desktop-linux image rm `
  csc3104/ev-fleet-backend:k8s-local `
  csc3104/ev-fleet-dashboard:k8s-local `
  csc3104/ev-fleet-simulation-console:k8s-local
```
