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

Use the repository-owned startup command on both the original machine and a new
laptop:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

It performs the build, deployment, readiness waits, and both dashboard tunnels
described below. The remaining commands in this document are for inspection and
troubleshooting rather than an alternative startup workflow.

Confirm the target runtimes before building:

```powershell
kubectl config use-context docker-desktop
kubectl get nodes -o wide
docker context show
docker version
```

## Build local images

The kubeadm provisioner can use images in Docker Desktop's local image store.
The manifests use `imagePullPolicy: Never`, so Kubernetes reports a clear image
error instead of contacting an external registry if either build is missing.

```powershell
docker --context desktop-linux build --platform linux/amd64 `
  -t csc3104/ev-fleet-backend:k8s-local .

docker --context desktop-linux build --platform linux/amd64 `
  -t csc3104/ev-fleet-dashboard:k8s-local .\dashboard

docker --context desktop-linux build --platform linux/amd64 `
  -t csc3104/ev-fleet-simulation-console:k8s-local .\simulation-console

docker --context desktop-linux image inspect `
  csc3104/ev-fleet-backend:k8s-local `
  csc3104/ev-fleet-dashboard:k8s-local `
  csc3104/ev-fleet-simulation-console:k8s-local
```

No registry, `kind load`, AWS, or other cloud service is involved.

## Validate and deploy

Render and client-validate the namespace-scoped resources before applying them:

```powershell
kubectl kustomize .\k8s
kubectl apply --dry-run=client -f .\k8s\namespace.yaml
kubectl apply --dry-run=client -k .\k8s
```

Create the namespace first and wait until it is Active. The namespace is kept out
of `kustomization.yaml` deliberately so namespace-scoped objects cannot be applied
before it exists.

```powershell
kubectl apply -f .\k8s\namespace.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
kubectl apply -k .\k8s

kubectl -n ev-fleet rollout status statefulset/postgres --timeout=300s
kubectl -n ev-fleet wait --for=condition=Available deployment --all --timeout=300s
kubectl -n ev-fleet rollout status statefulset/ev-moving-extra --timeout=300s
kubectl -n ev-fleet get deployments,pods,services -o wide
kubectl -n ev-fleet get statefulsets
kubectl -n ev-fleet get endpointslices
```

There should be ten `1/1` Deployments, a `1/1` PostgreSQL StatefulSet, one
`18/18` EV StatefulSet, 29 running Pods, nine `ClusterIP` Services, and one
headless identity Service. The Fleet has
20 moving EV simulators: `ev-moving-01`, `ev-moving-02`, and the stable StatefulSet
identities `ev-moving-extra-0` through `ev-moving-extra-17`.

The extra StatefulSet distributes three EVs to each Singapore zone. Their starting
SOC values are deterministically spread from 65% to 94%. Each StatefulSet Pod name
is also its Fleet vehicle ID, so Pod replacement preserves identity while still
losing any process-local journey state as described above.

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

The Phase 4 experiment matrix does not need to be rerun merely to deploy its
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
