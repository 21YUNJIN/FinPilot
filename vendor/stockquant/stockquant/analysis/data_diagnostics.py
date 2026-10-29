"""Cross-sectional missingness diagnostics for research data CSVs.

A cheap but high-value check for a factor pipeline: how much of each fundamental
field is missing, and whether the missingness is stable over time.  Non-random
missingness (MNAR) silently biases factor scores, so this report is meant to be
read *before* trusting any factor or model built on the data.

The module deliberately uses only the standard library so it runs against the
plain ``python3`` interpreter used by the test harness.
"""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence

# Factor/fundamental fields present in the project's Bar/CSV schema.
DEFAULT_FIELDS: Sequence[str] = (
    "market_cap",
    "pe",
    "pb",
    "roe",
    "eps",
    "bps",
    "revenue_growth",
    "profit_growth",
    "gross_margin",
    "forecast_growth",
    "forecast_positive",
    "holder_change",
    "sector",
)

_MISSING_TOKENS = {"", "nan", "none", "null", "na", "n/a", "-"}


def is_missing(value: object, extra_tokens: Iterable[str] = ()) -> bool:
    if value is None:
        return True
    text = str(value).strip().lower()
    return text in _MISSING_TOKENS or text in {t.lower() for t in extra_tokens}


@dataclass
class FieldStat:
    total: int = 0
    missing: int = 0

    @property
    def rate(self) -> float:
        return self.missing / self.total if self.total else 0.0


@dataclass
class MissingnessReport:
    path: str = ""
    total_rows: int = 0
    fields: Dict[str, FieldStat] = field(default_factory=dict)
    by_year: Dict[str, Dict[str, FieldStat]] = field(default_factory=dict)
    rows_all_missing: int = 0

    def field_rate(self, name: str) -> float:
        stat = self.fields.get(name)
        return stat.rate if stat else 0.0

    def sorted_fields(self) -> List[str]:
        return sorted(self.fields, key=lambda n: self.fields[n].rate, reverse=True)


def diagnose_csv_missingness(
    path: str,
    fields: Optional[Iterable[str]] = None,
    date_field: str = "date",
    encoding: str = "utf-8",
    extra_missing_tokens: Iterable[str] = (),
) -> MissingnessReport:
    """Scan a CSV and compute per-field and per-year missing rates.

    ``extra_missing_tokens`` lets callers treat additional sentinel strings as
    missing (for example ``("unknown",)`` to flag an uninformative sector
    label)."""
    selected = list(fields or DEFAULT_FIELDS)
    tokens = tuple(extra_missing_tokens)
    report = MissingnessReport(path=path)
    year_fields: Dict[str, Dict[str, FieldStat]] = {}

    with open(path, newline="", encoding=encoding) as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing_cols = [name for name in selected if name not in header]
        if missing_cols:
            raise ValueError(f"CSV is missing requested columns: {missing_cols}")

        for row in reader:
            report.total_rows += 1
            non_missing = 0
            date_text = str(row.get(date_field) or "")
            year = date_text[:4] if len(date_text) >= 4 else ""
            for name in selected:
                stat = report.fields.setdefault(name, FieldStat())
                stat.total += 1
                if is_missing(row.get(name), tokens):
                    stat.missing += 1
                else:
                    non_missing += 1
                if year:
                    ystat = year_fields.setdefault(year, {}).setdefault(name, FieldStat())
                    ystat.total += 1
                    if is_missing(row.get(name), tokens):
                        ystat.missing += 1
            if non_missing == 0 and selected:
                report.rows_all_missing += 1

    report.by_year = year_fields
    return report


def format_report(report: MissingnessReport, top_n: int = 20) -> str:
    lines: List[str] = []
    lines.append(f"Missingness report: {report.path}")
    lines.append(f"Total rows: {report.total_rows}")
    if report.total_rows:
        lines.append(
            f"Rows with all selected fields missing: {report.rows_all_missing} "
            f"({100.0 * report.rows_all_missing / report.total_rows:.2f}%)"
        )
    lines.append("")
    lines.append("Per-field missing rate (descending):")
    lines.append(f"{'field':<20}{'missing':>10}{'total':>10}{'rate':>10}")
    for name in report.sorted_fields()[:top_n]:
        stat = report.fields[name]
        lines.append(f"{name:<20}{stat.missing:>10}{stat.total:>10}{100.0 * stat.rate:>9.1f}%")
    if report.by_year:
        lines.append("")
        lines.append("Missing rate by year (fields with any missingness):")
        years = sorted(report.by_year)
        interesting = [n for n in report.sorted_fields() if report.field_rate(n) > 0.0]
        for name in interesting:
            rates = []
            for y in years:
                stat = report.by_year[y].get(name)
                rates.append(f"{100.0 * stat.rate:.0f}%" if stat and stat.total else ".")
            lines.append(f"{name:<20}" + "".join(f"{r:>6}" for r in rates))
        lines.append(f"{'year':<20}" + "".join(f"{y:>6}" for y in years))
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Report CSV missingness for factor fields.")
    parser.add_argument("csv_path", help="Path to the research CSV.")
    parser.add_argument("--date-field", default="date")
    parser.add_argument("--fields", default=None, help="Comma-separated fields; defaults to the factor schema.")
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args(argv)
    fields = args.fields.split(",") if args.fields else None
    report = diagnose_csv_missingness(args.csv_path, fields=fields, date_field=args.date_field)
    print(format_report(report, top_n=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
