#!/usr/bin/env bash
# macOS / bash entrypoint. Run from any directory; .env remains backend-only.
set -euo pipefail
cd "$(dirname "$0")/.."
for tool in docker kubectl python3 curl; do
  command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done

kubectl config use-context docker-desktop
kubectl wait --for=condition=Ready node/docker-desktop --timeout=120s

docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-backend:k8s-local .
docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-dashboard:k8s-local ./dashboard
docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-simulation-console:k8s-local ./simulation-console

kubectl apply -f k8s/namespace.yaml
kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
# Whitelisted settings only; no key in command arguments, files or frontend images.
python3 scripts/render_facilities_secret.py .env | kubectl apply -f -
kubectl apply -k k8s
# Reload only the services changed by the LTA integration and its Secret.
kubectl -n ev-fleet rollout restart deployment/facilities deployment/rebalancing \
  deployment/dispatch deployment/dashboard
kubectl -n ev-fleet rollout status statefulset/postgres --timeout=300s
for deployment in redis fleet demand facilities rebalancing dispatch dashboard simulation-console ev-moving-01 ev-moving-02; do
  kubectl -n ev-fleet rollout status "deployment/$deployment" --timeout=300s
done
kubectl -n ev-fleet rollout status statefulset/ev-moving-extra --timeout=420s

dashboard_pid=''
simulation_pid=''
cleanup() {
  if [[ -n "$dashboard_pid" ]]; then kill "$dashboard_pid" 2>/dev/null || true; fi
  if [[ -n "$simulation_pid" ]]; then kill "$simulation_pid" 2>/dev/null || true; fi
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
kubectl -n ev-fleet port-forward service/dashboard 8080:80 &
dashboard_pid=$!
kubectl -n ev-fleet port-forward service/simulation-console 8090:8090 &
simulation_pid=$!

ready=false
for attempt in {1..30}; do
  if ! kill -0 "$dashboard_pid" 2>/dev/null || ! kill -0 "$simulation_pid" 2>/dev/null; then
    echo 'Port forwarding failed. Check ports 8080 and 8090.' >&2
    exit 1
  fi
  if curl --fail --silent --max-time 2 http://127.0.0.1:8080/health >/dev/null && \
     curl --fail --silent --max-time 2 http://127.0.0.1:8090/health >/dev/null; then
    ready=true
    break
  fi
  sleep 1
done
if [[ "$ready" != true ]]; then echo 'Dashboard tunnels did not become ready.' >&2; exit 1; fi
echo 'EV rebalancing: http://127.0.0.1:8080'
echo 'Simulation console: http://127.0.0.1:8090'
echo 'Keep this terminal open. Ctrl+C closes tunnels; Kubernetes workloads keep running.'
while kill -0 "$dashboard_pid" 2>/dev/null && kill -0 "$simulation_pid" 2>/dev/null; do sleep 2; done
echo 'A port-forward stopped unexpectedly.' >&2
exit 1
