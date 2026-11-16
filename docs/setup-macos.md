# Setup on macOS

Run every command in Terminal from the repository root.

## 1. Install prerequisites

- Git, `bash`, `curl`, and Python **3.12+** as `python3`
- Node.js **22+** (only for local frontend work)
- [Docker Desktop for Mac](https://docs.docker.com/desktop/setup/install/mac-install/)
  for your chip (Apple silicon or Intel)
- Free local ports **8080** and **8090**

## 2. Enable Kubernetes in Docker Desktop

1. Open Docker Desktop, go to **Kubernetes** and click **Create cluster**.
2. Select **Kubeadm**, click **Create**, and wait until it shows as running.
   An existing Kubeadm cluster is fine.

See [Docker Desktop's Kubernetes guide](https://docs.docker.com/desktop/use-desktop/kubernetes/#enable-kubernetes).
Then check Docker is reachable and the node is `Ready`:

```sh
docker --context desktop-linux info
kubectl --context docker-desktop get nodes
```

## 3. Get the code and configure

```sh
git clone https://github.com/shauniee/Autonomous-EV-Fleet-Rebalancing.git
cd Autonomous-EV-Fleet-Rebalancing
if [ ! -e .env ]; then cp .env.example .env; fi
```

Add your LTA DataMall key and OneMap token to `.env` as described in
[configuration](configuration.md). Without them the app runs in offline demo mode.

## 4. Start the system

```sh
bash scripts/start-kubernetes.sh
```

This builds the images, applies the configuration, waits for the core services
and opens port forwards. **Keep this terminal open.** `Ctrl+C` closes the port
forwards; the Kubernetes workloads keep running.

- Simulation console: <http://127.0.0.1:8090>
- Operator dashboard: <http://127.0.0.1:8080>

EV simulators may still be starting. Check with
`kubectl --context docker-desktop -n ev-fleet get pods`, wait for online vehicles
in the operator dashboard, then follow the
[demo flow](simulation-console.md#typical-demo-flow).

If a port is taken, set `DASHBOARD_PORT` or `SIMULATION_CONSOLE_PORT` first and
use the URLs printed in Terminal.

Unlike the Windows script, the macOS script does not block a redeploy during
active relocations, rentals or charging, so wait for those to finish first.

## Other commands

| Command | Use it when |
|---|---|
| `scripts/start-kubernetes.sh --connect-only` | The deployment is running and you only need the dashboards. Does not rebuild, restart Pods or reload `.env`. |
| `scripts/start-kubernetes.sh --setup-metrics-server --allow-insecure-local-kubelet-tls` | A fresh cluster needs Metrics Server for the dashboard HPA. The TLS flag is for local Docker Desktop only. |
| `scripts/setup-local-metrics.sh --allow-insecure-kubelet-tls` | The deployment is running but the HPA shows `cpu: <unknown>`. Installs or repairs Metrics Server without rebuilding. |

Prefix each with `bash`. Without Metrics Server the simulation console's
performance analytics have no CPU or replica data; see
[the HPA section](kubernetes-local.md#dashboard-horizontal-pod-autoscaler-hpa).

## 5. Local development environment

The Kubernetes startup only needs `python3`. To run or test the backend
locally, create a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
```

Then run the checks:

```sh
env -u TELEMETRY_TEST_DATABASE_URL .venv/bin/python -m pytest -q
NODE_OPTIONS=--no-experimental-webstorage npm --prefix dashboard test -- --run
npm --prefix dashboard run build
npm --prefix simulation-console/frontend run build
```

Run `npm ci` in each frontend folder before the first run. `NODE_OPTIONS` is
needed on Node 25 or newer. See [local testing](local-testing.md#macos-and-linux)
for more.
