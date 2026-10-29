# FinPilot test runner with the same three layers as .github\workflows\checks.yml
# (V5 GOV-01). ASCII-ONLY ON PURPOSE (see deploy/README.md).
#
# Layers
#   quick        unit + contract tests (no database needed); also checks ASCII hygiene
#   integration  tests\integration on a REAL PostgreSQL (TEST_DATABASE_URL)
#   security     tests\security on the same database (audit chain, sandbox, trust boundary)
#   eval         full evaluation run (python -m eval.runner) -- only if eval.runner exists
#   all          quick -> integration -> security (+ eval when -IncludeEval)
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -Layer quick
#   powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -Layer integration `
#       -TestDatabaseUrl "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test"
#   powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -IncludeEval
#
# Exit code: 0 all selected layers passed, non-zero = number of failed layers.

[CmdletBinding()]
param(
    [ValidateSet("all", "quick", "integration", "security", "eval")]
    [string]$Layer = "all",
    [string]$TestDatabaseUrl = $env:TEST_DATABASE_URL,
    [switch]$IncludeEval,
    [string]$EvalConfig = "FULL",
    [string]$EvalSplit = "dev",
    [int]$EvalLimit = 12,
    [int]$EvalRepeats = 1,
    [string]$EvalOut = "eval-results"
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
if (-not $TestDatabaseUrl) {
    $TestDatabaseUrl = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test"
}
$env:TEST_DATABASE_URL = $TestDatabaseUrl

$script:Failed = 0

function Invoke-Pytest {
    param([string]$Name, [string[]]$Paths)
    $out = Join-Path $LogDir ("pytest_" + $Name + ".log")
    $argv = @("-m", "pytest", "-q", "-p", "no:cacheprovider") + $Paths
    Write-Host ("--- {0}: pytest {1}" -f $Name, ($Paths -join " "))
    $proc = Start-Process -FilePath $Python -ArgumentList $argv -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden -PassThru -Wait -RedirectStandardOutput $out -RedirectStandardError ($out + ".err")
    $text = ""
    if (Test-Path $out) { $text = Get-Content $out -Raw -Encoding UTF8 }
    $tail = ($text -split "`r?`n") | Where-Object { $_ -ne "" } | Select-Object -Last 3
    foreach ($line in $tail) { Write-Host ("    " + $line) }
    Write-Host ("    exit={0} log={1}" -f $proc.ExitCode, $out)
    if ($proc.ExitCode -ne 0) { $script:Failed = $script:Failed + 1 }
    return $proc.ExitCode
}

function Test-AsciiHygiene {
    $targets = @()
    $targets += (Join-Path $RepoRoot "alembic.ini")
    foreach ($dir in @("deploy", "scripts")) {
        $p = Join-Path $RepoRoot $dir
        if (Test-Path $p) {
            $targets += (Get-ChildItem -Path $p -Filter "*.ps1" -File | ForEach-Object { $_.FullName })
        }
    }
    $bad = @()
    foreach ($t in $targets) {
        if (-not (Test-Path $t)) { continue }
        $bytes = [System.IO.File]::ReadAllBytes($t)
        foreach ($b in $bytes) {
            if ($b -gt 127) { $bad += (Split-Path -Leaf $t); break }
        }
    }
    if ($bad.Count -eq 0) {
        Write-Host ("--- hygiene: {0} ASCII-only files OK" -f $targets.Count)
        return 0
    }
    Write-Host ("--- hygiene: NON-ASCII in " + ($bad -join ", "))
    $script:Failed = $script:Failed + 1
    return 1
}

function Test-EvalRunner {
    $probe = 'import importlib.util, sys; sys.exit(0 if importlib.util.find_spec("eval.runner") else 3)'
    $proc = Start-Process -FilePath $Python -ArgumentList @("-c", $probe) -WorkingDirectory $RepoRoot `
        -WindowStyle Hidden -PassThru -Wait
    return ($proc.ExitCode -eq 0)
}

switch ($Layer) {
    "quick" {
        Test-AsciiHygiene | Out-Null
        Invoke-Pytest "unit" @("tests/unit") | Out-Null
        Invoke-Pytest "contracts" @("tests/contracts") | Out-Null
    }
    "integration" {
        Invoke-Pytest "integration" @("tests/integration") | Out-Null
    }
    "security" {
        Invoke-Pytest "security" @("tests/security") | Out-Null
    }
    "eval" {
        if (-not (Test-EvalRunner)) {
            Write-Host "--- eval: SKIPPED (eval.runner not importable here; nothing evaluated, NOT a pass)"
            Write-Host "    nothing was evaluated; this is NOT a pass"
        } else {
            $out = Join-Path $LogDir "eval.log"
            Write-Host "--- eval: python -m eval.runner"
            $evalArgs = @("-m", "eval.runner", "--config", $EvalConfig, "--split", $EvalSplit,
                "--limit", "$EvalLimit", "--repeats", "$EvalRepeats", "--mode", "mock",
                "--out", ($EvalOut + "\" + $EvalConfig + "_" + $EvalSplit + ".jsonl"))
            $proc = Start-Process -FilePath $Python -ArgumentList $evalArgs `
                -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -Wait `
                -RedirectStandardOutput $out -RedirectStandardError ($out + ".err")
            Write-Host ("    exit={0} log={1}" -f $proc.ExitCode, $out)
            if ($proc.ExitCode -ne 0) { $script:Failed = $script:Failed + 1 }
        }
    }
    "all" {
        Test-AsciiHygiene | Out-Null
        Invoke-Pytest "unit" @("tests/unit") | Out-Null
        Invoke-Pytest "contracts" @("tests/contracts") | Out-Null
        Invoke-Pytest "integration" @("tests/integration") | Out-Null
        Invoke-Pytest "security" @("tests/security") | Out-Null
        if ($IncludeEval) {
            if (-not (Test-EvalRunner)) {
                Write-Host "--- eval: SKIPPED (eval.runner not importable here; nothing evaluated, NOT a pass)"
            } else {
                $out = Join-Path $LogDir "eval.log"
                $evalArgs = @("-m", "eval.runner", "--config", $EvalConfig, "--split", $EvalSplit,
                    "--limit", "$EvalLimit", "--repeats", "$EvalRepeats", "--mode", "mock",
                    "--out", ($EvalOut + "\" + $EvalConfig + "_" + $EvalSplit + ".jsonl"))
                $proc = Start-Process -FilePath $Python -ArgumentList $evalArgs `
                    -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru -Wait `
                    -RedirectStandardOutput $out -RedirectStandardError ($out + ".err")
                Write-Host ("--- eval exit={0} log={1}" -f $proc.ExitCode, $out)
                if ($proc.ExitCode -ne 0) { $script:Failed = $script:Failed + 1 }
            }
        }
    }
}

Write-Host ""
Write-Host ("layers: {0}   failed: {1}" -f $Layer, $script:Failed)
Write-Host ("logs  : {0}" -f $LogDir)
if ($script:Failed -gt 0) {
    Write-Host "RESULT: FAIL"
    exit $script:Failed
}
Write-Host "RESULT: PASS"
exit 0