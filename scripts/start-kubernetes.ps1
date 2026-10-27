$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Require-Command([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Required command '$Name' was not found. Install Docker Desktop with Kubernetes enabled."
    }
}

function Wait-Rollout([string]$Kind, [string]$Name, [int]$Seconds = 300) {
    & kubectl -n ev-fleet rollout status "$Kind/$Name" "--timeout=${Seconds}s"
    if ($LASTEXITCODE -ne 0) { throw "Rollout failed: $Kind/$Name" }
}

Require-Command docker
Require-Command kubectl

& kubectl config use-context docker-desktop
if ($LASTEXITCODE -ne 0) { throw "Docker Desktop Kubernetes context 'docker-desktop' is unavailable." }

$nodeReady = & kubectl get node docker-desktop -o jsonpath="{.status.conditions[?(@.type=='Ready')].status}"
if ($LASTEXITCODE -ne 0 -or $nodeReady -ne "True") {
    throw "Docker Desktop Kubernetes is not Ready. Enable it in Docker Desktop settings and retry."
}

$dockerContext = (& docker context show).Trim()
if ($dockerContext -ne "desktop-linux") {
    throw "Docker must use the 'desktop-linux' context; current context is '$dockerContext'."
}

Write-Host "Building reproducible local images..." -ForegroundColor Cyan
& docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-backend:k8s-local .
if ($LASTEXITCODE -ne 0) { throw "Backend image build failed." }
& docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-dashboard:k8s-local .\dashboard
if ($LASTEXITCODE -ne 0) { throw "Dashboard image build failed." }
& docker --context desktop-linux build --platform linux/amd64 -t csc3104/ev-fleet-simulation-console:k8s-local .\simulation-console
if ($LASTEXITCODE -ne 0) { throw "Simulation console image build failed." }

Write-Host "Deploying PostgreSQL, Redis, services, dashboards, and 20 EV simulators..." -ForegroundColor Cyan
& kubectl apply -f .\k8s\namespace.yaml
if ($LASTEXITCODE -ne 0) { throw "Namespace creation failed." }
& kubectl wait --for=jsonpath='{.status.phase}'=Active namespace/ev-fleet --timeout=60s
if ($LASTEXITCODE -ne 0) { throw "Namespace did not become active." }
& kubectl apply -k .\k8s
if ($LASTEXITCODE -ne 0) { throw "Kubernetes apply failed." }

Wait-Rollout statefulset postgres
Wait-Rollout deployment redis
foreach ($deployment in @("fleet", "demand", "facilities", "rebalancing", "dispatch", "dashboard", "simulation-console", "ev-moving-01", "ev-moving-02")) {
    Wait-Rollout deployment $deployment
}
Wait-Rollout statefulset ev-moving-extra 420

$notReady = & kubectl -n ev-fleet get pods --field-selector=status.phase!=Running -o name
if ($LASTEXITCODE -ne 0) { throw "Unable to read Pod status." }
if ($notReady) { throw "Some Pods are not running:`n$notReady" }

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
        try { $simulationReady = (Invoke-WebRequest http://127.0.0.1:8090/health -UseBasicParsing -TimeoutSec 2).StatusCode -eq 200 } catch {}
    } until (($dashboardReady -and $simulationReady) -or (Get-Date) -ge $deadline)

    if (-not ($dashboardReady -and $simulationReady)) {
        throw "The application deployed, but one or both dashboard tunnels did not become ready."
    }

    Write-Host "" 
    Write-Host "Complete stack is ready." -ForegroundColor Green
    Write-Host "Operator dashboard:   http://127.0.0.1:8080"
    Write-Host "Simulation dashboard: http://127.0.0.1:8090"
    Write-Host "PostgreSQL state is stored in the postgres-data-postgres-0 PVC."
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
