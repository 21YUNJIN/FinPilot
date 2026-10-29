"""Factor combination methods with a common fit/predict interface.

All combiners expose ``fit_predict(X_train, y_train, X_test) -> predictions``
so they can be plugged straight into :func:`stockquant.analysis.cpcv.cpcv_evaluate`.

The question they answer: given several factors that individually show weak
predictive power, how should they be combined? Linear regression is the default,
but it may not be the best combiner — and "more factors" is not automatically
"better model" (Harvey-Liu-Zhu 2016; Kelly-Malamud-Zhou 2024).
"""

from __future__ import annotations

import math
import statistics
from typing import Callable, List, Optional, Sequence

CombinerFn = Callable[[Sequence[Sequence[float]], Sequence[float], Sequence[Sequence[float]]], List[float]]


def _rank(values: Sequence[float]) -> List[float]:
    indexed = sorted(enumerate(values), key=lambda x: x[1])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(indexed):
        j = i
        while j + 1 < len(indexed) and indexed[j + 1][1] == indexed[i][1]:
            j += 1
        avg = (i + j) / 2.0
        for k in range(i, j + 1):
            ranks[indexed[k][0]] = avg
        i = j + 1
    return ranks


def _corr(xs: Sequence[float], ys: Sequence[float]) -> float:
    n = len(xs)
    if n < 5:
        return 0.0
    mx, my = statistics.mean(xs), statistics.mean(ys)
    cov = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    vx = sum((a - mx) ** 2 for a in xs)
    vy = sum((b - my) ** 2 for b in ys)
    if vx <= 1e-16 or vy <= 1e-16:
        return 0.0
    return cov / math.sqrt(vx * vy)


def ridge(lam: float = 50.0) -> CombinerFn:
    """Regularised linear combination (the current baseline)."""
    def f(x_train, y_train, x_test):
        import numpy as np
        a = np.column_stack([np.ones(len(x_train)), np.asarray(x_train, float)])
        b = np.column_stack([np.ones(len(x_test)), np.asarray(x_test, float)])
        pen = np.eye(a.shape[1]) * lam
        pen[0, 0] = 0.0
        beta = np.linalg.solve(a.T @ a + pen, a.T @ np.asarray(y_train, float))
        return (b @ beta).tolist()
    return f


def equal_weight() -> CombinerFn:
    """Plain average of the factor z-scores. No fitting, nothing to overfit."""
    def f(x_train, y_train, x_test):
        return [sum(row) / len(row) for row in x_test]
    return f


def ic_weight() -> CombinerFn:
    """Weight each factor by its cross-sectional IC measured on the training set."""
    def f(x_train, y_train, x_test):
        k = len(x_train[0]) if x_train else 0
        weights = []
        for j in range(k):
            col = [row[j] for row in x_train]
            weights.append(_corr(col, list(y_train)))
        total = sum(abs(w) for w in weights)
        if total <= 1e-12:
            weights = [1.0 / k] * k if k else []
        else:
            weights = [w / total for w in weights]
        return [sum(w * v for w, v in zip(weights, row)) for row in x_test]
    return f


def gbdt(max_iter: int = 200, learning_rate: float = 0.05, max_leaf_nodes: int = 31) -> CombinerFn:
    """Non-linear tree ensemble (scikit-learn HistGradientBoosting)."""
    def f(x_train, y_train, x_test):
        from sklearn.ensemble import HistGradientBoostingRegressor
        model = HistGradientBoostingRegressor(
            max_iter=max_iter, learning_rate=learning_rate,
            max_leaf_nodes=max_leaf_nodes, min_samples_leaf=50,
            l2_regularization=1.0, random_state=7,
        )
        model.fit(x_train, y_train)
        return model.predict(x_test).tolist()
    return f


def meta_label(primary: Optional[CombinerFn] = None, threshold: float = 0.55) -> CombinerFn:
    """Meta-labeling (Lopez de Prado, AFML ch.3).

    The primary model predicts the *direction*; a secondary classifier learns
    when the primary is right and is used to (a) size the bet and (b) flip the
    sign when the primary is judged unreliable.
    """
    primary = primary or ridge()

    def f(x_train, y_train, x_test):
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.preprocessing import StandardScaler

        pred_train = np.asarray(primary(x_train, y_train, x_train), dtype=float)
        pred_test = np.asarray(primary(x_train, y_train, x_test), dtype=float)
        actual = np.asarray(y_train, dtype=float)
        # meta-label: did the primary get the direction right?
        y_meta = (np.sign(pred_train) == np.sign(actual)).astype(int)
        if y_meta.min() == y_meta.max():
            return pred_test.tolist()
        scaler = StandardScaler().fit(x_train)
        clf = LogisticRegression(max_iter=500, C=1.0)
        clf.fit(scaler.transform(x_train), y_meta)
        proba = clf.predict_proba(scaler.transform(x_test))[:, 1]
        # confidence in [-1, 1]; flips the sign when the primary looks wrong
        confidence = 2.0 * proba - 1.0
        confidence = np.where(np.abs(confidence) < (2.0 * threshold - 1.0), 0.0, confidence)
        return (pred_test * confidence).tolist()
    return f
