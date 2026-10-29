from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, List, Tuple

from typing import Optional

from stockquant.domain import Bar


@dataclass(frozen=True)
class PortfolioConstraints:
    max_positions: int = 10
    max_weight: float = 0.12
    sector_max_weight: float = 0.35
    cash_buffer: float = 0.03
    min_score: float = -9.0
    weight_scheme: str = "score"  # equal or score
    max_turnover: float = 1.0      # one-way turnover budget per rebalance (1.0 = unlimited)
    cvar_alpha: Optional[float] = None   # tail level for the CVaR penalty (e.g. 0.05)
    cvar_lambda: float = 0.0             # CVaR penalty weight in the optimizer objective


def _normalize(weights: Dict[str, float]) -> Dict[str, float]:
    total = sum(max(0.0, v) for v in weights.values())
    if total <= 0:
        return {k: 0.0 for k in weights}
    return {k: max(0.0, v) / total for k, v in weights.items()}


def build_target_weights(
    scores: Dict[str, float],
    bars: Dict[str, Bar],
    constraints: PortfolioConstraints,
    current_weights: Optional[Dict[str, float]] = None,
) -> Dict[str, float]:
    """Build target weights.

    When ``current_weights`` is supplied and ``max_turnover < 1``, the target is
    pulled back toward the current position so the one-way turnover stays within
    the budget. This is the single most effective lever against cost erosion for
    a monthly-rebalanced cross-sectional signal (see the research notes).
    """
    eligible: List[Tuple[str, float]] = []
    for symbol, score in scores.items():
        bar = bars.get(symbol)
        if bar is None or not bar.tradable():
            continue
        if score < constraints.min_score:
            continue
        eligible.append((symbol, score))
    eligible.sort(key=lambda item: item[1], reverse=True)
    eligible = eligible[: max(0, constraints.max_positions)]
    if not eligible:
        return {}

    if constraints.weight_scheme == "equal":
        raw = {s: 1.0 for s, _ in eligible}
    else:
        # Shift scores to positive values while retaining cross-sectional ordering.
        min_score = min(s for _, s in eligible)
        raw = {s: (score - min_score + 0.25) for s, score in eligible}
    raw = _normalize(raw)

    investable = max(0.0, 1.0 - constraints.cash_buffer)
    weights = {s: w * investable for s, w in raw.items()}

    # Individual caps via iterative water-filling.
    for _ in range(20):
        over = {s: w for s, w in weights.items() if w > constraints.max_weight + 1e-12}
        if not over:
            break
        freed = 0.0
        for s in over:
            freed += weights[s] - constraints.max_weight
            weights[s] = constraints.max_weight
        room = {
            s: max(0.0, constraints.max_weight - w)
            for s, w in weights.items()
            if w < constraints.max_weight - 1e-12
        }
        room_total = sum(room.values())
        if room_total <= 1e-12:
            break
        for s, r in room.items():
            weights[s] += freed * r / room_total

    # Sector caps. If the dataset has no real sector labels, do not impose a
    # meaningless 35% cap on the entire universe.
    sector_of = {s: bars[s].sector for s, _ in eligible}
    sectors_available = any(sec and sec != "Unknown" for sec in sector_of.values())
    for _ in range(40 if sectors_available else 0):
        sector_weights: Dict[str, float] = {}
        for s, w in weights.items():
            sector_weights[sector_of[s]] = sector_weights.get(sector_of[s], 0.0) + w
        over_sectors = {
            sec: val
            for sec, val in sector_weights.items()
            if val > constraints.sector_max_weight + 1e-12
        }
        if not over_sectors:
            break
        freed = 0.0
        for sec, total in over_sectors.items():
            scale = constraints.sector_max_weight / total
            for s in list(weights):
                if sector_of[s] == sec:
                    old = weights[s]
                    weights[s] = old * scale
                    freed += old - weights[s]
        # Redistribute only to positions and sectors with capacity.
        candidate_room: Dict[str, float] = {}
        current_sector: Dict[str, float] = {}
        for s, w in weights.items():
            current_sector[sector_of[s]] = current_sector.get(sector_of[s], 0.0) + w
        for s, w in weights.items():
            sec_room = max(0.0, constraints.sector_max_weight - current_sector[sector_of[s]])
            room = min(max(0.0, constraints.max_weight - w), sec_room)
            if room > 1e-12:
                candidate_room[s] = room
        room_total = sum(candidate_room.values())
        if room_total <= 1e-12:
            break
        for s, room in candidate_room.items():
            weights[s] += freed * room / room_total

    # Remove dust and normalize invested weight conservatively.
    weights = {s: w for s, w in weights.items() if w >= 0.002}
    if not weights:
        return {}
    total = sum(weights.values())
    if total > investable:
        weights = {s: w * investable / total for s, w in weights.items()}

    # Turnover cap (applied last so dust removal / rescaling cannot push the
    # actual turnover back over the budget).
    if current_weights and constraints.max_turnover < 1.0:
        cur = {s: max(0.0, float(w)) for s, w in current_weights.items()}
        symbols = set(weights) | set(cur)
        move = {s: (weights.get(s, 0.0) - cur.get(s, 0.0)) for s in symbols}
        raw_turnover = 0.5 * sum(abs(v) for v in move.values())
        if raw_turnover > constraints.max_turnover + 1e-12:
            scale = constraints.max_turnover / raw_turnover
            weights = {s: cur.get(s, 0.0) + scale * move[s] for s in symbols}
            weights = {s: max(0.0, w) for s, w in weights.items()}
            weights = {s: w for s, w in weights.items() if w >= 0.0005}
    return weights

def build_enhanced_weights(
    scores: Dict[str, float],
    bars: Dict[str, Bar],
    constraints: PortfolioConstraints,
    benchmark_weights: Optional[Dict[str, float]] = None,
    tilt_strength: float = 0.6,
    max_active_weight: float = 0.02,
) -> Dict[str, float]:
    """Index-enhancement weights: benchmark + a zero-sum factor tilt.

    This is the *deployable* form of a factor signal: instead of a concentrated
    top-N bet (which drifts to beta 0.4-0.6 and lags the index), hold the whole
    benchmark and tilt each name by its composite score. The portfolio stays
    close to the benchmark (beta ~ 1, low tracking error) and the excess return
    comes from the active tilt, not from a directional exposure.

    When ``benchmark_weights`` is None the benchmark is the equal-weight of the
    eligible (tradeable, score-floor-passing) names, matching the engine's
    benchmark. ``max_active_weight`` caps the per-name deviation from the
    benchmark so the portfolio cannot drift into a concentrated bet.
    """
    eligible = [
        s for s, sc in scores.items()
        if bars.get(s) is not None and bars[s].tradable() and sc >= constraints.min_score
    ]
    if not eligible:
        return {}
    n = len(eligible)
    if benchmark_weights:
        base = {s: max(0.0, float(benchmark_weights.get(s, 0.0))) for s in eligible}
        total = sum(base.values())
        base = {s: v / total for s, v in base.items()} if total > 0 else {s: 1.0 / n for s in eligible}
    else:
        base = {s: 1.0 / n for s in eligible}

    vals = [scores[s] for s in eligible]
    mean = statistics.mean(vals)
    std = statistics.pstdev(vals)
    z = {s: ((scores[s] - mean) / std if std > 1e-12 else 0.0) for s in eligible}

    investable = max(0.0, 1.0 - constraints.cash_buffer)
    # Active = tilt_strength * z, capped at +/-max_active_weight and made zero-sum.
    active = {s: max(-max_active_weight, min(max_active_weight, tilt_strength * z[s] * base[s])) for s in eligible}
    drift = sum(active.values())
    active = {s: v - drift / n for s, v in active.items()}
    weights = {s: base[s] + active[s] for s in eligible}
    weights = {s: max(0.0, w) for s, w in weights.items()}

    # Individual caps via water-filling (same rule as build_target_weights).
    for _ in range(20):
        over = {s: w for s, w in weights.items() if w > constraints.max_weight + 1e-12}
        if not over:
            break
        freed = 0.0
        for s in over:
            freed += weights[s] - constraints.max_weight
            weights[s] = constraints.max_weight
        room = {s: max(0.0, constraints.max_weight - w) for s, w in weights.items()
                if w < constraints.max_weight - 1e-12}
        rt = sum(room.values())
        if rt <= 1e-12:
            break
        for s, r in room.items():
            weights[s] += freed * r / rt

    sector_of = {s: bars[s].sector for s in weights}
    sectors_available = any(sec and sec != "Unknown" for sec in sector_of.values())
    for _ in range(40 if sectors_available else 0):
        totals: Dict[str, float] = {}
        for s, w in weights.items():
            totals[sector_of[s]] = totals.get(sector_of[s], 0.0) + w
        over = {sec: v for sec, v in totals.items() if v > constraints.sector_max_weight + 1e-12}
        if not over:
            break
        for sec, tot in over.items():
            scale = constraints.sector_max_weight / tot
            for s in list(weights):
                if sector_of[s] == sec:
                    weights[s] *= scale

    weights = {s: w for s, w in weights.items() if w >= 2e-4}
    if not weights:
        return {}
    total = sum(weights.values())
    if total > investable and total > 0:
        weights = {s: w * investable / total for s, w in weights.items()}
    return weights

def build_relative_weights(
    scores: Dict[str, float],
    bars: Dict[str, Bar],
    constraints: PortfolioConstraints,
    benchmark_weights: Dict[str, float],
    tilt_strength: float = 0.3,
    max_active_weight: float = 0.004,
    betas: Optional[Dict[str, float]] = None,
    target_active_beta: Optional[float] = None,
) -> Dict[str, float]:
    """Benchmark-relative (index-enhancement) weights: ``w = w_b + a``.

    The benchmark ``w_b`` is the index weight of each name; the active weight
    ``a`` is a zero-sum tilt by the composite score, bounded per name. Because
    the tilt is zero-sum, the portfolio is always fully invested and its beta
    stays close to the benchmark's (the active beta is controlled), which is the
    standard index-enhancement construction. Long-only by clipping.
    """
    members = [s for s in benchmark_weights if bars.get(s) is not None and bars[s].tradable()]
    if len(members) < 5:
        return {}
    tot = sum(max(0.0, benchmark_weights.get(s, 0.0)) for s in members)
    if tot <= 0:
        base = {s: 1.0 / len(members) for s in members}
    else:
        base = {s: max(0.0, benchmark_weights.get(s, 0.0)) / tot for s in members}

    sc = {s: scores.get(s) for s in members if scores.get(s) is not None}
    if len(sc) < 5:
        return {}
    vals = list(sc.values())
    mean = statistics.mean(vals)
    std = statistics.pstdev(vals)
    z = {s: ((sc[s] - mean) / std if std > 1e-12 else 0.0) for s in sc}

    investable = max(0.0, 1.0 - constraints.cash_buffer)
    # active = tilt_strength * z * benchmark_weight, zero-sum, bounded
    a = {s: tilt_strength * z.get(s, 0.0) * base[s] for s in members}
    drift = sum(a.values())
    a = {s: a[s] - drift / len(members) for s in members}
    # cap |a|
    a = {s: max(-max_active_weight, min(max_active_weight, a[s])) for s in members}
    # re-zero-sum after capping
    drift = sum(a.values())
    a = {s: a[s] - drift / len(members) for s in members}
    w = {s: max(0.0, base[s] * investable + a[s] * investable) for s in members}

    # optional active-beta control (pull the active beta toward target)
    if betas and target_active_beta is not None:
        b = {s: float(betas.get(s, 0.0)) for s in members}
        bw = sum(base[s] * b[s] for s in members)
        cur = sum(w[s] * b[s] for s in members)
        bc = {s: b[s] - sum(base[t] * b[t] for t in members) for s in members}
        denom = sum(bc[s] ** 2 for s in members)
        if denom > 1e-12:
            delta = (target_active_beta * investable + bw * investable - cur) / denom
            w = {s: max(0.0, w[s] + delta * bc[s]) for s in members}

    # individual + sector caps (conservative, no redistribution that re-breaks)
    for _ in range(20):
        over = {s: v for s, v in w.items() if v > constraints.max_weight + 1e-12}
        if not over:
            break
        for s in over:
            w[s] = constraints.max_weight
    if any((bars[s].sector or "Unknown") not in ("", "Unknown") for s in w):
        for _ in range(40):
            tot_sec: Dict[str, float] = {}
            for s, v in w.items():
                tot_sec[bars[s].sector] = tot_sec.get(bars[s].sector, 0.0) + v
            over = {k: v for k, v in tot_sec.items() if v > constraints.sector_max_weight + 1e-12}
            if not over:
                break
            for k, v in over.items():
                sc_ = constraints.sector_max_weight / v
                for s in list(w):
                    if bars[s].sector == k:
                        w[s] *= sc_
    w = {s: v for s, v in w.items() if v >= 2e-4}
    total = sum(w.values())
    if total > investable and total > 0:
        w = {s: v * investable / total for s, v in w.items()}
    return w
