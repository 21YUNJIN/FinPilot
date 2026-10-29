import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The service is meant to run inside the StockQuant environment.  In the test
# run we point at the 2026-10-28 backup (or STOCKQUANT_ROOT) so the parity
# tests compare against the real kernel rather than a copy.
SQ_ROOT = os.environ.get("STOCKQUANT_ROOT")
if not SQ_ROOT:
    candidate = ROOT.parent / "StockQuant_Backup_2026-10-28"
    if candidate.exists():
        SQ_ROOT = str(candidate)
if SQ_ROOT and SQ_ROOT not in sys.path:
    sys.path.append(SQ_ROOT)