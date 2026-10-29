"""Machine-learning cross-sectional baseline (LightGBM vs ridge).

Gu, Kelly & Xiu (2020) establish that the right way to judge a non-linear model
is to compare its *out-of-sample* cross-sectional predictive power against a
regularized linear baseline, with strict time separation.  This module does a
single purged time split: LightGBM and ridge are both fit on the training period
and evaluated on the held-out period, and the result reports OOS IC for each.

The module degrades gracefully when ``lightgbm`` is not installed, so the
standard-library test harness keeps passing; the real comparison is run with the
project virtualenv (``.venv``), which ships lightgbm.
"""

from __future__ import annotations

import statistics
from typing import Dict, List, Optional, Sequence, Tuple

from stockquant.analysis.ic import _forward_return, _rebalance_due, spearman
from stockquant.analysis.neutralize import MODE_INDUSTRY_BETA, neutralized_labels
from stockquant.domain import MarketData
from stockquant.factors import DEFAULT_FACTORS, FactorEngine, FactorSpec


def collect_panel_with_meta(
    market: MarketData,
    start: str,
    end: str,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    factors: Optional[Sequence[FactorSpec]] = None,
    min_market_cap_quantile: float = 0.0,
    label_mode: str = MODE_INDUSTRY_BETA,
    orthogonalize: bool = False,
    impute_missing: bool = False,
) -> Tuple[List[str], List[List[float]], List[float], List[Tuple[str, str]]]:
    """Collect a factor panel plus (date, symbol) metadata for each row.

    Labels are neutralized (sector mean + market beta by default) so the model
    is trained on selection-relevant returns, not industry/beta exposure.
    """
    fset = tuple(factors) if factors is not None else DEFAULT_FACTORS
    names = [f.name for f in fset]
    engine = FactorEngine(
        fset,
        min_history=min_history,
        sector_neutral=sector_neutral,
        min_market_cap_quantile=min_market_cap_quantile,
        orthogonalize=orthogonalize,
        impute_missing=impute_missing,
    )
    dates = [d for d in market.dates if start <= d <= end]
    # Collect raw per-row factor values first, then drop factors that are empty
    # across the whole panel.  A dataset without fundamentals (e.g. AKShare
    # price-only data) would otherwise produce zero usable rows.
    raw_rows: List[Tuple[str, str, float, Dict[str, Optional[float]]]] = []
    for i in range(0, max(0, len(dates) - horizon)):
        cur = dates[i]
        nxt = dates[i + 1] if i + 1 < len(dates) else None
        if nxt is None or not _rebalance_due(cur, nxt, rebalance):
            continue
        future = dates[i + horizon]
        components, diag = engine.components(market, cur)
        if diag.scored_count < 5:
            continue
        targets: Dict[str, float] = {}
        symbols = set()
        for mapping in components.values():
            symbols.update(mapping)
        for symbol in symbols:
            fr = _forward_return(market, symbol, cur, future)
            if fr is not None:
                targets[symbol] = fr
        if len(targets) < 5:
            continue
        targets = neutralized_labels(market, cur, future, list(targets), targets, mode=label_mode)
        for symbol, target in targets.items():
            values: Dict[str, Optional[float]] = {}
            for name in names:
                value = components.get(name, {}).get(symbol)
                values[name] = float(value) if value is not None else None
            raw_rows.append((cur, symbol, target, values))

    active = [name for name in names if any(row[3].get(name) is not None for row in raw_rows)]
    X: List[List[float]] = []
    y: List[float] = []
    meta: List[Tuple[str, str]] = []
    for cur, symbol, target, values in raw_rows:
        if not active or any(values.get(name) is None for name in active):
            continue
        X.append([float(values[name]) for name in active])  # type: ignore[arg-type]
        y.append(target)
        meta.append((cur, symbol))
    return active, X, y, meta


def _ridge_predict(X_train, y_train, X_test, ridge_lambda=50.0):
    import numpy as np

    x = np.column_stack([np.ones(len(X_train)), np.asarray(X_train, dtype=float)])
    xt = np.column_stack([np.ones(len(X_test)), np.asarray(X_test, dtype=float)])
    target = np.asarray(y_train, dtype=float)
    penalty = np.eye(x.shape[1]) * ridge_lambda
    penalty[0, 0] = 0.0
    try:
        beta = np.linalg.solve(x.T @ x + penalty, x.T @ target)
    except np.linalg.LinAlgError:
        beta = np.linalg.pinv(x.T @ x + penalty) @ x.T @ target
    return xt @ beta


def _oos_ic_by_date(preds: List[float], actuals: List[float], dates: List[str]) -> Dict[str, float]:
    by_date: Dict[str, Tuple[List[float], List[float]]] = {}
    for p, a, d in zip(preds, actuals, dates):
        by_date.setdefault(d, ([], []))
        by_date[d][0].append(p)
        by_date[d][1].append(a)
    out: Dict[str, float] = {}
    for d, (ps, ys) in by_date.items():
        if len(ps) >= 5:
            ic = spearman(ps, ys)
            if ic is not None:
                out[d] = ic
    return out


def lightgbm_baseline(
    market: MarketData,
    start: str,
    end: str,
    min_history: int = 80,
    horizon: int = 20,
    rebalance: str = "monthly",
    sector_neutral: bool = True,
    factors: Optional[Sequence[FactorSpec]] = None,
    min_market_cap_quantile: float = 0.0,
    label_mode: str = MODE_INDUSTRY_BETA,
    orthogonalize: bool = False,
    impute_missing: bool = False,
    train_fraction: float = 0.7,
    embargo_days: int = 21,
    n_estimators: int = 200,
    learning_rate: float = 0.05,
    num_leaves: int = 31,
    ridge_lambda: float = 50.0,
) -> Dict[str, object]:
    """Fit a gradient-boosted tree model (LightGBM or scikit-learn) and ridge on a
    purged time split and report OOS IC."""
    try:
        import numpy  # noqa: F401
    except Exception:
        return {"available": False, "error": "numpy is not installed"}

    backend = None
    try:
        import lightgbm as _lgb  # noqa: F401
        backend = "lightgbm"
    except Exception:
        try:
            import sklearn  # noqa: F401
            backend = "sklearn"
        except Exception:
            return {"available": False, "error": "neither lightgbm nor scikit-learn is installed"}

    names, X, y, meta = collect_panel_with_meta(
        market, start, end, min_history, horizon, rebalance, sector_neutral, factors,
        min_market_cap_quantile, label_mode, orthogonalize, impute_missing,
    )
    if len(X) < 200:
        return {"available": True, "error": "insufficient samples for a train/test split"}

    Xa = __import__("numpy").asarray(X, dtype=float)
    ya = __import__("numpy").asarray(y, dtype=float)
    row_dates = [m[0] for m in meta]
    all_dates = sorted(set(row_dates))
    cutoff = all_dates[max(0, int(len(all_dates) * train_fraction) - 1)]
    test_min = all_dates[min(len(all_dates) - 1, int(len(all_dates) * train_fraction))]

    train_idx = [i for i, d in enumerate(row_dates) if d <= cutoff]
    test_idx = [
        i for i, d in enumerate(row_dates)
        if d >= test_min and _days_between(cutoff, d) >= embargo_days
    ]
    if len(train_idx) < 200 or len(test_idx) < 100:
        return {"available": True, "error": "train/test split too small"}

    if backend == "lightgbm":
        lgb = __import__("lightgbm")
        model = lgb.LGBMRegressor(
            n_estimators=n_estimators,
            learning_rate=learning_rate,
            num_leaves=num_leaves,
            min_child_samples=20,
            subsample=0.8,
            colsample_bytree=0.8,
            random_state=7,
            verbosity=-1,
        )
    else:
        sklearn = __import__("sklearn.ensemble", fromlist=["HistGradientBoostingRegressor"])
        model = sklearn.HistGradientBoostingRegressor(
            max_iter=n_estimators,
            learning_rate=learning_rate,
            max_leaf_nodes=num_leaves,
            min_samples_leaf=20,
            l2_regularization=0.1,
            random_state=7,
        )
    model.fit(Xa[train_idx], ya[train_idx])
    gbdt_pred = model.predict(Xa[test_idx]).tolist()

    ridge_pred = _ridge_predict(
        Xa[train_idx].tolist(), ya[train_idx].tolist(), Xa[test_idx].tolist(), ridge_lambda
    ).tolist()

    test_dates = [row_dates[i] for i in test_idx]
    test_y = [float(ya[i]) for i in test_idx]

    gbdt_ic = _oos_ic_by_date(gbdt_pred, test_y, test_dates)
    ridge_ic = _oos_ic_by_date(ridge_pred, test_y, test_dates)

    def _mean(values: Sequence[float]) -> float:
        return sum(values) / len(values) if values else 0.0

    importances = _permutation_importance(
        model, Xa[test_idx], test_y, test_dates, _mean(list(gbdt_ic.values()))
    )
    importance_rows = sorted(zip(names, importances), key=lambda kv: kv[1], reverse=True)

    return {
        "available": True,
        "backend": backend,
        "factor_names": names,
        "samples": float(len(X)),
        "train_samples": float(len(train_idx)),
        "test_samples": float(len(test_idx)),
        "train_end": cutoff,
        "test_start": test_min,
        "embargo_days": float(embargo_days),
        "gbdt_oos_mean_ic": _mean(list(gbdt_ic.values())),
        "gbdt_oos_ic_std": statistics.pstdev(list(gbdt_ic.values())) if len(gbdt_ic) > 1 else 0.0,
        "ridge_oos_mean_ic": _mean(list(ridge_ic.values())),
        "ridge_oos_ic_std": statistics.pstdev(list(ridge_ic.values())) if len(ridge_ic) > 1 else 0.0,
        "gbdt_oos_ic_by_date": gbdt_ic,
        "ridge_oos_ic_by_date": ridge_ic,
        "feature_importance": importance_rows,
    }


def _permutation_importance(model, X_test, y_test, dates, baseline_ic):
    """Model-agnostic feature importance: mean OOS IC drop when a feature is
    shuffled (higher = more predictive)."""
    import numpy as np

    Xt = np.asarray(X_test, dtype=float).copy()
    rng = np.random.RandomState(0)
    out = []
    for j in range(Xt.shape[1]):
        Xj = Xt.copy()
        rng.shuffle(Xj[:, j])
        preds = model.predict(Xj).tolist()
        ic_j = _oos_ic_by_date(preds, y_test, dates)
        out.append(baseline_ic - (sum(ic_j.values()) / len(ic_j) if ic_j else 0.0))
    return out


def _days_between(a: str, b: str) -> int:
    from datetime import date as Date

    try:
        return (Date.fromisoformat(b) - Date.fromisoformat(a)).days
    except ValueError:
        return 10**9
