"""汇总一个评测结果 JSONL，输出可读指标（只读，不改结果）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "services" / "api" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from eval.analysis.summarize import summarize_file  # noqa: E402


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: summarize_run.py <result.jsonl> [out.json]")
        return 2
    path = Path(sys.argv[1])
    summary = summarize_file(path)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    if len(sys.argv) > 2:
        Path(sys.argv[2]).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {sys.argv[2]}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())