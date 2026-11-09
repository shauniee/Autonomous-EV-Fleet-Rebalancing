param([switch]$AllowInsecureKubeletTls)
$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
if (-not (Get-Command kubectl -ErrorAction SilentlyContinue)) { throw "kubectl not available." }
$ready = & kubectl --context docker-desktop get node docker-desktop -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}"
if ($LASTEXITCODE -ne 0 -or $ready -ne "True") { throw "Docker Desktop Kubernetes node is not Ready." }

$existing = & kubectl --context docker-desktop -n kube-system get deployment metrics-server -o name --ignore-not-found
if ($LASTEXITCODE -ne 0) { throw "Cannot check Metrics Server." }
if (-not $existing) {
    Write-Host "Installing pinned Metrics Server v0.9.0 on Docker Desktop..." -ForegroundColor Cyan
    & kubectl --context docker-desktop apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.9.0/components.yaml"
    if ($LASTEXITCODE -ne 0) { throw "Metrics Server installation failed. Check internet access." }
} else {
    Write-Host "Existing Metrics Server detected; preserving its installed version."
}

if ($AllowInsecureKubeletTls) {
    # Explicit local workaround for Docker Desktop kubelet certificates lacking IP SANs.
    # Never use on production or shared clusters.
    $raw = & kubectl --context docker-desktop -n kube-system get deployment metrics-server -o json
    if ($LASTEXITCODE -ne 0) { throw "Unable to read Metrics Server deployment." }
    $deployment = $raw | ConvertFrom-Json
    $matching = @($deployment.spec.template.spec.containers | Where-Object { $_.name -eq "metrics-server" })
    if ($matching.Count -ne 1) { throw "Metrics Server container not found." }
    $currentArgs = @($matching[0].args)
    if ($currentArgs -notcontains "--kubelet-insecure-tls") {
        Write-Warning "Local testing only: bypassing Metrics Server kubelet TLS verification."
        $patch = @{
            spec = @{
                template = @{
                    spec = @{
                        containers = @(@{ name = "metrics-server"; args = @($currentArgs + "--kubelet-insecure-tls") })
                    }
                }
            }
        } | ConvertTo-Json -Depth 12 -Compress
        $patchFile = Join-Path ([IO.Path]::GetTempPath()) ("ev-metrics-" + [guid]::NewGuid().ToString("N") + ".json")
        try {
            [IO.File]::WriteAllText($patchFile, $patch, ([System.Text.UTF8Encoding]::new($false)))
            & kubectl --context docker-desktop -n kube-system patch deployment metrics-server --type=strategic --patch-file $patchFile
            if ($LASTEXITCODE -ne 0) { throw "Metrics Server patch failed." }
        } finally { Remove-Item $patchFile -ErrorAction SilentlyContinue }
    }
}
& kubectl --context docker-desktop -n kube-system rollout status deployment/metrics-server --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Metrics Server not ready; inspect deployment logs." }
& kubectl --context docker-desktop wait --for=condition=Available apiservice/v1beta1.metrics.k8s.io --timeout=180s
if ($LASTEXITCODE -ne 0) { throw "Metrics API unavailable; inspect kubelet certificate or Metrics Server logs." }
Write-Host "Metrics API available. Verify with: kubectl top pods -n ev-fleet" -ForegroundColor Green
