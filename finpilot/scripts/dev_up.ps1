# FinPilot one-command local start (V5 DEL-02 / SYS-04).
#
# ASCII-ONLY ON PURPOSE: Windows PowerShell 5.1 decodes BOM-less UTF-8 .ps1 with the
# ANSI code page and mangles non-ASCII literals. Chinese docs: deploy/README.md.
#
# Starts, in order, with every child process redirected to .run\logs (never holding
# the parent pipeline -- pg_ctl in particular must not inherit the console pipe):
#   1. PostgreSQL (pg_ctl start) unless 127.0.0.1:55432 already accepts connections
#   2. alembic upgrade head          (DATA-04; schema at head before serving)
#   3. finpilot.seed                 (re-creatable seed; idempotent)
#   4. api             (uvicorn finpilot.app:app)            -> 127.0.0.1:58000
#   5. worker-outbox   (scripts\worker_outbox.py)
#   6. worker-scheduler(scripts\worker_scheduler.py)
#   7. bank-sandbox    (uvicorn bank:app)                    -> 127.0.0.1:58101
#   8. merchant-sandbox(uvicorn merchant:app)                -> 127.0.0.1:58102
#
# Usage (repository root):
#   powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 -Status
#   powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 -SkipSeed -SkipMigrate

[CmdletBinding()]
param(
    [switch]$Status,
    [switch]$SkipMigrate,
    [switch]$SkipSeed,
    [switch]$Seed,
    [switch]$SkipDatabase,
    [string]$PgBin = $env:FINPILOT_PG_BIN,
    [string]$PgData = "G:\finpilot_tools\pgdata",
    [string]$PgLog = "G:\finpilot_tools\postgres.log",
    [string]$DatabaseUrl = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot",
    [int]$ApiPort = 58000,
    [int]$BankPort = 58101,
    [int]$MerchantPort = 58102,
    [int]$HealthTimeoutSeconds = 60
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $RepoRoot ".venv\Scripts\python.exe"
$RunDir = Join-Path $RepoRoot ".run"
$LogDir = Join-Path $RunDir "logs"
$StateDir = Join-Path $RepoRoot ".state"
$PidFile = Join-Path $RunDir "pids.json"

foreach ($d in @($RunDir, $LogDir, $StateDir)) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

if (-not $PgBin) { $PgBin = "G:\finpilot_tools\pgsql\bin" }

function Get-PgTool {
    param([string]$Name)
    $candidate = Join-Path $PgBin ($Name + ".exe")
    if (Test-Path $candidate) { return $candidate }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    return $null
}

function Test-HttpOk {
    param([string]$Url, [int]$TimeoutSec = 3)
    try {
        $resp = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Wait-Http {
    param([string]$Url, [int]$Seconds, [string]$Label)
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        if (Test-HttpOk $Url) { return $true }
        Start-Sleep -Milliseconds 700
    }
    Write-Host ("  WARN {0} not healthy after {1}s: {2}" -f $Label, $Seconds, $Url)
    return $false
}

function Wait-PgReady {
    param([int]$Seconds)
    $pgIsReady = Get-PgTool "pg_isready"
    if (-not $pgIsReady) { return $false }
    $deadline = (Get-Date).AddSeconds($Seconds)
    while ((Get-Date) -lt $deadline) {
        & $pgIsReady -h 127.0.0.1 -p 55432 -q | Out-Null
        if ($LASTEXITCODE -eq 0) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-Managed {
    # Start a child process fully detached from our stdio; returns the PID.
    #
    # Why not Start-Process -RedirectStandardOutput: on Windows that uses
    # CreateProcess with bInheritHandles=TRUE, so the long-lived service inherits the
    # caller's stdout pipe and keeps it open until the service exits -- a piped or CI
    # invocation of this script then hangs after printing RESULT: UP (observed here).
    # scripts\launch.py spawns with close_fds=True (no inherited handles) and prints PID.
    param([string]$Key, [string[]]$Argv, [string]$WorkingDir)
    $out = Join-Path $LogDir ($Key + ".out.log")
    $err = Join-Path $LogDir ($Key + ".err.log")
    $pidFile = Join-Path $RunDir ($Key + ".pid")
    if (Test-Path $pidFile) { Remove-Item $pidFile -Force -ErrorAction SilentlyContinue }
    $launchArgs = @((Join-Path $RepoRoot "scripts\launch.py"), "--out", $out, "--err", $err, "--cwd", $WorkingDir, "--pid-file", $pidFile, "--", $Python) + $Argv
    $outTmp = Join-Path $LogDir ($Key + ".launch.out.txt")
    $errTmp = Join-Path $LogDir ($Key + ".launch.err.txt")
    # NOTE: no -Wait. Waiting on the launcher also waits on the (long-lived) service it
    # spawned, which is what made this script hang; poll the PID file instead.
    $null = Start-Process -FilePath $Python -ArgumentList $launchArgs -WorkingDirectory $WorkingDir `
        -WindowStyle Hidden -PassThru -RedirectStandardOutput $outTmp -RedirectStandardError $errTmp
    $deadline = (Get-Date).AddSeconds(20)
    $childPid = 0
    while ((Get-Date) -lt $deadline) {
        if (Test-Path $pidFile) {
            $text = (Get-Content $pidFile -Raw -Encoding ASCII).Trim()
            if ($text -match '^\d+$') { $childPid = [int]$text; break }
        }
        Start-Sleep -Milliseconds 200
    }
    if ($childPid -eq 0) {
        $probe = ""
        if (Test-Path $outTmp) { $probe = (Get-Content $outTmp -Raw -Encoding UTF8).Trim() }
        Write-Host ("  FATAL: could not launch {0}; launcher said: {1} (see {2})" -f $Key, $probe, $errTmp)
        exit 1
    }
    Write-Host ("  started {0} pid={1} log={2}" -f $Key, $childPid, $out)
    return $childPid
}

if ($Status) {
    Write-Host "FinPilot local status"
    if (Test-Path $PidFile) {
        $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
        foreach ($prop in $pids.PSObject.Properties) {
            if ($prop.Name -eq "started_at") { continue }
            $proc = Get-Process -Id ([int]$prop.Value) -ErrorAction SilentlyContinue
            $state = "stopped"
            if ($proc) { $state = "running" }
            Write-Host ("  {0,-18} pid={1,-8} {2}" -f $prop.Name, $prop.Value, $state)
        }
    } else {
        Write-Host "  no .run\pids.json (never started from this checkout)"
    }
    $pgIsReady = Get-PgTool "pg_isready"
    if ($pgIsReady) {
        & $pgIsReady -h 127.0.0.1 -p 55432 -q | Out-Null
        Write-Host ("  postgres           exit=" + $LASTEXITCODE)
    }
    Write-Host ("  api /healthz       " + (Test-HttpOk ("http://127.0.0.1:{0}/healthz" -f $ApiPort)))
    Write-Host ("  bank /bank/health  " + (Test-HttpOk ("http://127.0.0.1:{0}/bank/health" -f $BankPort)))
    Write-Host ("  merchant /health   " + (Test-HttpOk ("http://127.0.0.1:{0}/merchant/health" -f $MerchantPort)))
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Host ("FATAL: missing interpreter " + $Python)
    Write-Host "Create it with: python -m venv .venv ; .\.venv\Scripts\python.exe -m pip install -e ."
    exit 1
}

$resolved = @{}

# --- 1) PostgreSQL ------------------------------------------------------------
if ($SkipDatabase) {
    Write-Host "[1/8] PostgreSQL: skipped (-SkipDatabase)"
} else {
    Write-Host "[1/8] PostgreSQL"
    if (Wait-PgReady 2) {
        Write-Host "  already accepting connections on 127.0.0.1:55432"
    } else {
        $pgCtl = Get-PgTool "pg_ctl"
        if (-not $pgCtl) {
            Write-Host ("  FATAL: pg_ctl not found (PgBin=" + $PgBin + "); pass -PgBin")
            exit 1
        }
        if (-not (Test-Path $PgData)) {
            Write-Host ("  FATAL: data directory not found: " + $PgData)
            exit 1
        }
        $ctlOut = Join-Path $LogDir "pg_ctl.out.txt"
        $ctlErr = Join-Path $LogDir "pg_ctl.err.txt"
        $null = Start-Process -FilePath $pgCtl `
            -ArgumentList @("-D", $PgData, "-l", $PgLog, "start") `
            -RedirectStandardOutput $ctlOut -RedirectStandardError $ctlErr `
            -WindowStyle Hidden -PassThru -Wait
        if (Wait-PgReady 45) {
            Write-Host "  started (pg_ctl)"
        } else {
            Write-Host "  FATAL: PostgreSQL did not become ready; see $PgLog and $ctlErr"
            exit 1
        }
    }
}

$env:DATABASE_URL = $DatabaseUrl
$env:APP_MODE = "mock"
$env:ENVIRONMENT = "local"
$env:BANK_SANDBOX_BASE_URL = ("http://127.0.0.1:{0}" -f $BankPort)
$env:MERCHANT_SANDBOX_BASE_URL = ("http://127.0.0.1:{0}" -f $MerchantPort)
$env:BANK_SANDBOX_STATE = (Join-Path $StateDir "bank_sandbox.sqlite3")
$env:MERCHANT_SANDBOX_STATE = (Join-Path $StateDir "merchant_sandbox.sqlite3")
$env:PROVIDER_CALLBACK_BASE_URL = ("http://127.0.0.1:{0}" -f $ApiPort)

# --- 2) migrations ------------------------------------------------------------
if ($SkipMigrate) {
    Write-Host "[2/8] migrations: skipped (-SkipMigrate)"
} else {
    Write-Host "[2/8] alembic upgrade head"
    $out = Join-Path $LogDir "migrate.out.log"
    $err = Join-Path $LogDir "migrate.err.log"
    $p = Start-Process -FilePath $Python -ArgumentList @("-m", "alembic", "-x", ("db_url=" + $DatabaseUrl), "upgrade", "head") `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput $out -RedirectStandardError $err
    if ($p.ExitCode -ne 0) {
        Write-Host ("  FATAL: alembic upgrade head failed (exit=" + $p.ExitCode + "); see " + $err)
        exit 1
    }
    Write-Host "  schema at head"
}

# --- 3) seed ------------------------------------------------------------------
if ($SkipSeed) {
    Write-Host "[3/8] seed: skipped (-SkipSeed)"
} else {
    Write-Host "[3/8] finpilot.seed"
    # The loader is only partially idempotent: users/accounts/payees/... are skipped
    # when present, but holdings / product_navs / card_controls are inserted
    # unconditionally, so seeding a non-empty database can violate uq_holding.
    # Therefore: seed only when the database has no users, unless -Seed forces it.
    $countProbe = @'
import sys
from sqlalchemy import create_engine, text
engine = create_engine(sys.argv[1], future=True)
try:
    with engine.connect() as conn:
        print(conn.execute(text("SELECT count(*) FROM users")).scalar_one())
finally:
    engine.dispose()
'@
    $countProbeFile = Join-Path $RunDir "seed_count.py"
    [System.IO.File]::WriteAllText($countProbeFile, $countProbe, (New-Object System.Text.UTF8Encoding($false)))
    $probeOut = Join-Path $LogDir "seed.count.txt"
    $probeErr = Join-Path $LogDir "seed.count.err.txt"
    $null = Start-Process -FilePath $Python -ArgumentList @($countProbeFile, $DatabaseUrl) `
        -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -Wait `
        -RedirectStandardOutput $probeOut -RedirectStandardError $probeErr
    $userCount = -1
    if (Test-Path $probeOut) {
        $raw = Get-Content $probeOut -Raw -Encoding UTF8
        if ($null -ne $raw) {
            $probeText = $raw.Trim()
            if ($probeText -match '^\d+$') { $userCount = [int]$probeText }
        }
    }
    if ($userCount -gt 0 -and -not $Seed) {
        Write-Host ("  skipped: database already has {0} user(s); pass -Seed to force" -f $userCount)
    } else {
        $out = Join-Path $LogDir "seed.out.log"
        $err = Join-Path $LogDir "seed.err.log"
        $p = Start-Process -FilePath $Python `
            -ArgumentList @("-m", "finpilot.seed", "--json", "data/synthetic/seed_app.json", "--database-url", $DatabaseUrl) `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -Wait `
            -RedirectStandardOutput $out -RedirectStandardError $err
        if ($p.ExitCode -ne 0) {
            Write-Host ("  WARN: seed failed (exit=" + $p.ExitCode + "); see " + $err)
            Write-Host "  hint: the seed loader is not idempotent for holdings/product_navs;"
            Write-Host "        rebuild the schema first (alembic downgrade base && alembic upgrade head) or use scripts\seed.ps1 -Reset"
        } else {
            Write-Host ("  seeded; report: " + $out)
        }
    }
}

# --- 4) api -------------------------------------------------------------------
Write-Host "[4/8] api"
$resolved["api"] = Start-Managed "api" @("-m", "uvicorn", "finpilot.app:app", "--host", "127.0.0.1", "--port", "$ApiPort") $RepoRoot
$apiOk = Wait-Http ("http://127.0.0.1:{0}/healthz" -f $ApiPort) $HealthTimeoutSeconds "api"
if ($apiOk) { Write-Host ("  /healthz 200 on http://127.0.0.1:{0}/healthz" -f $ApiPort) }

# --- 5) worker-outbox ---------------------------------------------------------
Write-Host "[5/8] worker-outbox"
$resolved["worker-outbox"] = Start-Managed "worker-outbox" @((Join-Path $RepoRoot "scripts\worker_outbox.py"), "--interval", "5") $RepoRoot

# --- 6) worker-scheduler ------------------------------------------------------
Write-Host "[6/8] worker-scheduler"
$resolved["worker-scheduler"] = Start-Managed "worker-scheduler" @((Join-Path $RepoRoot "scripts\worker_scheduler.py"), "--interval", "5") $RepoRoot

# --- 7) bank sandbox ----------------------------------------------------------
Write-Host "[7/8] bank-sandbox (58101)"
$resolved["bank-sandbox"] = Start-Managed "bank-sandbox" @("-m", "uvicorn", "bank:app", "--app-dir", "services/provider-sandbox", "--host", "127.0.0.1", "--port", "$BankPort") $RepoRoot
$bankOk = Wait-Http ("http://127.0.0.1:{0}/bank/health" -f $BankPort) $HealthTimeoutSeconds "bank-sandbox"

# --- 8) merchant sandbox ------------------------------------------------------
Write-Host "[8/8] merchant-sandbox (58102)"
$resolved["merchant-sandbox"] = Start-Managed "merchant-sandbox" @("-m", "uvicorn", "merchant:app", "--app-dir", "services/provider-sandbox", "--host", "127.0.0.1", "--port", "$MerchantPort") $RepoRoot
$merchantOk = Wait-Http ("http://127.0.0.1:{0}/merchant/health" -f $MerchantPort) $HealthTimeoutSeconds "merchant-sandbox"

$resolved["started_at"] = (Get-Date).ToString("s")
$resolved | ConvertTo-Json | Set-Content -Path $PidFile -Encoding UTF8

Write-Host ""
Write-Host "FinPilot local stack"
Write-Host ("  api        http://127.0.0.1:{0}/docs   (/healthz, /app, /im if built)" -f $ApiPort)
Write-Host ("  bank       http://127.0.0.1:{0}/bank/health" -f $BankPort)
Write-Host ("  merchant   http://127.0.0.1:{0}/merchant/health" -f $MerchantPort)
Write-Host ("  web client http://127.0.0.1:{0}/app/  (served by api when apps/web/dist exists)" -f $ApiPort)
Write-Host ("  im client  http://127.0.0.1:{0}/im/   (served by api when apps/im/dist exists)" -f $ApiPort)
Write-Host ("  logs       {0}" -f $LogDir)
Write-Host ("  pids       {0}" -f $PidFile)
Write-Host "  stop with  powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1"
if (-not ($apiOk -and $bankOk -and $merchantOk)) {
    Write-Host "RESULT: DEGRADED (at least one health probe failed; see logs)"
    exit 2
}
Write-Host "RESULT: UP"
exit 0