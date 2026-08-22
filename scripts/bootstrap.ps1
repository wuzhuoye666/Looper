param(
    [string]$Python = "python"
)

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Set-Location $repo
$env:TEMP = Join-Path $repo ".tools\tmp"
$env:TMP = $env:TEMP
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null

$venvPython = Join-Path $repo ".venv\Scripts\python.exe"
$globalUv = Get-Command uv -ErrorAction SilentlyContinue
$needsEnvironment = -not (Test-Path $venvPython)
if (-not $needsEnvironment -and $globalUv) {
    $version = & $venvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
    $needsEnvironment = $LASTEXITCODE -ne 0 -or $version.Trim() -ne "3.12"
}

if ($needsEnvironment) {
    if ($globalUv) {
        $arguments = @("venv", "--python", "3.12")
        if (Test-Path ".venv") { $arguments += "--clear" }
        $arguments += ".venv"
        & $globalUv.Source @arguments
        if ($LASTEXITCODE -ne 0) { throw "Failed to create the Python 3.12 environment" }
    } else {
        & $Python -m venv .venv
        if ($LASTEXITCODE -ne 0) { throw "Failed to create .venv" }
    }
}

& $venvPython -m ensurepip --upgrade --default-pip
if ($LASTEXITCODE -ne 0) { throw "Failed to bootstrap pip in .venv" }

& $venvPython -m pip install --disable-pip-version-check --upgrade pip setuptools wheel
if ($LASTEXITCODE -ne 0) { throw "Failed to upgrade Python packaging tools" }
& $venvPython -m pip install --disable-pip-version-check --requirement requirements.lock
if ($LASTEXITCODE -ne 0) { throw "Failed to install locked Python dependencies" }
& $venvPython -m pip install --disable-pip-version-check --no-deps --editable .
if ($LASTEXITCODE -ne 0) { throw "Failed to install Looper" }
& $venvPython -m pip check
if ($LASTEXITCODE -ne 0) { throw "Python dependency consistency check failed" }

Write-Host "Python environment ready at .venv"
Write-Host "Run 'pnpm install' and then 'pnpm dev'."
