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
    $venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    $python = if (Test-Path $venvPython) {
        $venvPython
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        "py"
    } else {
        "python"
    }
    Require-Tool $python
    Require-Tool "npm.cmd"
    $prefix = if ($python -eq "py") { @("-3") } else { @() }

    # Isolate local Python tests from configured PostgreSQL databases.
    # Never accidentally run default tests against the live EV Fleet database.

    $oldDatabase = [Environment]::GetEnvironmentVariable(
        "TELEMETRY_TEST_DATABASE_URL", "Process"
    )

    $oldDatabaseUrl = [Environment]::GetEnvironmentVariable(
        "DATABASE_URL", "Process"
    )
    $oldPytestOptions = [Environment]::GetEnvironmentVariable("PYTEST_ADDOPTS", "Process")

    try {
        # Temporarily remove database URLs for the default Python test suite.
        [Environment]::SetEnvironmentVariable(
            "TELEMETRY_TEST_DATABASE_URL", $null, "Process"
        )

        [Environment]::SetEnvironmentVariable(
            "DATABASE_URL", $null, "Process"
        )
        [Environment]::SetEnvironmentVariable("PYTEST_ADDOPTS", $null, "Process")

        Run-Checked "Python backend tests" $python (
            $prefix + @("-m", "pytest", "-q")
        )

    } finally {
        # Restore the original environment even if tests fail.
        [Environment]::SetEnvironmentVariable(
            "TELEMETRY_TEST_DATABASE_URL", $oldDatabase, "Process"
        )

        [Environment]::SetEnvironmentVariable(
            "DATABASE_URL", $oldDatabaseUrl, "Process"
        )
        [Environment]::SetEnvironmentVariable("PYTEST_ADDOPTS", $oldPytestOptions, "Process")
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
        Run-Checked "Disposable PostgreSQL telemetry" "act" (@("workflow_dispatch", "-W", ".github/workflows/infrastructure-checks.yml", "-j", "telemetry-postgres") + $runner)
        Run-Checked "Local Docker Compose smoke" "act" (@("workflow_dispatch", "-W", ".github/workflows/infrastructure-checks.yml", "-j", "compose-smoke") + $runner)
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
