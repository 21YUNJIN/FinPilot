# FinPilot clean-environment self check (V5 NFR-02 / DEL-02 / SYS-04).
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 decodes BOM-less UTF-8 .ps1 files
# with the ANSI code page (cp936 on a zh-CN host) and would mangle non-ASCII
# literals. All Chinese documentation lives in deploy/README.md and README.md.
#
# Checks performed (each prints PASS/FAIL, exit code = number of failures):
#   1. virtualenv interpreter exists and is Python >= 3.11
#   2. required dependency versions are importable and new enough
#   3. alembic.ini / *.ps1 hygiene: ASCII-only (the locale-codec trap)
#   4. PostgreSQL reachable (TCP + SELECT 1 through the app engine)
#   5. migration chain is at head AND has no diff versus finpilot.models
#   6. seed can be rebuilt from scratch (throwaway DB: create -> migrate -> seed)
#   7. API /healthz returns HTTP 200 with status=ok
#
# Usage (repository root):
#   powershell -ExecutionPolicy Bypass -File deploy\verify_install.ps1 `
#       -DatabaseUrl "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
#
# Useful switches:
#   -SkipSeedProbe   skip the throwaway-database seed probe (faster)
#   -SkipApiProbe    skip starting uvicorn for /healthz
#   -PgBin <dir>     PostgreSQL bin directory (default: FINPILOT_PG_BIN or common paths)

[CmdletBinding()]
param(
    [string]$DatabaseUrl = $env:DATABASE_URL,
    [string]$PgBin = $env:FINPILOT_PG_BIN,
    [int]$ApiPort = 58000,
    [switch]$SkipSeedProbe,
    [switch]$SkipApiProbe,
    [string]$WorkDir
)

$ErrorActionPreference = "Continue"

try {
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch {
    # ignore: only affects console rendering
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
if (-not $WorkDir) {
    $WorkDir = Join-Path $RepoRoot ".run\verify"
}
if (-not (Test-Path $WorkDir)) {
    New-Item -ItemType Directory -Path $WorkDir -Force | Out-Null
}

$script:Failures = 0
$script:Checks = @()

function Add-Check {
    param([string]$Name, [bool]$Ok, [string]$Detail)
    $status = "FAIL"
    if ($Ok) { $status = "PASS" }
    if (-not $Ok) { $script:Failures = $script:Failures + 1 }
    $script:Checks += [pscustomobject]@{ Name = $Name; Status = $status; Detail = $Detail }
    Write-Host ("[{0}] {1} :: {2}" -f $status, $Name, $Detail)
}

function Invoke-Capture {
    # Run a program without stealing the pipeline; returns @{ code; out; err; text }
    param([string]$Exe, [string[]]$Argv, [string]$Tag)
    $outFile = Join-Path $WorkDir ("{0}.out.txt" -f $Tag)
    $errFile = Join-Path $WorkDir ("{0}.err.txt" -f $Tag)
    $quoted = @()
    foreach ($a in $Argv) {
        if ($a -match '\s' -and -not $a.StartsWith('"')) {
            $quoted += ('"' + $a + '"')
        } else {
            $quoted += $a
        }
    }
    $proc = Start-Process -FilePath $Exe -ArgumentList $quoted -NoNewWindow -Wait -PassThru `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $text = ""
    if (Test-Path $outFile) { $text = [System.IO.File]::ReadAllText($outFile) }
    $errText = ""
    if (Test-Path $errFile) { $errText = [System.IO.File]::ReadAllText($errFile) }
    return [pscustomobject]@{ code = $proc.ExitCode; out = $text; err = $errText; all = ($text + "`n" + $errText) }
}

function Test-AsciiOnly {
    param([string]$Path)
    $bytes = [System.IO.File]::ReadAllBytes($Path)
    foreach ($b in $bytes) {
        if ($b -gt 127) { return $false }
    }
    return $true
}

function Find-PgTool {
    param([string]$Name)
    $candidates = @()
    if ($PgBin) { $candidates += (Join-Path $PgBin ($Name + ".exe")) }
    if ($env:FINPILOT_PG_BIN) { $candidates += (Join-Path $env:FINPILOT_PG_BIN ($Name + ".exe")) }
    $candidates += ("G:\finpilot_tools\pgsql\bin\" + $Name + ".exe")
    foreach ($c in $candidates) {
        if (Test-Path $c) { return $c }
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

Write-Host "FinPilot verify_install"
Write-Host ("repository : {0}" -f $RepoRoot)
Write-Host ("interpreter: {0}" -f $Python)
Write-Host ("database   : {0}" -f $DatabaseUrl)
Write-Host ""

# --- 1) interpreter -----------------------------------------------------------
if (-not (Test-Path $Python)) {
    Add-Check "python.venv" $false ("missing interpreter: " + $Python)
} else {
    $r = Invoke-Capture $Python @("-c", "import sys;print('%d.%d.%d' % sys.version_info[:3])") "python_version"
    $verText = $r.out.Trim()
    $ok = $false
    if ($r.code -eq 0 -and $verText -match '^(\d+)\.(\d+)') {
        $major = [int]$Matches[1]
        $minor = [int]$Matches[2]
        if ($major -gt 3 -or ($major -eq 3 -and $minor -ge 11)) { $ok = $true }
    }
    Add-Check "python.venv" $ok ("version=" + $verText + " exit=" + $r.code)
}

# --- 2) dependencies ----------------------------------------------------------
$depProbe = @'
import sys
mods = [
    ("fastapi", "0.110"), ("uvicorn", "0.29"), ("sqlalchemy", "2.0"), ("alembic", "1.13"),
    ("psycopg", "3.1"), ("pydantic", "2.7"), ("pydantic_settings", "2.2"),
    ("httpx", "0.27"), ("yaml", "6.0"), ("pytest", "8.0"),
]
bad = []
lines = []
for name, minimum in mods:
    try:
        mod = __import__(name)
        ver = getattr(mod, "__version__", "0")
    except Exception as exc:
        bad.append("%s:MISSING(%s)" % (name, type(exc).__name__))
        continue
    parts = []
    for chunk in str(ver).split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        parts.append(int(digits) if digits else 0)
    while len(parts) < 3:
        parts.append(0)
    need = [int(p) for p in (minimum.split(".") + ["0", "0"])[:3]]
    ok = tuple(parts[:3]) >= tuple(need)
    lines.append("%s=%s%s" % (name, ver, "" if ok else " <REQUIRED " + minimum))
    if not ok:
        bad.append("%s:%s<MIN%s" % (name, ver, minimum))
print("; ".join(lines))
print("BAD=" + (",".join(bad) if bad else "none"))
'@
$probeFile = Join-Path $WorkDir "dep_probe.py"
[System.IO.File]::WriteAllText($probeFile, $depProbe, (New-Object System.Text.UTF8Encoding($false)))
$r = Invoke-Capture $Python @($probeFile) "deps"
$depOk = ($r.code -eq 0) -and ($r.out -match "BAD=none")
$depDetail = ($r.out -replace "`r?`n", " | ").Trim()
Add-Check "python.dependencies" $depOk $depDetail

# --- 3) ASCII hygiene ---------------------------------------------------------
$asciiTargets = @()
$asciiTargets += (Join-Path $RepoRoot "alembic.ini")
foreach ($p in @((Join-Path $RepoRoot "deploy"), (Join-Path $RepoRoot "scripts"))) {
    if (Test-Path $p) {
        $asciiTargets += (Get-ChildItem -Path $p -Filter "*.ps1" -File | ForEach-Object { $_.FullName })
    }
}
$nonAscii = @()
foreach ($t in $asciiTargets) {
    if (Test-Path $t) {
        if (-not (Test-AsciiOnly $t)) { $nonAscii += (Split-Path -Leaf $t) }
    }
}
$asciiDetail = ("checked=" + $asciiTargets.Count)
if ($nonAscii.Count -gt 0) { $asciiDetail = $asciiDetail + " non_ascii=" + ($nonAscii -join ",") }
Add-Check "config.ascii_only" ($nonAscii.Count -eq 0) $asciiDetail

# --- 4) database connectivity -------------------------------------------------
$pgIsReady = Find-PgTool "pg_isready"
if ($pgIsReady) {
    $r = Invoke-Capture $pgIsReady @("-h", "127.0.0.1", "-p", "55432", "-q") "pg_isready"
    Add-Check "postgres.pg_isready" ($r.code -eq 0) ("exit=" + $r.code + " " + ($r.all -replace "`r?`n", " ").Trim())
} else {
    Add-Check "postgres.pg_isready" $false "pg_isready not found (pass -PgBin)"
}

if (-not $DatabaseUrl) {
    Add-Check "database.connect" $false "no -DatabaseUrl (set DATABASE_URL or pass -DatabaseUrl)"
} else {
    $connectProbe = @'
import sys
from sqlalchemy import create_engine, text
url = sys.argv[1]
engine = create_engine(url, pool_pre_ping=True, future=True)
with engine.connect() as conn:
    version = conn.execute(text("SELECT version()")).scalar_one()
    print(str(version).split(",")[0])
engine.dispose()
'@
    $connectFile = Join-Path $WorkDir "connect_probe.py"
    [System.IO.File]::WriteAllText($connectFile, $connectProbe, (New-Object System.Text.UTF8Encoding($false)))
    $r = Invoke-Capture $Python @($connectFile, $DatabaseUrl) "db_connect"
    Add-Check "database.connect" ($r.code -eq 0) (($r.out + " " + $r.err) -replace "`r?`n", " ").Trim()
}

# --- 5) migration chain -------------------------------------------------------
if ($DatabaseUrl) {
    $r = Invoke-Capture $Python @("-m", "alembic", "-x", ("db_url=" + $DatabaseUrl), "current") "alembic_current"
    $currentText = ($r.out + $r.err)
    $atHead = ($r.code -eq 0) -and ($currentText -match "\(head\)")
    Add-Check "migration.at_head" $atHead (($r.out -replace "`r?`n", " ").Trim() + " exit=" + $r.code)

    $r = Invoke-Capture $Python @("-m", "alembic", "-x", ("db_url=" + $DatabaseUrl), "check") "alembic_check"
    $noDiff = ($r.code -eq 0) -and (($r.out + $r.err) -match "No new upgrade operations detected")
    Add-Check "migration.no_diff_vs_models" $noDiff ("exit=" + $r.code + " " + ($r.out -replace "`r?`n", " ").Trim())
    Write-Host "    (alembic check compares the live schema against finpilot.models.Base.metadata)"
}

# --- 6) seed rebuild probe (throwaway database) --------------------------------
if ($SkipSeedProbe) {
    Add-Check "seed.rebuildable" $true "SKIPPED (-SkipSeedProbe): seed was NOT verified"
} elseif (-not $DatabaseUrl) {
    Add-Check "seed.rebuildable" $false "no -DatabaseUrl"
} else {
    $probeDb = "finpilot_verify_seed"
    $adminUrl = $DatabaseUrl -replace "/[^/]+$", "/postgres"
    $seedProbe = @'
import sys
from sqlalchemy import create_engine, text
admin_url, probe_db, target_url = sys.argv[1], sys.argv[2], sys.argv[3]
admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
with admin.connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % probe_db))
    conn.execute(text('CREATE DATABASE "%s"' % probe_db))
admin.dispose()

from alembic import command
from alembic.config import Config
from argparse import Namespace
from pathlib import Path
repo = Path(sys.argv[4])
cfg = Config(str(repo / "alembic.ini"))
cfg.set_main_option("script_location", str(repo / "migrations"))
cfg.cmd_opts = Namespace(x=["db_url=" + target_url])
command.upgrade(cfg, "head")

from sqlalchemy.orm import sessionmaker
from finpilot.seed import DEFAULT_SEED_JSON, load_seed_file, seed_database
engine = create_engine(target_url, future=True)
factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
with factory() as session:
    counts = seed_database(session, load_seed_file(DEFAULT_SEED_JSON))
with engine.connect() as conn:
    tables = conn.execute(text(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' "
        "AND table_type='BASE TABLE' AND table_name <> 'alembic_version'")).scalar_one()
engine.dispose()
print("tables=%s users=%s billing=%s" % (tables, counts["users"], counts["billing_transactions"]))

admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True)
with admin.connect() as conn:
    conn.execute(text('DROP DATABASE IF EXISTS "%s" WITH (FORCE)' % probe_db))
admin.dispose()
'@
    $seedFile = Join-Path $WorkDir "seed_probe.py"
    [System.IO.File]::WriteAllText($seedFile, $seedProbe, (New-Object System.Text.UTF8Encoding($false)))
    $targetUrl = $DatabaseUrl -replace "/[^/]+$", ("/" + $probeDb)
    $r = Invoke-Capture $Python @($seedFile, $adminUrl, $probeDb, $targetUrl, $RepoRoot) "seed_probe"
    $seedDetail = (($r.out + " " + $r.err) -replace "`r?`n", " ").Trim()
    $seedOk = ($r.code -eq 0) -and ($r.out -match "tables=47 ")
    Add-Check "seed.rebuildable" $seedOk $seedDetail
}

# --- 7) API health check ------------------------------------------------------
if ($SkipApiProbe) {
    Add-Check "api.healthz" $true "SKIPPED (-SkipApiProbe): /healthz was NOT verified"
} elseif (-not $DatabaseUrl) {
    Add-Check "api.healthz" $false "no -DatabaseUrl"
} else {
    $apiOut = Join-Path $WorkDir "api_probe.out.log"
    $apiErr = Join-Path $WorkDir "api_probe.err.log"
    $env:DATABASE_URL = $DatabaseUrl
    $env:APP_MODE = "mock"
    $argv = @("-m", "uvicorn", "finpilot.app:app", "--host", "127.0.0.1", "--port", "$ApiPort")
    $proc = Start-Process -FilePath $Python -ArgumentList $argv -NoNewWindow -PassThru `
        -RedirectStandardOutput $apiOut -RedirectStandardError $apiErr -WorkingDirectory $RepoRoot
    $deadline = (Get-Date).AddSeconds(45)
    $body = $null
    $lastError = ""
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 750
        try {
            $resp = Invoke-WebRequest -Uri ("http://127.0.0.1:{0}/healthz" -f $ApiPort) -UseBasicParsing -TimeoutSec 5
            if ($resp.StatusCode -eq 200) {
                $body = $resp.Content
                break
            }
            $lastError = "status=" + $resp.StatusCode
        } catch {
            $lastError = $_.Exception.Message
        }
        if ($proc.HasExited) { break }
    }
    if (-not $proc.HasExited) {
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    }
    Start-Sleep -Milliseconds 500
    $ok = $false
    $detail = "no response: " + $lastError
    if ($body) {
        $json = $null
        try { $json = $body | ConvertFrom-Json } catch { $json = $null }
        if ($json -and $json.status -eq "ok" -and $json.database.ok) {
            $ok = $true
            $detail = ("HTTP 200 status=ok mode={0} db={1}" -f $json.mode, $json.database.database)
        } else {
            $detail = "HTTP 200 but body not ok: " + $body
        }
    }
    Add-Check "api.healthz" $ok $detail
    $env:DATABASE_URL = $null
}

# --- summary ------------------------------------------------------------------
Write-Host ""
Write-Host "summary"
foreach ($c in $script:Checks) {
    Write-Host ("  {0}  {1}" -f $c.Status, $c.Name)
}
$passed = $script:Checks.Count - $script:Failures
Write-Host ("checks: {0} passed, {1} failed, {2} total" -f $passed, $script:Failures, $script:Checks.Count)
Write-Host ("logs  : {0}" -f $WorkDir)
if ($script:Failures -gt 0) {
    Write-Host "RESULT: FAIL"
    exit $script:Failures
}
Write-Host "RESULT: PASS"
exit 0