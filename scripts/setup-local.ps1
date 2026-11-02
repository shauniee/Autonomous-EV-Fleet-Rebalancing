param([switch]$Force)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$venvPath = Join-Path $projectRoot ".venv"
$venvPython = Join-Path $venvPath "Scripts\python.exe"
$markerPath = Join-Path $venvPath ".dependency-source.sha256"

function Invoke-Checked([string]$Executable, [string[]]$Arguments, [string]$FailureMessage) {
    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) { throw $FailureMessage }
}

function Test-Python([string]$Executable) {
    if (-not (Test-Path -LiteralPath $Executable)) { return $false }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = '-c "import sys"'
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { return $false }
        if (-not $process.WaitForExit(10000)) {
            $process.Kill()
            return $false
        }
        return $process.ExitCode -eq 0
    } catch {
        return $false
    } finally {
        $process.Dispose()
    }
}

function Resolve-SystemPython {
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        $startInfo = New-Object System.Diagnostics.ProcessStartInfo
        $startInfo.FileName = $launcher.Source
        $startInfo.Arguments = '-3 -c "import sys; print(sys.executable)"'
        $startInfo.UseShellExecute = $false
        $startInfo.CreateNoWindow = $true
        $startInfo.RedirectStandardOutput = $true
        $startInfo.RedirectStandardError = $true
        $process = New-Object System.Diagnostics.Process
        $process.StartInfo = $startInfo
        try {
            if ($process.Start() -and $process.WaitForExit(10000)) {
                $candidate = $process.StandardOutput.ReadToEnd().Trim()
                if ($process.ExitCode -eq 0 -and $candidate -and (Test-Python $candidate)) {
                    return $candidate
                }
            } elseif (-not $process.HasExited) {
                $process.Kill()
            }
        } finally {
            $process.Dispose()
        }
    }

    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python -and (Test-Python $python.Source)) { return $python.Source }
    throw "No usable Python 3 interpreter was found. Install Python and ensure 'py -3' works."
}

function Remove-BrokenVenv {
    if (-not (Test-Path -LiteralPath $venvPath)) { return }
    $item = Get-Item -LiteralPath $venvPath -Force
    Write-Warning "Removing the unusable project virtual environment at $venvPath."
    if ($item.LinkType) {
        Remove-Item -LiteralPath $venvPath -Force
    } else {
        Remove-Item -LiteralPath $venvPath -Recurse -Force
    }
}

Set-Location $projectRoot
$systemPython = Resolve-SystemPython
$created = $false
if (-not (Test-Python $venvPython)) {
    Remove-BrokenVenv
    Write-Host "Creating project virtual environment..." -ForegroundColor Cyan
    Invoke-Checked $systemPython @("-m", "venv", $venvPath) "Unable to create the project virtual environment."
    if (-not (Test-Python $venvPython)) { throw "The new virtual environment is unusable." }
    $created = $true
}

$requirements = Join-Path $projectRoot "requirements.txt"
$pyproject = Join-Path $projectRoot "pyproject.toml"
if (Test-Path -LiteralPath $requirements) {
    $dependencySource = $requirements
    $installArguments = @("-m", "pip", "install", "-r", $requirements)
} elseif (Test-Path -LiteralPath $pyproject) {
    $dependencySource = $pyproject
    $installArguments = @("-m", "pip", "install", "-e", $projectRoot)
} else {
    throw "Neither requirements.txt nor pyproject.toml exists at the project root."
}

$expectedHash = (Get-FileHash -LiteralPath $dependencySource -Algorithm SHA256).Hash
$installedHash = if (Test-Path -LiteralPath $markerPath) {
    (Get-Content -LiteralPath $markerPath -Raw).Trim()
} else {
    ""
}
if ($Force -or $created -or $installedHash -ne $expectedHash) {
    Write-Host "Installing runtime dependencies from $([IO.Path]::GetFileName($dependencySource))..." -ForegroundColor Cyan
    Invoke-Checked $venvPython $installArguments "Unable to install the project dependencies."
    [IO.File]::WriteAllText($markerPath, $expectedHash + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

Write-Host "Python environment ready: $venvPython" -ForegroundColor Green
