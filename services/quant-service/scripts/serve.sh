#!/usr/bin/env bash
# Start the read-only S3 wealth-risk service.
#
# The service deliberately runs in the StockQuant Python environment: it
# imports stockquant.backtest.metrics so that every number can be traced to the
# same kernel the StockQuant reports use.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STOCKQUANT_ROOT="${STOCKQUANT_ROOT:-$HERE/../StockQuant_Backup_2026-10-28}"
PYTHON="${PYTHON:-python3}"

if [[ ! -d "$STOCKQUANT_ROOT/stockquant" ]]; then
  echo "找不到 StockQuant 源码：$STOCKQUANT_ROOT" >&2
  echo "请设置 STOCKQUANT_ROOT 指向已恢复的 StockQuant 仓库。" >&2
  exit 2
fi

export PYTHONPATH="$STOCKQUANT_ROOT:$HERE${PYTHONPATH:+:$PYTHONPATH}"
export SQ_BANK_ENGINE_COMMIT="${SQ_BANK_ENGINE_COMMIT:-$(git -C "$STOCKQUANT_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
export SQ_BANK_TOKEN="${SQ_BANK_TOKEN:?请先设置 SQ_BANK_TOKEN（服务端令牌）}"
export SQ_BANK_AUDIT_LOG="${SQ_BANK_AUDIT_LOG:-$HERE/var/audit.jsonl}"

exec "$PYTHON" -m stockquant_bank serve --host "${SQ_BANK_HOST:-127.0.0.1}" --port "${SQ_BANK_PORT:-8770}"