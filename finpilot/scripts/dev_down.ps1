# FinPilot local stop (V5 DEL-02). ASCII-ONLY ON PURPOSE (see deploy/README.md).
#
# Stops the processes started by scripts\dev_up.ps1 (PID file .run\pids.json) and,
# unless -KeepDatabase is given, the portable PostgreSQL instance.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1 -KeepDatabase
#   powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1 -Force

[CmdletBinding()]
param(
    [switch]$KeepDatabase,
    [switch]$Force,
    [string]$PgBin = $env:FINPILOT_PG_BIN,
    [string]$PgData = "G:\finpilot_tools\pgdata"
)

$ErrorActionPreference = "Continue"
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

$RepoRoot = Split-Path -Parent $PSScriptRoot
$RunDir = Join-Path $RepoRoot ".run"
$PidFile = Join-Path $RunDir "pids.json"
if (-not $PgBin) { $PgBin = "G:\finpilot_tools\pgsql\bin" }

function Stop-Managed {
    param([string]$Name, [int]$Id)
    $proc = Get-Process -Id $Id -ErrorAction SilentlyContinue
    if (-not $proc) {
        Write-Host ("  {0,-18} pid={1} already stopped" -f $Name, $Id)
        return
    }
    try {
        Stop-Process -Id $Id -Force:$Force -ErrorAction Stop
        Write-Host ("  {0,-18} pid={1} stop requested" -f $Name, $Id)
    } catch {
        Write-Host ("  {0,-18} pid={1} FAILED: {2}" -f $Name, $Id, $_.Exception.Message)
    }
}

Write-Host "FinPilot local stop"

if (Test-Path $PidFile) {
    $pids = Get-Content $PidFile -Raw | ConvertFrom-Json
    foreach ($prop in $pids.PSObject.Properties) {
        if ($prop.Name -eq "started_at") { continue }
        Stop-Managed $prop.Name ([int]$prop.Value)
    }
    Start-Sleep -Seconds 1
    Rename-Item -Path $PidFile -NewName ("pids.stopped.{0}.json" -f (Get-Date).ToString("yyyyMMddTHHmmss")) -ErrorAction SilentlyContinue
} else {
    Write-Host "  no .run\pids.json; nothing to stop by PID"
}

# Fallback: catch children started outside dev_up.ps1 by matching the command line.
$orphans = Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -and ($_.CommandLine -like "*finpilot.app:app*" -or $_.CommandLine -like "*worker_outbox.py*" -or $_.CommandLine -like "*worker_scheduler.py*" -or $_.CommandLine -like "*bank:app*" -or $_.CommandLine -like "*merchant:app*") }
foreach ($o in $orphans) {
    Write-Host ("  orphan pid={0} {1}" -f $o.ProcessId, $o.CommandLine)
    Stop-Process -Id $o.ProcessId -Force -ErrorAction SilentlyContinue
}

if ($KeepDatabase) {
    Write-Host "  postgres           kept running (-KeepDatabase)"
} else {
    $pgCtl = Join-Path $PgBin "pg_ctl.exe"
    if (-not (Test-Path $pgCtl)) {
        $cmd = Get-Command "pg_ctl" -ErrorAction SilentlyContinue
        if ($cmd) { $pgCtl = $cmd.Source }
    }
    if ((Test-Path $pgCtl) -and (Test-Path $PgData)) {
        $logDir = Join-Path $RunDir "logs"
        if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir -Force | Out-Null }
        $ctlOut = Join-Path $logDir "pg_ctl_stop.out.txt"
        $ctlErr = Join-Path $logDir "pg_ctl_stop.err.txt"
        $null = Start-Process -FilePath $pgCtl -ArgumentList @("-D", $PgData, "-m", "fast", "stop") `
            -RedirectStandardOutput $ctlOut -RedirectStandardError $ctlErr `
            -WindowStyle Hidden -PassThru -Wait
        Write-Host "  postgres           stop requested (pg_ctl -m fast)"
    } else {
        Write-Host "  postgres           pg_ctl not found; skipped"
    }
}

Write-Host "RESULT: DOWN"
exit 0