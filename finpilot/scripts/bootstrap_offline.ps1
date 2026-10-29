# FinPilot offline bootstrap (ASCII-only by design).
# Creates .venv from the bundled wheelhouse without any network access.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap_offline.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\bootstrap_offline.ps1 -Python "C:\Python312\python.exe"

param(
    [string]$Python = "",
    [switch]$Force
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

$wheelhouse = Join-Path $root "wheelhouse"
$lock = Join-Path $root "requirements-lock.txt"
$venv = Join-Path $root ".venv"

Write-Host "== FinPilot offline bootstrap ==" -ForegroundColor Cyan
Write-Host "root:       $root"
Write-Host "wheelhouse: $wheelhouse"
Write-Host "lock:       $lock"
Write-Host ""

if (-not (Test-Path $wheelhouse)) { throw "wheelhouse/ not found. This package is incomplete." }
if (-not (Test-Path $lock)) { throw "requirements-lock.txt not found. This package is incomplete." }
$wheelCount = (Get-ChildItem $wheelhouse -Filter *.whl -File).Count
Write-Host "wheels found: $wheelCount"
if ($wheelCount -lt 40) { throw "wheelhouse looks incomplete ($wheelCount wheels)." }

if ($Python -eq "") {
    $candidates = @("python", "python3", "py -3.12")
    foreach ($candidate in $candidates) {
        $parts = $candidate -split ' '
        $exe = $parts[0]
        $cmd = Get-Command $exe -ErrorAction SilentlyContinue
        if ($cmd) {
            $args = @()
            if ($parts.Count -gt 1) { $args += $parts[1..($parts.Count-1)] }
            $version = & $exe @args -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
            if ($version -match '^3\.(1[1-9]|[2-9][0-9])$') {
                $Python = $candidate
                break
            }
        }
    }
}
if ($Python -eq "") {
    throw "Python 3.11+ not found on PATH. Install Python 3.12 (64-bit Windows) or pass -Python <path>."
}
$pyParts = $Python -split ' '
$pyExe = $pyParts[0]
$pyArgs = @()
if ($pyParts.Count -gt 1) { $pyArgs = $pyParts[1..($pyParts.Count-1)] }
$pyVersion = & $pyExe @pyArgs -c "import sys; print(sys.version.split()[0])"
Write-Host "using Python $pyVersion ($Python)"
Write-Host ""

if (Test-Path $venv) {
    if (-not $Force) {
        Write-Host ".venv already exists; reusing it (pass -Force to recreate)." -ForegroundColor Yellow
    } else {
        Write-Host "removing existing .venv (-Force)" -ForegroundColor Yellow
        Remove-Item $venv -Recurse -Force
    }
}
if (-not (Test-Path $venv)) {
    Write-Host "[1/3] creating virtual environment ..."
    & $pyExe @pyArgs -m venv $venv
    if ($LASTEXITCODE -ne 0) { throw "venv creation failed" }
} else {
    Write-Host "[1/3] virtual environment exists, skipped"
}

$venvPy = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $venvPy)) { throw "venv python not found at $venvPy" }

Write-Host "[2/3] installing dependencies from wheelhouse (no network) ..."
& $venvPy -m pip install --no-index --find-links $wheelhouse -r $lock --quiet
if ($LASTEXITCODE -ne 0) { throw "dependency install failed" }

Write-Host "[3/3] installing the finpilot package itself (editable, no build isolation) ..."
& $venvPy -m pip install --no-index --find-links $wheelhouse --no-build-isolation -e $root --quiet
if ($LASTEXITCODE -ne 0) { throw "project install failed" }

Write-Host ""
Write-Host "== verify ==" -ForegroundColor Cyan
& $venvPy -c "import finpilot, fastapi, sqlalchemy, psycopg, jsonschema, cryptography; print('imports: OK')"
& $venvPy -c "from finpilot.orchestration.executor import tool_handlers; print('domain handlers:', len(tool_handlers()))"

Write-Host ""
Write-Host "Offline install finished." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1) copy .env.example to .env and set DATABASE_URL"
Write-Host "  2) $venvPy -m alembic upgrade head"
Write-Host "  3) $venvPy -m finpilot.seed --json data\synthetic\seed_app.json"
Write-Host "  4) powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1"
Write-Host "  5) open http://127.0.0.1:58000/app/  (clients are pre-built, Node.js not required)"