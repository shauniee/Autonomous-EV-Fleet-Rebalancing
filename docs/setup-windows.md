# Setup on Windows

Run every command from **Windows PowerShell** in the repository root. Each
computer needs its own Docker Desktop and its own `.env` file.

## 1. Install prerequisites

- Git and Windows PowerShell
- Python **3.12+** and Node.js **22+**
- Docker Desktop using **Linux containers**, with **Kubernetes** enabled
- `kubectl` using the **`docker-desktop`** context
- Free local ports **8080** (operator dashboard) and **8090** (simulation console)

## 2. Get the code and configure

```powershell
git clone https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing.git
cd Autonomous-EV-Fleet-Rebalancing
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

Check that Docker and Kubernetes point at your local Docker Desktop and the node
is `Ready`. Never deploy to an unintended remote cluster.

```powershell
docker context show
kubectl config current-context
kubectl get nodes
```

Add your LTA DataMall key and OneMap token to `.env` as described in
[configuration](configuration.md). Without them the app runs in offline demo mode.

## 3. Prepare, test and launch

Run these in order:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup-local.ps1
```

Creates or repairs the project `.venv` and installs the Python packages. A
SHA-256 marker skips reinstalling until the dependency file changes; add
`-Force` to reinstall. The start script also calls this automatically.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\test-local.ps1
```

Runs the backend tests, dashboard unit tests and build, and the simulation
console build, installing npm dependencies if missing. A pass does not prove
live OneMap routing or Kubernetes work.

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\start-kubernetes.ps1
```

Checks the local Docker and Kubernetes environment, refuses to redeploy while a
relocation, rental or charging task is active, builds the images, deploys
everything, waits for rollouts and opens the dashboard tunnels. **Keep this
window open.** `Ctrl+C` closes the tunnels; the Kubernetes workloads keep running.

## 4. Open the dashboards

- Simulation console: <http://127.0.0.1:8090>
- Operator dashboard: <http://127.0.0.1:8080>

EV simulators may still be starting. Wait for online vehicles in the operator
dashboard, then follow the [demo flow](simulation-console.md#typical-demo-flow).

## Other commands

| Command | Use it when |
|---|---|
| `.\scripts\start-kubernetes.ps1 -ConnectOnly` | The deployment is running and you only need the dashboards. Does not rebuild, restart Pods or reload `.env`. |
| `.\scripts\start-kubernetes.ps1 -FreshBoot` | You need to recreate `.venv` and replace project Pods. Keeps PostgreSQL data and still refuses during active work. Not a routine fix. |
| `.\scripts\start-kubernetes.ps1 -CheckPostgresOnly` | You only want to check the PostgreSQL StatefulSet. |
| `.\scripts\start-kubernetes.ps1 -SetupMetricsServer -AllowInsecureLocalKubeletTls` | A fresh cluster needs Metrics Server for the dashboard HPA. The TLS flag is for local Docker Desktop only. |
| `.\scripts\test-local.ps1 -Integration` | You changed infrastructure, Compose, Kubernetes or the database. Needs Docker and `act`; uses disposable resources only. |

Prefix each with `powershell -ExecutionPolicy Bypass -File`.

## Optional customer rentals

Start and stop the rental workload from the simulation console, or with kubectl:

```powershell
kubectl apply -f .\k8s\simulations\customer-deployment.yaml
kubectl -n ev-fleet logs -f deployment/customer-rental-demo
```

Never run it together with `customer-job.yaml`; both use the same six vehicle
IDs. See [customer rentals](simulation-console.md#customer-rentals).

## Troubleshooting

| Symptom | Check |
|---|---|
| OneMap routing unavailable | The routing banner and token expiry. After updating `.env`, run the normal startup. |
| No recommendation, or DEFER | The recommendation's **Evidence** panel for stale demand, Fleet or routing. |
| HOLD / no useful move | EV availability, parking, battery reserve and the fleet-wide shortage effect. |
| Data is stale | Simulator Pods and service health; refresh demand. |
| Review dispatch is disabled | Wait for fresh data or request a new recommendation. |
| Laptop and PC behave differently | Same commit, `.env`, Kubernetes context, Pod readiness and clocks. |

See also [why a recommendation may not appear](simulation-console.md#why-a-recommendation-may-not-appear),
[local Kubernetes deployment](kubernetes-local.md) and [local testing](local-testing.md).
