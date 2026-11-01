param([switch]$SetupMetricsServer, [switch]$AllowInsecureLocalKubeletTls, [switch]$ConnectOnly, [switch]$CheckPostgresOnly)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
Set-Location (Resolve-Path (Join-Path $PSScriptRoot ".."))

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install Docker Desktop with Kubernetes enabled."
    }
}

function Resolve-Python {
    $venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) { return @{ Executable = $venvPython; Prefix = @() } }
    if (Get-Command py -ErrorAction SilentlyContinue) { return @{ Executable = "py"; Prefix = @("-3") } }
    if (Get-Command python -ErrorAction SilentlyContinue) { return @{ Executable = "python"; Prefix = @() } }
    throw "Python is required to validate and render the whitelisted .env integration settings."
}

function Render-EnvArtifact([hashtable]$Python, [string[]]$Mode = @()) {
    $arguments = @($Python.Prefix) + @("scripts\render_facilities_secret.py", ".env") + $Mode
    $output = (& $Python.Executable @arguments) -join [Environment]::NewLine
    if ($LASTEXITCODE -ne 0 -or -not $output) {
        throw "Unable to validate and render integration settings from .env."
    }
    return $output
}

function Apply-RenderedSecret([string]$Manifest, [string]$Name) {
    $Manifest | & kubectl apply -f -
    if ($LASTEXITCODE -ne 0) { throw "Unable to apply Kubernetes Secret '$Name'." }
}

function Wait-Rollout([string]$Kind, [string]$Name, [int]$Seconds = 300) {
    & kubectl -n ev-fleet rollout status "$Kind/$Name" "--timeout=${Seconds}s"
    if ($LASTEXITCODE -ne 0) {
        if ($Kind -eq "statefulset" -and $Name -eq "postgres") {
            Write-Warning "PostgreSQL did not become ready: check the app DB and Pod-network password authentication before restarting Fleet. Recent PostgreSQL logs:"
            & kubectl -n ev-fleet logs postgres-0 -c postgres --tail=35
            Write-Warning "PostgreSQL PVC has NOT been deleted or reset."
        }
        throw "Rollout failed: $Kind/$Name"
    }
}

function Assert-ExistingPostgresHealthy {
    # The Pod IP from Kubernetes is authoritative. The old hostname -i probe
    # can return a different address and fail even when Postgres readiness works.
    # A missing database or authentication rule MUST still fail closed.
    $namespace = (& kubectl get namespace ev-fleet --ignore-not-found -o name) -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect the ev-fleet namespace." }
    if (-not $namespace) { return } # Fresh install.
    $postgres = (& kubectl -n ev-fleet get statefulset postgres --ignore-not-found -o name) -join ""
    if ($LASTEXITCODE -ne 0) { throw "Unable to inspect PostgreSQL StatefulSet." }
    if (-not $postgres) { return } # Fresh install.

    $podIp = (& kubectl -n ev-fleet get pod postgres-0 -o jsonpath='{.status.podIP}') -join ""
    if ($LASTEXITCODE -ne 0 -or -not $podIp) {
        throw "PostgreSQL preflight cannot find the postgres-0 Pod IP. Inspect: kubectl -n ev-fleet get pods -o wide. No storage was changed."
    }
    if ($podIp -notmatch '^[0-9a-fA-F.:]+$') {
        throw "PostgreSQL returned an unexpected Pod IP; refusing to construct a shell probe."
    }

    # Avoid sending an encoded shell pipeline through Windows PowerShell's
    # legacy native-argument quoting. The only kubectl exec payload is a
    # single portable sh -c argument with no nested quotes or shell pipes.
    # PostgreSQL password is read ONLY from the container's environment.
    # SELECT/**/1 is PostgreSQL's whitespace-free equivalent of SELECT 1.
    $sqlCheck = 'PGPASSWORD=$POSTGRES_PASSWORD PGCONNECT_TIMEOUT=5 psql -X -qAt -w -h __POD_IP__ -U $POSTGRES_USER -d $POSTGRES_DB -c SELECT/**/1'
    $sqlCheck = $sqlCheck.Replace('__POD_IP__', $podIp)
    Write-Host "Testing PostgreSQL TCP authentication to Pod IP $podIp (read-only)..." -ForegroundColor Cyan
    $sqlOutput = (& kubectl -n ev-fleet exec postgres-0 -c postgres -- sh -c $sqlCheck) -join ""
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL Pod-IP preflight failed. The psql or kubectl diagnostic above identifies the actual problem (for example wrong POSTGRES_PASSWORD, missing database or pg_hba.conf rule). The interactive -W test uses a manually typed password and does NOT verify that the Secret's POSTGRES_PASSWORD matches. Check the current Kubernetes Secret and persistent database credentials without exposing passwords. No PVC data was changed."
    }
    if ($sqlOutput.Trim() -ne '1') {
        throw "PostgreSQL Pod-IP preflight returned an unexpected SQL response (expected 1). Output: $sqlOutput. No PVC data was changed."
    }
    Write-Host "Authenticated PostgreSQL Pod-IP SQL check passed." -ForegroundColor Green
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
        # Encode the inline read-only probe to avoid Windows PowerShell 5.1
        # swallowing nested quotation marks when passing python -c through kubectl.
        $probe = @'
import json
import sys
import urllib.request

with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/vehicles", timeout=8) as reply:
    vehicles = json.load(reply)["vehicles"]
busy = [
    vehicle["vehicle_id"]
    for vehicle in vehicles
    if vehicle.get("active_rental")
    or vehicle.get("active_claim")
    or vehicle.get("effective_relocation_state") not in (None, "IDLE")
    or (vehicle.get("telemetry") or {}).get("charging_state") == "CHARGING"
    or (vehicle.get("active_charging_task") or {}).get("status") == "IN_PROGRESS"
]
if busy:
    print("Deployment blocked; active EV workflows: " + ", ".join(busy), file=sys.stderr)
    sys.exit(2)
print("Fleet confirms no active rentals, relocations or charging.")
'@
        $encodedProbe = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes($probe))
        & kubectl -n ev-fleet exec deployment/fleet -- python -c "import base64;exec(base64.b64decode('$encodedProbe'))"
        if ($LASTEXITCODE -ne 0) {
            throw "Deployment aborted: Fleet is unavailable or has active EV work. Do not restart simulator ownership while a journey may be in progress."
        }
    }
}

Require-Command kubectl
if (-not ($ConnectOnly -or $CheckPostgresOnly)) { Require-Command docker }
if ($AllowInsecureLocalKubeletTls -and -not $SetupMetricsServer) { throw 'TLS workaround requires -SetupMetricsServer.' }
if ($ConnectOnly -and ($SetupMetricsServer -or $AllowInsecureLocalKubeletTls)) { throw '-ConnectOnly cannot be combined with Metrics Server setup switches.' }
if ($CheckPostgresOnly -and ($ConnectOnly -or $SetupMetricsServer -or $AllowInsecureLocalKubeletTls)) { throw '-CheckPostgresOnly cannot be combined with other startup modes.' }

& kubectl config use-context docker-desktop
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop Kubernetes context 'docker-desktop' is unavailable." }

$nodeReady = & kubectl get node docker-desktop -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}"
if ($LASTEXITCODE -ne 0 -or $nodeReady -ne "True") {
    throw "Docker Desktop Kubernetes is not Ready. Enable it in Docker Desktop settings and retry."
}

if ($CheckPostgresOnly) {
    $postgres = (& kubectl -n ev-fleet get statefulset postgres --ignore-not-found -o name) -join ""
    if ($LASTEXITCODE -ne 0 -or -not $postgres) { throw "No existing PostgreSQL StatefulSet to check in namespace ev-fleet." }
    Assert-ExistingPostgresHealthy
    Write-Host "PostgreSQL-only check passed. No images, Pods, or persistent volumes were changed." -ForegroundColor Green
    return
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

    # Fail early on incomplete databases, before checking Fleet or building images.
    Assert-ExistingPostgresHealthy
    # Check BEFORE image builds, and again immediately before applying updates.
    Assert-SafeToRedeploy

    # Validate before expensive builds. Secret JSON stays in process memory and
    # is piped directly to kubectl later; it is never written to disk or logged.
    $python = Resolve-Python
    $facilitiesSecret = Render-EnvArtifact $python
    $routingSecret = Render-EnvArtifact $python @("--routing")
    $trafficControlSecret = Render-EnvArtifact $python @("--traffic-control")
    $trafficConfigPatch = Render-EnvArtifact $python @("--traffic-config")

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
        # Keep the fixed local alias for the optional standalone rental Job manifest.
        $legacyRef = "csc3104/ev-fleet-$($image.Name):k8s-local"
        & docker --context desktop-linux build --platform linux/amd64 -t $imageRef -t $legacyRef $image.Directory
        if ($LASTEXITCODE -ne 0) { throw "Build failed: $imageRef; Kubernetes was not updated." }
    }

    Assert-ExistingPostgresHealthy
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
    $trafficPatchFile = Join-Path ([IO.Path]::GetTempPath()) ("ev-fleet-traffic-patch-" + [guid]::NewGuid().ToString("N") + ".json")
    try {
        [IO.File]::WriteAllText($manifestFile, $rendered + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        # This patch contains only the provider/enable flags, never credentials.
        # A file avoids Windows PowerShell 5.1 altering nested JSON quotes.
        [IO.File]::WriteAllText($trafficPatchFile, $trafficConfigPatch + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
        & kubectl apply -f .\k8s\namespace.yaml
        if ($LASTEXITCODE -ne 0) { throw "Namespace creation failed." }
        & kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
        if ($LASTEXITCODE -ne 0) { throw "Namespace did not become active." }
        & kubectl apply --dry-run=client -f $manifestFile
        if ($LASTEXITCODE -ne 0) { throw "Rendered manifest validation failed; nothing else applied." }
        & kubectl apply -f $manifestFile
        if ($LASTEXITCODE -ne 0) { throw "Kubernetes apply failed. Inspect Pod images and events before retrying." }

        Apply-RenderedSecret $facilitiesSecret "ev-fleet-facilities"
        Apply-RenderedSecret $routingSecret "ev-fleet-routing"
        Apply-RenderedSecret $trafficControlSecret "ev-fleet-traffic-control"
        & kubectl -n ev-fleet patch configmap ev-fleet-config --type merge --patch-file $trafficPatchFile
        if ($LASTEXITCODE -ne 0) { throw "Unable to apply the traffic provider configuration." }

        # ConfigMap/Secret environment values are read only when a container
        # starts. Restart every consumer after the validated settings exist.
        foreach ($workload in @(
            "deployment/facilities", "deployment/rebalancing", "deployment/dispatch",
            "deployment/simulation-console", "deployment/ev-moving-01",
            "deployment/ev-moving-02", "statefulset/ev-moving-extra"
        )) {
            & kubectl -n ev-fleet rollout restart $workload
            if ($LASTEXITCODE -ne 0) { throw "Unable to restart $workload after integration settings changed." }
        }
    } finally {
        Remove-Item -Path $manifestFile -ErrorAction SilentlyContinue
        Remove-Item -Path $trafficPatchFile -ErrorAction SilentlyContinue
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

function Test-LocalEndpoint([string]$Url) {
    try {
        return (Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200
    } catch {
        return $false
    }
}

Write-Host "Opening local dashboard tunnels (reusing healthy existing tunnels)..." -ForegroundColor Cyan
$dashboardJob = $null
$simulationJob = $null
if (Test-LocalEndpoint "http://127.0.0.1:8080/health") {
    Write-Host "Reusing operator dashboard at port 8080."
} else {
    $dashboardJob = Start-Job -ScriptBlock { kubectl -n ev-fleet port-forward service/dashboard 8080:80 }
}
if (Test-LocalEndpoint "http://127.0.0.1:8090/health/live") {
    Write-Host "Reusing simulation dashboard at port 8090."
} else {
    $simulationJob = Start-Job -ScriptBlock { kubectl -n ev-fleet port-forward service/simulation-console 8090:8090 }
}

try {
    $deadline = (Get-Date).AddSeconds(30)
    do {
        Start-Sleep -Milliseconds 500
        if (($dashboardJob -and $dashboardJob.State -in @("Failed", "Completed", "Stopped")) -or
            ($simulationJob -and $simulationJob.State -in @("Failed", "Completed", "Stopped"))) {
            throw "A dashboard port-forward failed. Inspect the jobs and verify ports 8080 and 8090 are available."
        }
        $dashboardReady = Test-LocalEndpoint "http://127.0.0.1:8080/health"
        $simulationReady = Test-LocalEndpoint "http://127.0.0.1:8090/health/live"
    } until (($dashboardReady -and $simulationReady) -or (Get-Date) -ge $deadline)

    if (-not ($dashboardReady -and $simulationReady)) {
        throw "One or both dashboard tunnels are unavailable. Check port-forward errors."
    }

    Write-Host ""
    Write-Host "EV Fleet dashboards are ready." -ForegroundColor Green
    Write-Host "Operator dashboard:   http://127.0.0.1:8080"
    Write-Host "Simulation dashboard: http://127.0.0.1:8090"
    Write-Host "PostgreSQL state remains on the postgres-data-postgres-0 PVC."
    Write-Host "Dashboard HPA: kubectl get hpa dashboard -n ev-fleet"
    Write-Host "CPU metrics: kubectl top pods -n ev-fleet"

    if (-not $dashboardJob -and -not $simulationJob) {
        Write-Host "Reusing existing tunnels; no terminal needs to stay open for this command."
        return
    }
    Write-Host "Keep this window open for newly created tunnels; Ctrl+C closes only those tunnels."
    while ($true) {
        if (($dashboardJob -and $dashboardJob.State -in @("Failed", "Completed", "Stopped")) -or
            ($simulationJob -and $simulationJob.State -in @("Failed", "Completed", "Stopped"))) {
            throw "A dashboard port-forward stopped unexpectedly."
        }
        Start-Sleep -Seconds 2
    }
}
finally {
    foreach ($job in @($dashboardJob, $simulationJob)) {
        if ($null -ne $job) {
            Stop-Job $job -ErrorAction SilentlyContinue
            Remove-Job $job -Force -ErrorAction SilentlyContinue
        }
    }
}
