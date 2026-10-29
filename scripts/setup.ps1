param([string]$Python = "python")
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$version = & $Python -c "import sys; print('%s.%s' % sys.version_info[:2])"
if ($LASTEXITCODE -ne 0 -or $version -ne "3.12") { throw "Python 3.12 (64-bit Windows) is required for the bundled wheels." }
$fp = Join-Path $root ".venv-finpilot"
$quant = Join-Path $root ".venv-quant"
& $Python -m venv $fp
if ($LASTEXITCODE -ne 0) { throw "FinPilot venv creation failed." }
& $Python -m venv $quant
if ($LASTEXITCODE -ne 0) { throw "Quant venv creation failed." }
$fpPy = Join-Path $fp "Scripts\python.exe"
$quantPy = Join-Path $quant "Scripts\python.exe"
$wheelhouse = Join-Path $root "finpilot\wheelhouse"
& $fpPy -m pip install --no-index --find-links $wheelhouse -r (Join-Path $root "finpilot\requirements-lock.txt")
if ($LASTEXITCODE -ne 0) { throw "Offline FinPilot dependencies failed." }
# These dependencies are not present in the original offline package; network is needed.
& $fpPy -m pip install "requests>=2.28,<3"
if ($LASTEXITCODE -ne 0) { throw "FinPilot HTTP dependency installation failed." }
& $quantPy -m pip install "flask>=3,<4" "requests>=2.28,<3" "numpy>=1.24,<3"
if ($LASTEXITCODE -ne 0) { throw "Quant dependencies installation failed." }
Write-Host "Environments ready. Configure a dedicated PostgreSQL demo database; see README.md."
