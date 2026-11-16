# Docker, nginx and Kubernetes

How the system is packaged and run. For step-by-step commands see
[local Kubernetes deployment](kubernetes-local.md) and
[local testing](local-testing.md).

## Container images

There are three images:

| Image | Built from | Contents |
|---|---|---|
| Backend | `Dockerfile` | Python 3.12 slim, `requirements.lock` and the `ev_fleet` package, run as a non-root `appuser` |
| Dashboard | `dashboard/Dockerfile` | Node 22 build stage runs `npm run build`; the result is copied into `nginx:1.28-alpine` |
| Simulation console | `simulation-console/Dockerfile` | Node 22 build stage for the frontend, then Python 3.12 slim running the console's FastAPI backend on port 8090 as user `probe` |

**One backend image runs every Python service.** Its default command starts
Fleet (`uvicorn ev_fleet.fleet.app:app --port 8000 --workers 1`). Compose and
Kubernetes override the command to start Demand, Rebalancing, Dispatch,
Facilities, the EV simulators, the traffic collector, the customer rental
workload or the optional CRDS workers from the same image. Inside a container
every HTTP service listens on port 8000; the 8001–8005 ports only exist when
services run directly on the host.

`--workers 1` matters: state-owning services keep their state in memory, so a
second worker would be a second, inconsistent copy.

## Docker Compose

`compose.yaml` is the lighter way to run the system without Kubernetes:

- All backends share one `x-backend` template: the same image build, `init`, and
  a health check against `/health/ready`.
- Services find each other by Compose DNS name (`http://fleet:8000`, and so on).
- Host ports default to 8001–8005 and can be changed with `FLEET_PORT`,
  `DEMAND_PORT` and similar variables.
- Redis and the LTA traffic collector run alongside the backends.
- **Profiles** keep optional parts off by default. `relocation` adds the
  dashboard (port 8080) and two moving simulators (`ev-moving-01`,
  `ev-moving-02`). `crds-workers` adds two HTTP CRDS scoring workers.
- Without the `relocation` profile, `ev-01` and `ev-02` run as stationary
  simulators that report telemetry but never accept commands.

Compose reads settings from `.env`.

## nginx in the dashboard

The dashboard container is nginx serving the built React files. It also acts as
the dashboard's reverse proxy, so the browser only ever talks to one origin and
no CORS is needed:

```
browser ──► GET /api/dispatch/dispatches/123
nginx   ──► GET http://dispatch:8000/api/v1/dispatches/123
```

- `/api/<service>/<path>` is forwarded to `http://<service>:8000/api/v1/<path>`
  for `fleet`, `demand`, `rebalancing`, `dispatch` and `facilities`.
- **Internal routes return 403.** Simulator telemetry, telemetry sessions,
  claims, command polling, ACKs and events, recovery handoff, parking holds and
  parking vacate are machine-to-machine calls. Blocking them at nginx means a
  browser can never pretend to be a simulator or take a claim.
- Proxy connect timeout is 2 s. Read timeouts are 8 s for Fleet and Demand and
  135 s for Rebalancing and Dispatch, which can wait on routing and CRDS.
- `/health` returns `ok` from nginx itself. It shows the static site is up, not
  that the backends are healthy; each dashboard panel reports its own API
  errors.
- Any other path falls back to `index.html` so client-side routes work.

There are two copies of this config. `dashboard/nginx.conf` is baked into the
image for Compose and uses Docker's DNS resolver (`127.0.0.11`) at request time,
so the UI still loads while a backend is down. In Kubernetes the
`dashboard-nginx` ConfigMap is mounted over it with one `location` block per
service. Keep the two in step when changing routes.

In development, the Vite dev server does the same `/api/<service>` →
`/api/v1` rewrite (`dashboard/vite.config.ts`).

The simulation console does not use nginx: its FastAPI backend serves both the
built frontend and `/api/probe/v1`.

## Kubernetes

The manifests in `k8s/` are combined with Kustomize (`kubectl kustomize k8s`)
into the `ev-fleet` namespace on Docker Desktop's single-node cluster. The start
script builds the images locally, gives each build a unique tag, and substitutes
those tags before applying. Pods use `imagePullPolicy: Never`, so nothing is
pulled from a registry and no cloud account is needed.

| File | Objects |
|---|---|
| `namespace.yaml` | `ev-fleet` namespace |
| `configmaps.yaml` | `ev-fleet-config` (shared settings) and `dashboard-nginx` |
| `data-services.yaml` | PostgreSQL StatefulSet with a persistent volume and its Secret; Redis Deployment |
| `services.yaml` | ClusterIP Services: five backends on port 8000, dashboard on 80, console on 8090 |
| `backends.yaml` | Fleet, Demand, Rebalancing, Dispatch and Facilities Deployments |
| `traffic-collector.yaml` | Single LTA traffic collector writing to Redis |
| `simulators.yaml` | `ev-moving-01` and `ev-moving-02` Deployments plus the `ev-moving-extra` StatefulSet of 18 |
| `dashboard.yaml`, `hpa-dashboard.yaml` | Dashboard Deployment and its autoscaler |
| `simulation-console.yaml`, `analytics-storage.yaml` | Console Deployment and its 256 MiB analytics volume |
| `simulations/` | Customer rental Deployment (0 replicas), optional Job, and the console's RBAC |
| `crds-worker.yaml` | Optional HTTP CRDS workers, **not** in the kustomization |

### Design choices

- **One replica per state owner.** Fleet, Demand, Rebalancing, Dispatch and
  Facilities each run exactly one replica with one worker, because their state
  is in memory behind a lock. Scaling them out would create competing writers.
- **Probes.** Each backend has a startup and liveness probe on `/health/live`
  and a readiness probe on `/health/ready`. A Pod that is still restoring its
  snapshot is not sent traffic.
- **Self-healing persistence.** When a service cannot confirm a PostgreSQL write
  it exits with code 75. Kubernetes restarts it and it reloads the last
  committed snapshot (see [architecture](architecture.md#failure-handling)).
- **StatefulSet for simulators.** `ev-moving-extra` gives each of its 18 Pods a
  stable hostname (`ev-moving-extra-0` and so on), which the simulator uses as
  its vehicle ID, so a restarted Pod keeps driving the same car.
- **Only the dashboard autoscales.** The HPA keeps 1–3 dashboard replicas at a
  60% CPU target. The dashboard is stateless nginx, so extra replicas are safe.
  The simulation console's analytics panel exists to observe this.
- **Least-privilege RBAC.** The console's ServiceAccount can only scale
  `customer-rental-demo`, read the rental Job, and read the dashboard HPA, Pods
  and metrics.
- **Configuration and secrets.** Non-secret settings live in `ev-fleet-config`.
  `scripts/render_facilities_secret.py` turns only whitelisted values from `.env`
  (LTA key, OneMap token, traffic settings) into Kubernetes Secrets. Changing a
  ConfigMap does not reach running Pods until they restart.

### Access

All Services are ClusterIP, so nothing is exposed outside the cluster.
`scripts/start-kubernetes.sh` (or the `.ps1` version on Windows) builds the
images, applies the manifests and opens two `kubectl port-forward` tunnels bound
to `127.0.0.1`:

- dashboard on <http://127.0.0.1:8080>
- simulation console on <http://127.0.0.1:8090>

## Data services

- **PostgreSQL** stores one JSONB snapshot per service plus per-record rows and
  telemetry history. Without `DATABASE_URL` a service runs in memory only.
- **Redis** is a display cache and the shared LTA traffic store. It is
  fail-open: if Redis is down, displays degrade, and no decision ever depends on
  it.
