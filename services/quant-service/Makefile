PY ?= python3
STOCKQUANT_ROOT ?= ../StockQuant_Backup_2026-10-28

export STOCKQUANT_ROOT
export PYTHONPATH := $(STOCKQUANT_ROOT):$(CURDIR)

.PHONY: test evidence data serve

test:
	$(PY) -m pytest

evidence:
	SQ_BANK_ENGINE_COMMIT=5559e3f $(PY) scripts/run_evidence.py

data:
	$(PY) scripts/generate_synthetic_catalog.py

serve:
	SQ_BANK_ENGINE_COMMIT=5559e3f bash scripts/serve.sh
