param([switch]$Force, [switch]$Recreate)

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

function Test-Python([string]$Executable, [switch]$RequireWindowsVenvLayout) {
    if (-not (Test-Path -LiteralPath $Executable)) { return $false }
    $startInfo = New-Object System.Diagnostics.ProcessStartInfo
    $startInfo.FileName = $Executable
    $startInfo.Arguments = if ($RequireWindowsVenvLayout) {
        '-c "import pathlib, sysconfig; raise SystemExit(pathlib.Path(sysconfig.get_path(''scripts'')).name.lower() != ''scripts'')"'
    } else {
        '-c "import sys"'
    }
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

function Test-SystemPythonCandidate([string]$Candidate) {
    if (-not $Candidate) { return $false }
    $candidatePath = [IO.Path]::GetFullPath($Candidate)
    $venvPrefix = [IO.Path]::GetFullPath($venvPath).TrimEnd('\') + '\'
    if ($candidatePath.StartsWith($venvPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if ($candidatePath.IndexOf('\WindowsApps\', [StringComparison]::OrdinalIgnoreCase) -ge 0) {
        return $false
    }
    # POSIX-layout interpreters on Windows can run Python but create bin/python
    # environments that this Windows setup and its callers cannot use.
    return Test-Python $candidatePath -RequireWindowsVenvLayout
}

function Resolve-SystemPython {
    # Prefer a conventional installation. Microsoft Store aliases can launch
    # once yet leave a venv tied to an app path that later becomes inaccessible.
    # Check every PATH match because an activated .venv can legitimately be the
    # first entry while a stable system installation remains available later.
    foreach ($python in @(Get-Command python -All -ErrorAction SilentlyContinue)) {
        $isApplication = $python.CommandType -eq [Management.Automation.CommandTypes]::Application
        if ($isApplication -and (Test-SystemPythonCandidate $python.Source)) {
            return $python.Source
        }
    }

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
                if ($process.ExitCode -eq 0 -and (Test-SystemPythonCandidate $candidate)) {
                    return $candidate
                }
            } elseif (-not $process.HasExited) {
                $process.Kill()
            }
        } finally {
            $process.Dispose()
        }
    }

    throw "No stable Python 3 interpreter was found outside .venv and WindowsApps. Install Python from python.org and ensure 'python' or 'py -3' resolves to it."
}

function Stop-VenvProcesses {
    $venvPrefix = [IO.Path]::GetFullPath($venvPath).TrimEnd('\') + '\'
    $usingVenv = @(Get-Process -ErrorAction SilentlyContinue | Where-Object {
        try {
            $_.Path -and [IO.Path]::GetFullPath($_.Path).StartsWith(
                $venvPrefix, [StringComparison]::OrdinalIgnoreCase)
        } catch {
            $false
        }
    })
    foreach ($process in $usingVenv) {
        Write-Warning "Stopping $($process.Name) process $($process.Id) because it is using the project virtual environment."
        Stop-Process -Id $process.Id -Force -ErrorAction Stop
        Wait-Process -Id $process.Id -Timeout 10 -ErrorAction SilentlyContinue
    }
}

function Remove-VenvDirectory {
    for ($attempt = 1; $attempt -le 5; $attempt++) {
        Stop-VenvProcesses
        try {
            Remove-Item -LiteralPath $venvPath -Recurse -Force -ErrorAction Stop
            return
        } catch {
            if ($attempt -eq 5) { throw }
            Write-Warning "Virtual environment removal attempt $attempt failed; retrying after filesystem handles settle."
            Start-Sleep -Milliseconds (250 * $attempt)
        }
    }
}

function Remove-BrokenVenv {
    if (-not (Test-Path -LiteralPath $venvPath)) { return }
    $expectedVenvPath = [IO.Path]::GetFullPath((Join-Path $projectRoot ".venv")).TrimEnd('\')
    $actualVenvPath = [IO.Path]::GetFullPath($venvPath).TrimEnd('\')
    if (-not $actualVenvPath.Equals($expectedVenvPath, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove unexpected virtual environment path: $actualVenvPath"
    }
    $item = Get-Item -LiteralPath $venvPath -Force
    Write-Warning "Removing the project virtual environment at $venvPath."
    if ($item.LinkType) {
        Remove-Item -LiteralPath $venvPath -Force
    } else {
        Remove-VenvDirectory
    }
}

Set-Location $projectRoot
$systemPython = Resolve-SystemPython
$created = $false
if ($Recreate) {
    Remove-BrokenVenv
}
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
    $installArguments = @("-m", "pip", "install", "-e", "${projectRoot}[test]")
} else {
    throw "Neither requirements.txt nor pyproject.toml exists at the project root."
}

$hasher = [Security.Cryptography.SHA256]::Create()
try {
    $digest = $hasher.ComputeHash([IO.File]::ReadAllBytes($dependencySource))
    $expectedHash = [BitConverter]::ToString($digest).Replace("-", "")
} finally {
    $hasher.Dispose()
}
$installedHash = if (Test-Path -LiteralPath $markerPath) {
    (Get-Content -LiteralPath $markerPath -Raw).Trim()
} else {
    ""
}
if ($Force -or $created -or $installedHash -ne $expectedHash) {
    Write-Host "Installing runtime dependencies from $([IO.Path]::GetFileName($dependencySource))..." -ForegroundColor Cyan
    if ($dependencySource -eq $pyproject) {
        # Python 3.10 can seed a venv with pip 21, which cannot perform this
        # repository's PEP 660 editable pyproject install.
        Invoke-Checked $venvPython @("-m", "pip", "install", "--upgrade", "pip>=23", "setuptools>=68") "Unable to prepare pip for the editable project install."
    }
    Invoke-Checked $venvPython $installArguments "Unable to install the project dependencies."
    [IO.File]::WriteAllText($markerPath, $expectedHash + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
}

Write-Host "Python environment ready: $venvPython" -ForegroundColor Green
