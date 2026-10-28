param([switch]$SetupMetricsServer, [switch]$AllowInsecureLocalKubeletTls, [switch]$ConnectOnly)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install Docker Desktop with Kubernetes enabled."
    }
}

function Wait-Rollout([string]$Kind, [string]$Name, [int]$Seconds = 300) {
    & kubectl -n ev-fleet rollout status "$Kind/$Name" "--timeout=${Seconds}s"
    if ($LASTEXITCODE -ne 0) { throw "Rollout failed: $Kind/$Name" }
}

function Assert-SafeToRedeploy {
    # Simulator physical journey state is process-local. Never roll it mid-flight.
    $namespace = (& kubectl get namespace ev-fleet --ignore-not-found -o name) -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the existing ev-fleet namespace." }
    if (-not $namespace) { return } # First deployment.

    $rentalReplicas = (& kubectl -n ev-fleet get deployment customer-rental-demo --ignore-not-found -o jsonpath='{.spec.replicas}') -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect customer-rental-demo." }
    if ($rentalReplicas -and [int]$rentalReplicas -gt 0) {
        throw "Deployment blocked: customer-rental-demo is running. Finish rentals/relocations and pause it from the Simulation Console before redeploying."
    }

    $activeJob = (& kubectl -n ev-fleet get job customer-rental-simulation --ignore-not-found -o jsonpath='{.status.active}') -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the customer rental Job." }
    if ($activeJob -and [int]$activeJob -gt 0) {
        throw "Deployment blocked: the customer rental Job is active. Let it finish before redeploying."
    }

    $fleetExists = (& kubectl -n ev-fleet get deployment fleet --ignore-not-found -o name) -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the Fleet deployment." }
    if ($fleetExists) {
        # Read-only, fail-closed check from the current Fleet Pod; no port-forward needed.
        $check = 'import json,sys,urllib.request; vehicles=json.load(urllib.request.urlopen("http://127.0.0.1:8000/api/v1/vehicles",timeout=8))["vehicles"]; busy=[v["vehicle_id"] for v in vehicles if v.get("active_rental") or v.get("active_claim") or v.get("effective_relocation_state") not in (None,"IDLE") or (v.get("telemetry") or {}).get("charging_state")=="CHARGING" or (v.get("active_charging_task") or {}).get("status")=="IN_PROGRESS"]; print("Deployment blocked; active EV workflows: "+", ".join(busy) if busy else "Fleet confirms no active rentals, relocations or charging"); sys.exit(2 if busy else 0)'
        & kubectl -n ev-fleet exec deployment/fleet -- python -c $check
        if ($LASTEXITCODE -ne 0) {
            throw "Deployment aborted: Fleet is unavailable or has active EV work. Do not restart simulator ownership while a journey may be in progress."
        }
    }
}

Require-Command kubectl
if (-not $ConnectOnly) { Require-Command docker }
if ($AllowInsecureLocalKubeletTls -and -not $SetupMetricsServer) { throw 'TLS workaround requires -SetupMetricsServer.' }
if ($ConnectOnly -and ($SetupMetricsServer -or $AllowInsecureLocalKubeletTls)) { throw '-ConnectOnly cannot be combined with Metrics Server setup switches.' }

& kubectl config use-context docker-desktop
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop Kubernetes context 'docker-desktop' is unavailable." }

$nodeReady = & kubectl get node docker-desktop -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}"
if ($LASTEXITCODE -ne 0 -or $nodeReady -ne "True") {
    throw "Docker Desktop Kubernetes is not Ready. Enable it in Docker Desktop settings and retry."
}

if ($ConnectOnly) {
    Write-Host "Connect-only: no Docker build, Kubernetes apply, or Pod restart." -ForegroundColor Cyan
    foreach ($deployment in @("dashboard", "simulation-console")) {
        Wait-Rollout deployment $deployment 120
    }
} else {
    $dockerContext = (& docker context show).Trim()
    if ($LASTEXITCODE -ne 0 -or $dockerContext -ne "desktop-linux") {
        throw "Docker must use the 'desktop-linux' context; current context is '$dockerContext'."
    }

    # Check BEFORE image builds, and again immediately before applying updates.
    Assert-SafeToRedeploy

    if ($SetupMetricsServer) {
        & (Join-Path $PSScriptRoot "setup-local-metrics.ps1") -AllowInsecureKubeletTls:$AllowInsecureLocalKubeletTls
        if (-not $?) { throw "Metrics Server setup failed." }
    } else {
        Write-Host "Metrics Server setup skipped; HPA needs an existing healthy Metrics API." -ForegroundColor Yellow
    }

    # A unique tag changes each Pod template, unlike repeated :k8s-local builds.
    # Docker Desktop's kubeadm cluster shares the desktop-linux local image store.
    $tag = "local-" + (Get-Date -Format "yyyyMMddHHmmss") + "-" + [guid]::NewGuid().ToString("N").Substring(0,8)
    Write-Host "Building application images with version tag $tag..." -ForegroundColor Cyan
    $images = @(
        @{ Name = "backend"; Directory = "." },
        @{ Name = "dashboard"; Directory = ".\dashboard" },
        @{ Name = "simulation-console"; Directory = ".\simulation-console" }
    )
    foreach ($image in $images) {
        $imageRef = "csc3104/ev-fleet-$($image.Name):$tag"
        & docker --context desktop-linux build --platform linux/amd64 -t $imageRef $image.Directory
        if ($LASTEXITCODE -ne 0) { throw "Build failed: $imageRef; Kubernetes was not updated." }
    }

    Assert-SafeToRedeploy
    Write-Host "Rendering Kubernetes manifests with versioned images..." -ForegroundColor Cyan
    $rendered = (& kubectl kustomize .\k8s) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0 -or -not $rendered) { throw "Kustomize rendering failed; nothing applied." }
    foreach ($image in $images) {
        $originalRef = "csc3104/ev-fleet-$($image.Name):k8s-local"
        $versionedRef = "csc3104/ev-fleet-$($image.Name):$tag"
        if (-not $rendered.Contains($originalRef)) {
            throw "Expected image reference $originalRef not found in Kustomize output. Refusing partial rollout."
        }
        $rendered = $rendered.Replace($originalRef, $versionedRef)
    }
    if ($rendered.Contains(":k8s-local")) { throw "Unversioned image reference remains in rendered manifests." }

    $manifestFile = Join-Path ([IO.Path]::GetTempPath()) ("ev-fleet-deploy-" + [guid]::NewGuid().ToString("N") + ".yaml")
    try {
        [IO.File]::WriteAllText($manifestFile, $rendered + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        & kubectl apply --dry-run=client -f $manifestFile
        if ($LASTEXITCODE -ne 0) { throw "Rendered manifest validation failed; nothing applied." }
        & kubectl apply -f .\k8s\namespace.yaml
        if ($LASTEXITCODE -ne 0) { throw "Namespace creation failed." }
        & kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
        if ($LASTEXITCODE -ne 0) { throw "Namespace did not become active." }
        & kubectl apply -f $manifestFile
        if ($LASTEXITCODE -ne 0) { throw "Kubernetes apply failed. Inspect Pod images and events before retrying." }
    } finally {
        Remove-Item -Path $manifestFile -ErrorAction SilentlyContinue
    }

    # Image references change across *all* backend processes (including EVs),
    # preventing an old Fleet Pod rejecting new simulator telemetry contracts.
    Wait-Rollout statefulset postgres
    Wait-Rollout deployment redis
    foreach ($deployment in @("fleet", "demand", "facilities", "rebalancing", "dispatch", "dashboard", "simulation-console", "ev-moving-01", "ev-moving-02")) {
        Wait-Rollout deployment $deployment
    }
    Wait-Rollout statefulset ev-moving-extra 420
    & kubectl -n ev-fleet get hpa dashboard
    if ($LASTEXITCODE -ne 0) { throw "Dashboard HPA resource is missing." }

    $notReady = & kubectl -n ev-fleet get pods --field-selector=status.phase!=Running -o name
    if ($LASTEXITCODE -ne 0) { throw "Unable to read Pod status." }
    if ($notReady) { throw "Some Pods are not running: "+ ($notReady -join ", ") }
    Write-Host "Kubernetes has rolled out the $tag images. Checking dashboard tunnels..." -ForegroundColor Green
}

Write-Host "Starting both local dashboard tunnels..." -ForegroundColor Cyan
$dashboardJob = Start-Job -ScriptBlock { kubectl -n ev-fleet port-forward service/dashboard 8080:80 }
$simulationJob = Start-Job -ScriptBlock { kubectl -n ev-fleet port-forward service/simulation-console 8090:8090 }

try {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        if ($dashboardJob.State -eq "Failed" -or $simulationJob.State -eq "Failed") {
            throw "A dashboard port-forward failed. Check whether ports 8080 and 8090 are already in use."
        }
        $dashboardReady = $false
        $simulationReady = $false
        try { $dashboardReady = (Invoke-WebRequest http://127.0.0.1:8080/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
        try { $simulationReady = (Invoke-WebRequest http://127.0.0.1:8090/health/live -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
    } until (($dashboardReady -and $simulationReady) -or (Get-Date) -ge $deadline)

    if (-not ($dashboardReady -and $simulationReady)) {
        throw "The application deployed, but one or both dashboard tunnels did not become ready."
    }

    Write-Host "" 
    Write-Host "Complete stack is ready." -ForegroundColor Green
    Write-Host "Operator dashboard:   http://127.0.0.1:8080"
    Write-Host "Simulation dashboard: http://127.0.0.1:8090"
    Write-Host "PostgreSQL state is stored in the postgres-data-postgres-0 PVC."
    Write-Host "Dashboard HPA: kubectl get hpa dashboard -n ev-fleet"
    Write-Host "CPU metrics: kubectl top pods -n ev-fleet"
    Write-Host "Keep this window open. Press Ctrl+C to close both dashboard tunnels."

    while ($dashboardJob.State -eq "Running" -and $simulationJob.State -eq "Running") {
        Start-Sleep -Seconds 2
    }
    throw "A dashboard port-forward stopped unexpectedly."
}
finally {
    Stop-Job $dashboardJob, $simulationJob -ErrorAction SilentlyContinue
    Remove-Job $dashboardJob, $simulationJob -Force -ErrorAction SilentlyContinue
}
