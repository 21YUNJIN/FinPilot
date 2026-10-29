"""Sell-side research reports → PIT rating / coverage revision features.

Pre-registered in ``docs/ANALYST_REVISION_SIGNAL.md``.  Design constraints:

* **Point-in-time**: a report carries a ``date`` without a time, so it is treated
  as "published after the close" -- usable from the *next* session (the same rule
  as ``factors.engine.pit_usable``).  A report dated ``d`` is therefore only
  visible from ``d + 1`` onwards, which is why every window filter here is
  strictly ``< asof``.
* **No EPS revisions**: Eastmoney only exposes the *current* three forecast years,
  so historical EPS forecasts for the relevant fiscal year do not exist in the
  free data.  Ratings and coverage do (2017 onwards).
* **No imputation**: a symbol without enough reports in *both* windows is simply
  missing.  Filling it with a neutral value would silently convert "no coverage"
  into "no news".
"""
from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

__all__ = ["Event", "RATING_SCORE", "load_events", "revision_features", "FEATURE_NAMES",
           "canonical_symbol"]

RATING_SCORE: Dict[str, float] = {
    "强烈推荐": 5.0, "买入": 4.0, "推荐": 3.5, "增持": 3.0, "优于大市": 3.0,
    "持有": 2.0, "中性": 2.0, "同步大市": 2.0, "谨慎": 1.5,
    "减持": 1.0, "弱于大市": 1.0, "卖出": 0.0,
}

FEATURE_NAMES = ("rating_rev_3m", "coverage_rev_3m", "rating_level")


@dataclass(frozen=True)
class Event:
    date: str
    score: Optional[float]
    institution: str


def canonical_symbol(code: str) -> str:
    """``000001`` -> ``000001.SZ`` (the cache is keyed by bare code, the panel by symbol).

    Without this the feature lookup silently matched nothing and every IC came out
    empty -- a key-format mismatch that *looks* like "no signal".
    """
    text = str(code).strip().upper()
    if "." in text:
        return text
    digits = "".join(ch for ch in text if ch.isdigit())
    if len(digits) != 6:
        return text
    if digits.startswith(("6", "9")):
        return f"{digits}.SH"
    if digits.startswith(("4", "8")):
        return f"{digits}.BJ"
    return f"{digits}.SZ"


def load_events(cache_dir: str, normalize_symbols: bool = True) -> Dict[str, List[Event]]:
    """``{symbol: [Event, ...]}`` sorted by date, from the per-symbol cache."""
    out: Dict[str, List[Event]] = {}
    root = Path(cache_dir)
    if not root.exists():
        return out
    for path in sorted(root.glob("*.csv")):
        symbol = path.stem
        if symbol.startswith("_"):
            continue
        events: List[Event] = []
        try:
            with path.open(newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    raw = str(row.get("rating") or "").strip()
                    events.append(Event(date=str(row.get("date") or "")[:10],
                                        score=RATING_SCORE.get(raw),
                                        institution=str(row.get("institution") or "").strip()))
        except Exception:
            continue
        if events:
            key = canonical_symbol(symbol) if normalize_symbols else symbol
            out.setdefault(key, []).extend(events)
    for key in out:
        out[key] = sorted(out[key], key=lambda e: e.date)
    return out


def _window_stats(events: Iterable[Event], start: str, end: str, dedup: bool) -> Tuple[Optional[float], int]:
    picked = [e for e in events if start <= e.date < end]
    if dedup:
        latest: Dict[str, Event] = {}
        for event in picked:
            key = event.institution or f"__{event.date}"
            if key not in latest or event.date > latest[key].date:
                latest[key] = event
        picked = list(latest.values())
    scores = [e.score for e in picked if e.score is not None]
    mean = (sum(scores) / len(scores)) if scores else None
    return mean, len(picked)


def revision_features(
    events_by_symbol: Dict[str, List[Event]],
    asof: str,
    window_days: int = 90,
    require_min: int = 2,
    dedup: bool = False,
) -> Dict[str, Dict[str, float]]:
    """Features per symbol at ``asof`` (see the pre-registration for definitions)."""
    try:
        anchor = date.fromisoformat(asof)
    except ValueError:
        return {}
    w = int(window_days)
    recent = ((anchor - timedelta(days=w)).isoformat(), asof)
    prior = ((anchor - timedelta(days=2 * w)).isoformat(), recent[0])

    out: Dict[str, Dict[str, float]] = {}
    for symbol, events in events_by_symbol.items():
        visible = [e for e in events if e.date < asof]      # date-only -> next session
        if not visible:
            continue
        mean_now, n_now = _window_stats(visible, *recent, dedup=dedup)
        mean_prev, n_prev = _window_stats(visible, *prior, dedup=dedup)
        if n_now < require_min or n_prev < require_min:
            continue
        features: Dict[str, float] = {}
        if mean_now is not None and mean_prev is not None:
            features["rating_rev_3m"] = mean_now - mean_prev
        if mean_now is not None:
            features["rating_level"] = mean_now
        features["coverage_rev_3m"] = math.log1p(n_now) - math.log1p(n_prev)
        if features:
            out[symbol] = features
    return out
