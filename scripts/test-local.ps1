param([switch]$Integration)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$before = Get-Location

function Require-Tool([string]$Name) {
    if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
        throw "Missing '$Name'. See docs/local-testing.md."
    }
}

function Run-Checked([string]$Label, [string]$Executable, [string[]]$Arguments) {
    Write-Host ""
    Write-Host "[TEST] $Label" -ForegroundColor Cyan
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Label failed (exit code $LASTEXITCODE)."
    }
    Write-Host "[PASS] $Label" -ForegroundColor Green
}

try {
    Set-Location (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
    $python = if (Get-Command py -ErrorAction SilentlyContinue) { "py" } else { "python" }
    Require-Tool $python
    Require-Tool "npm.cmd"
    $prefix = if ($python -eq "py") { @("-3") } else { @() }

    # Never accidentally aim the PostgreSQL integration test at a live DB.
    $oldDatabase = [Environment]::GetEnvironmentVariable("TELEMETRY_TEST_DATABASE_URL", "Process")
    [Environment]::SetEnvironmentVariable("TELEMETRY_TEST_DATABASE_URL", $null, "Process")
    try {
        Run-Checked "Python backend tests" $python ($prefix + @("-m", "pytest", "-q"))
    } finally {
        [Environment]::SetEnvironmentVariable("TELEMETRY_TEST_DATABASE_URL", $oldDatabase, "Process")
    }

    if (-not (Test-Path "dashboard/node_modules")) {
        Run-Checked "Install dashboard test dependencies" "npm.cmd" @("--prefix", "dashboard", "ci")
    }
    Run-Checked "Dashboard unit tests" "npm.cmd" @("--prefix", "dashboard", "test", "--", "--run")
    Run-Checked "Dashboard TypeScript/build" "npm.cmd" @("--prefix", "dashboard", "run", "build")

    if (-not (Test-Path "simulation-console/frontend/node_modules")) {
        Run-Checked "Install simulation console dependencies" "npm.cmd" @("--prefix", "simulation-console/frontend", "ci")
    }
    Run-Checked "Simulation console TypeScript/build" "npm.cmd" @("--prefix", "simulation-console/frontend", "run", "build")

    if ($Integration) {
        Require-Tool "docker"
        Require-Tool "act"
        Run-Checked "Docker engine" "docker" @("info", "--format", "{{.ServerVersion}}")
        $runner = @("-P", "ubuntu-latest=catthehacker/ubuntu:act-latest")
        Run-Checked "Disposable PostgreSQL telemetry" "act" (@("workflow_dispatch", "-W", ".github/workflows/phase5c-checks.yml", "-j", "telemetry-postgres") + $runner)
        Run-Checked "Local Docker Compose smoke" "act" (@("workflow_dispatch", "-W", ".github/workflows/phase5c-checks.yml", "-j", "compose-smoke") + $runner)
        Run-Checked "Kubernetes manifest checks" "act" (@("workflow_dispatch", "-W", ".github/workflows/kubernetes-portability.yml", "-j", "manifests") + $runner)
        Write-Host "[PASS] Unit, build, and disposable integration checks passed." -ForegroundColor Green
    } else {
        Write-Host "[PASS] Backend, dashboard and simulation console local checks passed." -ForegroundColor Green
        Write-Host "[NOT RUN] Docker/PostgreSQL/Kubernetes integration. Use -Integration for infrastructure-sensitive changes." -ForegroundColor Yellow
    }
    exit 0
} catch {
    Write-Host "[FAIL] $($_.Exception.Message)" -ForegroundColor Red
    exit 1
} finally {
    Set-Location $before
}
