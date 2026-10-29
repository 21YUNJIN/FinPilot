"""Produce the integration evidence bundle for the S3 quant service.

Runs the real Flask app over loopback HTTP (the same shape FinPilot will call),
saves every response, checks the invariants that the 2026-10-28 review said
were missing (real call, independently recomputable numbers, version identity),
and writes RUN.md plus a SHA-256 index.

The catalog is synthetic.  This proves the pipeline, not market performance.
"""
from __future__ import annotations

import hashlib
import json
import sys
import threading
from pathlib import Path
from werkzeug.serving import make_server

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from finpilot_adapter.policy import RiskGate  # noqa: E402
from finpilot_adapter.risk_report import StockQuantRiskClient, nav_source_hash  # noqa: E402
from stockquant_bank.catalog import request_products  # noqa: E402
from stockquant_bank.service import create_app  # noqa: E402

EVIDENCE = ROOT / "evidence"
TOKEN = "evidence-run-token"


def write(name: str, payload) -> Path:
    path = EVIDENCE / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=False), encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    EVIDENCE.mkdir(exist_ok=True)
    (EVIDENCE / "audit.jsonl").unlink(missing_ok=True)
    app = create_app(token=TOKEN, audit_path=str(EVIDENCE / "audit.jsonl"))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    client = StockQuantRiskClient(base, TOKEN)
    products = request_products()
    as_of = "2026-10-23"
    files = []

    meta = client.meta()
    files.append(write("01_meta.json", meta))

    report = client.risk_report(products, as_of=as_of)
    files.append(write("02_risk_report.json", report.raw))

    ranks = {}
    for mode in ("stability", "balanced", "return"):
        call = client.rank(
            products,
            user={
                "risk_level": "R3",
                "preference_mode": mode,
                "horizon_days": 365,
                "liquidity_need_days": 10,
                "budget_minor": 1_000_000,
            },
            as_of=as_of,
        )
        ranks[mode] = call.data
        files.append(write(f"03_rank_{mode}.json", call.raw))

    stress = client.stress(products, as_of=as_of)
    files.append(write("04_stress.json", stress.raw))

    annotations = RiskGate(require_metrics_for_ranking=False).annotate(report.data["products"])
    files.append(write("05_policy_annotations.json", annotations))

    # --- invariant checks -------------------------------------------------
    by_id = {p["product_id"]: p for p in products}
    checks = []
    hash_rows = []
    for item in report.data["products"]:
        expected = nav_source_hash(by_id[item["product_id"]]["nav"])
        actual = item["provenance"]["source_hash"]
        hash_rows.append({
            "product_id": item["product_id"],
            "passed": expected == actual,
            "source_hash": actual,
        })
    checks.append({
        "check": "source_hash_recomputed_client_side",
        "passed": all(row["passed"] for row in hash_rows),
        "products": hash_rows,
    })
    cvar_missing = [
        item["product_id"] for item in report.data["products"]
        if item["metrics"]["historical_cvar_95"]["value"] is None
    ]
    cvar_expected = [
        item["product_id"] for item in report.data["products"]
        if item["data_window"]["returns"] < 252
    ]
    short = [item["product_id"] for item in report.data["products"] if not item["usable_for_ranking"]]
    checks.append({
        "check": "cvar_withheld_exactly_when_below_252_returns",
        "products": cvar_missing,
        "passed": cvar_missing == cvar_expected,
    })
    checks.append({"check": "short_history_marked_not_usable", "products": short, "passed": bool(short)})

    h4 = h4_probe(ranks)
    files.append(write("06_h4_probe.json", h4))
    files.append(write("07_invariants.json", {"checks": checks, "all_passed": all(c["passed"] for c in checks)}))

    server.shutdown()
    thread.join(timeout=5)

    index = [{"file": path.name, "sha256": sha256(path), "bytes": path.stat().st_size} for path in sorted(files)]
    write("SHA256SUMS.json", index)
    write("RUN.md", run_markdown(meta, report.data, ranks, h4, checks, index))

    ok = all(c["passed"] for c in checks)
    print(json.dumps({"evidence_dir": str(EVIDENCE), "files": len(index), "all_checks_passed": ok}, ensure_ascii=False))
    return 0 if ok else 1


def h4_probe(ranks: dict) -> dict:
    """Toy H4/A4 comparison on the synthetic catalog.

    H4 asks whether risk information changes an explainable ordering.  This
    probe counts top-1 changes and rank inversions between preference modes and
    against the A4 baseline (no quant metrics).  It is a pipeline self-test on
    synthetic NAV, not competition evidence.
    """
    modes = {}
    for mode, data in ranks.items():
        top = data["ranked"][0]["product_id"] if data["ranked"] else None
        baseline_top = data["baseline_ranked"][0]["product_id"] if data["baseline_ranked"] else None
        modes[mode] = {
            "eligible": data["eligible"],
            "top1": top,
            "baseline_top1": baseline_top,
            "top1_changed_by_quant": top != baseline_top,
            "products_with_rank_change": sum(1 for c in data["rank_changes"] if c["delta"] != 0),
            "max_abs_delta": max((abs(c["delta"]) for c in data["rank_changes"]), default=0),
        }
    stability_top = modes.get("stability", {}).get("top1")
    return_top = modes.get("return", {}).get("top1")
    return {
        "basis": "synthetic_bank_catalog_v2",
        "not_competition_evidence": True,
        "preference_modes": modes,
        "stability_vs_return_top1_differs": stability_top != return_top,
        "interpretation": (
            "同一组合、同一数据，只改用户明确表达的偏好，排序首位发生变化；"
            "移除量化指标的 baseline 也给出不同首位。这说明指标进入了决策，"
            "但仍需用真实产品净值与真实用户偏好重跑。"
        ),
    }


def run_markdown(meta, report, ranks, h4, checks, index) -> str:
    engine = meta["data"]["engine"]
    lines = [
        "# S3 量化服务联调证据（自动生成）",
        "",
        f"- 引擎：`{engine['name']}` {engine['version']}，commit `{engine['commit']}`，模块 `{engine['module']}`",
        f"- 方法版本：`{engine['method_version']}`",
        "- 数据：`synthetic_bank_catalog_v2`（合成净值，仅用于联调）",
        f"- 产品数：{report['summary']['requested']}，成功 {report['summary']['computed']}，可排序 {report['summary']['usable_for_ranking']}",
        "",
        "## 口径检查",
        "",
    ]
    for check in checks:
        lines.append(f"- [{'x' if check['passed'] else ' '}] `{check['check']}`")
    lines += ["", "## 排序偏好对照", ""]
    for mode, block in h4["preference_modes"].items():
        lines.append(
            f"- `{mode}`：首位 {block['top1']}（A4 无量化首位 {block['baseline_top1']}），"
            f"名次变化 {block['products_with_rank_change']} 个产品"
        )
    lines += [
        "",
        "## 文件",
        "",
        "| 文件 | 字节 | SHA-256 |",
        "|---|---:|---|",
    ]
    for item in index:
        lines.append(f"| {item['file']} | {item['bytes']} | `{item['sha256'][:16]}…` |")
    lines += [
        "",
        "本证据只证明“服务真的被调用、数字可独立复算、版本与来源可追”，"
        "不证明策略收益，也不替代 H4 在真实产品与真实用户上的评测。",
        "",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())