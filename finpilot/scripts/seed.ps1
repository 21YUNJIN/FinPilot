# FinPilot seed loader wrapper (V5 DATA-03 / GOV-02). ASCII-ONLY ON PURPOSE.
#
# Loads data\synthetic\seed_app.json (or the built-in demo seed) into the target
# database. The loader is idempotent: existing rows are skipped, so re-running only
# fills what is missing.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\seed.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\seed.ps1 -Demo
#   powershell -ExecutionPolicy Bypass -File scripts\seed.ps1 -DatabaseUrl "postgresql+psycopg://..."
#   powershell -ExecutionPolicy Bypass -File scripts\seed.ps1 -Reset      # drop_all + create_all (DEV ONLY)
#
# -Reset calls finpilot.seed --reset, which runs Base.metadata.drop_all/create_all.
# That is a development shortcut; production must use the Alembic chain instead:
#   python -m alembic -x db_url=... downgrade base
#   python -m alembic -x db_url=... upgrade head

[CmdletBinding()]
param(
    [string]$DatabaseUrl = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot",
    [string]$SeedJson = "data/synthetic/seed_app.json",
    [switch]$Demo,
    [switch]$Reset
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$LogDir = Join-Path $RepoRoot ".run\logs"
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir -Force | Out-Null }

if (-not (Test-Path $Python)) {
    Write-Host ("FATAL: missing interpreter " + $Python)
    exit 1
}

$argv = @("-m", "finpilot.seed", "--database-url", $DatabaseUrl)
if ($Demo) { $argv += "--demo" } else { $argv += @("--json", $SeedJson) }
if ($Reset) { $argv += "--reset" }

Write-Host ("seed -> " + $DatabaseUrl)
if ($Reset) { Write-Host "  WARN: -Reset rebuilds the schema with drop_all/create_all (development only)" }

$out = Join-Path $LogDir "seed.out.log"
$err = Join-Path $LogDir "seed.err.log"
$proc = Start-Process -FilePath $Python -ArgumentList $argv -WorkingDirectory $RepoRoot `
    -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $out -RedirectStandardError $err

if (Test-Path $out) { Get-Content $out -Encoding UTF8 | Out-String | Write-Host }
if ($proc.ExitCode -ne 0) {
    if (Test-Path $err) { Get-Content $err -Encoding UTF8 | Out-String | Write-Host }
    Write-Host ("RESULT: FAIL exit=" + $proc.ExitCode)
    exit $proc.ExitCode
}
Write-Host "RESULT: OK"
exit 0