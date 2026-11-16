#!/usr/bin/env bash
# Install or repair Metrics Server for the dashboard HPA on Docker Desktop.
set -euo pipefail
insecure=false
case "${1:-}" in
  '') ;;
  --allow-insecure-kubelet-tls) insecure=true ;;
  *) echo 'Usage: bash scripts/setup-local-metrics.sh [--allow-insecure-kubelet-tls]' >&2; exit 1 ;;
esac
command -v kubectl >/dev/null || { echo 'Required command missing: kubectl' >&2; exit 1; }
k() { kubectl --context docker-desktop -n kube-system "$@"; }

ready=$(kubectl --context docker-desktop get node docker-desktop \
  -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}")
[[ "$ready" == True ]] || { echo 'Docker Desktop Kubernetes node is not Ready.' >&2; exit 1; }

if [[ -z "$(k get deployment metrics-server -o name --ignore-not-found)" ]]; then
  echo 'Installing pinned Metrics Server v0.9.0 on Docker Desktop...'
  kubectl --context docker-desktop apply -f \
    https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml
else
  echo 'Existing Metrics Server detected; preserving its installed version.'
fi

if $insecure; then
  # Local workaround: Docker Desktop kubelet certificates lack IP SANs.
  # Never use on production or shared clusters.
  names=$(k get deployment metrics-server -o jsonpath='{.spec.template.spec.containers[*].name}')
  index=-1; i=0
  for name in $names; do [[ "$name" == metrics-server ]] && index=$i; i=$((i + 1)); done
  (( index >= 0 )) || { echo 'Metrics Server container not found.' >&2; exit 1; }
  args=$(k get deployment metrics-server -o jsonpath="{.spec.template.spec.containers[$index].args[*]}")
  if [[ " $args " != *' --kubelet-insecure-tls '* ]]; then
    echo 'WARNING: local testing only: bypassing Metrics Server kubelet TLS verification.' >&2
    k patch deployment metrics-server --type=json -p \
      "[{\"op\":\"add\",\"path\":\"/spec/template/spec/containers/$index/args/-\",\"value\":\"--kubelet-insecure-tls\"}]"
  fi
fi

k rollout status deployment/metrics-server --timeout=180s
kubectl --context docker-desktop wait --for=condition=Available \
  apiservice/v1beta1.metrics.k8s.io --timeout=180s
echo 'Metrics API available. Verify with: kubectl --context docker-desktop top pods -n ev-fleet'
