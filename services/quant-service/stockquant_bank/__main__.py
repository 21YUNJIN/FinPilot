"""Command line entry point for local runs and CI smoke checks."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from .catalog import load_catalog, request_products
from .rank import rank_products
from .report import build_risk_report
from .stress import stress_product


def _print(payload: Any) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
    sys.stdout.write("\n")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="stockquant_bank")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the read-only HTTP service")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)

    report = sub.add_parser("report", help="print risk reports for the catalog")
    report.add_argument("--as-of", default=None)
    report.add_argument("--products", nargs="*", default=None)

    rank = sub.add_parser("rank", help="print a suitability-aware ranking")
    rank.add_argument("--as-of", default=None)
    rank.add_argument("--risk-level", default="R3")
    rank.add_argument("--mode", default="balanced")
    rank.add_argument("--horizon-days", type=int, default=365)
    rank.add_argument("--liquidity-need-days", type=int, default=30)
    rank.add_argument("--budget-minor", type=int, default=1_000_000)
    rank.add_argument("--no-quant", action="store_true", help="A4 ablation: omit risk metrics")

    stress = sub.add_parser("stress", help="print stress scenarios")
    stress.add_argument("--as-of", default=None)
    stress.add_argument("--products", nargs="*", default=None)

    args = parser.parse_args(argv)

    if args.command == "serve":
        from .service import create_app

        app = create_app()
        app.run(host=args.host, port=args.port, threaded=True)
        return 0

    as_of = args.as_of
    if as_of is None:
        as_of = load_catalog()[-1]["nav_end"]
    products = request_products(getattr(args, "products", None))
    if args.command == "report":
        _print(build_risk_report(products, as_of=as_of))
    elif args.command == "rank":
        _print(rank_products(
            products,
            as_of=as_of,
            user={
                "risk_level": args.risk_level,
                "preference_mode": args.mode,
                "horizon_days": args.horizon_days,
                "liquidity_need_days": args.liquidity_need_days,
                "budget_minor": args.budget_minor,
            },
            include_quant=not args.no_quant,
        ))
    elif args.command == "stress":
        from .nav import validate_series

        out = []
        for product in products:
            series = validate_series(product["product_id"], product["nav"], as_of=as_of)
            out.append(stress_product(series))
        _print({"as_of": as_of, "products": out})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())