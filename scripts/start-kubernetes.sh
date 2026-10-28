#!/usr/bin/env bash
# Build/deploy both apps, or reconnect to an existing deployment without changes.
set -euo pipefail
cd "$(dirname "$0")/.."
mode="${1:-}"
if [[ "$mode" != "" && "$mode" != "--connect-only" ]]; then
  echo 'Usage: bash scripts/start-kubernetes.sh [--connect-only]' >&2; exit 1
fi
for tool in kubectl curl; do
  command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done
k() { kubectl --context docker-desktop -n ev-fleet "$@"; }
trap 'echo "Startup failed. Inspect: kubectl --context docker-desktop -n ev-fleet get pods" >&2' ERR

if [[ "$mode" != "--connect-only" ]]; then
  for tool in docker python3; do
    command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
  done
  python3 scripts/render_facilities_secret.py --check .env
  python3 scripts/render_facilities_secret.py --routing --check .env
  kubectl --context docker-desktop wait --for=condition=Ready node/docker-desktop --timeout=120s
  architecture=$(docker --context desktop-linux info --format '{{.Architecture}}')
  case "$architecture" in arm64|aarch64) platform=linux/arm64 ;; x86_64|amd64) platform=linux/amd64 ;; *) echo "Unsupported Docker architecture: $architecture" >&2; exit 1 ;; esac
  tag="local-$(date +%Y%m%d%H%M%S)"
  echo 'Building native images. Application Pods will be replaced; run between demos.'
  docker --context desktop-linux build --platform "$platform" -t "csc3104/ev-fleet-backend:$tag" .
  docker --context desktop-linux build --platform "$platform" -t "csc3104/ev-fleet-dashboard:$tag" ./dashboard
  docker --context desktop-linux build --platform "$platform" -t "csc3104/ev-fleet-simulation-console:$tag" ./simulation-console
  kubectl --context docker-desktop apply -f k8s/namespace.yaml
  kubectl --context docker-desktop wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
  python3 scripts/render_facilities_secret.py .env | kubectl --context docker-desktop apply -f -
  python3 scripts/render_facilities_secret.py --routing .env | kubectl --context docker-desktop apply -f -
  # Unique image tags ensure every changed service, including telemetry owners,
  # loads this build. The checked-in manifests remain unchanged.
  kubectl kustomize k8s | sed \
    -e "s|csc3104/ev-fleet-backend:k8s-local|csc3104/ev-fleet-backend:$tag|g" \
    -e "s|csc3104/ev-fleet-dashboard:k8s-local|csc3104/ev-fleet-dashboard:$tag|g" \
    -e "s|csc3104/ev-fleet-simulation-console:k8s-local|csc3104/ev-fleet-simulation-console:$tag|g" | kubectl --context docker-desktop apply -f -
fi

# Make both interfaces accessible as soon as core services are ready. Extra EV
# StatefulSet updates can take minutes and must not block the web interfaces.
deployments=(dashboard simulation-console)
if [[ "$mode" != "--connect-only" ]]; then
  k rollout status statefulset/postgres --timeout=300s
  deployments=(redis fleet demand facilities rebalancing dispatch dashboard simulation-console)
fi
for deployment in "${deployments[@]}"; do
  k rollout status "deployment/$deployment" --timeout=300s
done

dashboard_port="${DASHBOARD_PORT:-8080}"
console_port="${SIMULATION_CONSOLE_PORT:-8090}"
for port in "$dashboard_port" "$console_port"; do
  if [[ ! "$port" =~ ^[1-9][0-9]{0,4}$ ]] || (( port > 65535 )); then
    echo 'Dashboard ports must be integers from 1 to 65535.' >&2; exit 1
  fi
done
if [[ "$dashboard_port" == "$console_port" ]]; then echo 'The two apps need different local ports.' >&2; exit 1; fi
ready() {
  curl --noproxy 127.0.0.1 --fail --silent --max-time 2 "http://127.0.0.1:$1$2" >/dev/null && \
    curl --noproxy 127.0.0.1 --fail --silent --max-time 2 "http://127.0.0.1:$1/" >/dev/null
}
# Reuse healthy tunnels already opened by the user. Never kill another process.
for item in "$dashboard_port:/health" "$console_port:/health/live"; do
  port="${item%%:*}"; health="${item#*:}"
  if ! ready "$port" "$health" && command -v lsof >/dev/null && lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is occupied by an unhealthy or different app. Stop its tunnel, or set DASHBOARD_PORT / SIMULATION_CONSOLE_PORT to unused ports." >&2
    exit 1
  fi
done

# Each supervisor owns only its own kubectl child and reconnects if a Pod changes.
forward() (
  service="$1"; port="$2"; target="$3"; health="$4"; child=''
  trap 'if [[ -n "$child" ]]; then kill "$child" 2>/dev/null || true; fi' EXIT
  trap 'exit 0' INT TERM
  while true; do
    if ready "$port" "$health"; then sleep 2; continue; fi
    k port-forward --address 127.0.0.1 "service/$service" "$port:$target" &
    child=$!
    wait "$child" || true
    child=''
    sleep 2
  done
)
forward dashboard "$dashboard_port" 80 /health &
dashboard_pid=$!
forward simulation-console "$console_port" 8090 /health/live &
console_pid=$!
cleanup() { kill "$dashboard_pid" "$console_pid" 2>/dev/null || true; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
for attempt in {1..30}; do
  if ready "$dashboard_port" /health && ready "$console_port" /health/live; then break; fi
  sleep 1
done
if ! ready "$dashboard_port" /health || ! ready "$console_port" /health/live; then
  echo 'One or both interfaces are unavailable. Check port-forward errors above.' >&2; exit 1
fi
echo "EV rebalancing: http://127.0.0.1:$dashboard_port"
echo "Simulation console: http://127.0.0.1:$console_port"
echo 'EV readiness: kubectl --context docker-desktop -n ev-fleet get pods'
echo 'Keep this terminal open. Ctrl+C closes owned tunnels; Kubernetes keeps running.'
wait
