from __future__ import annotations

import json
import math
import statistics
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from stockquant.backtest import BacktestConfig
from stockquant.domain import MarketData
from stockquant.factors import FactorEngine


ACTIONS = ["hold", "add", "reduce", "exit"]


def _forward_return(market: MarketData, symbol: str, start: str, end: str) -> float:
    a = market.bar(symbol, start)
    b = market.bar(symbol, end)
    if a is None or b is None or a.close <= 0 or b.close <= 0:
        return 0.0
    return b.close / a.close - 1.0


def _vol(market: MarketData, symbol: str, date: str, lookback: int = 60) -> float:
    bars = market.history(symbol, date, lookback=lookback + 1)
    rets = [b.close / a.close - 1.0 for a, b in zip(bars, bars[1:]) if a.close > 0 and b.close > 0]
    return statistics.pstdev(rets) * math.sqrt(252.0) if len(rets) > 10 else 0.0


class OfflineActionModel:
    def __init__(self, weights: Dict[str, List[float]], feature_names: List[str], mean: List[float], std: List[float], enabled: bool = False, validation: Optional[Dict[str, float]] = None):
        self.weights = weights
        self.feature_names = feature_names
        self.mean = mean
        self.std = std
        self.enabled = enabled
        self.validation = validation or {}

    @classmethod
    def load(cls, path: str) -> "OfflineActionModel":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(payload["weights"], payload["feature_names"], payload["mean"], payload["std"], payload.get("enabled", False), payload.get("validation", {}))

    def save(self, path: str) -> None:
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"weights": self.weights, "feature_names": self.feature_names, "mean": self.mean, "std": self.std, "enabled": self.enabled, "validation": self.validation},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def predict(self, feature_map: Dict[str, float]) -> Dict[str, object]:
        values = []
        for name in self.feature_names:
            value = float(feature_map.get(name, 0.0))
            mean, std = self.mean[len(values)], self.std[len(values)]
            values.append((value - mean) / std if std > 1e-12 else 0.0)
        values.append(1.0)
        scores = {}
        for action, weights in self.weights.items():
            scores[action] = sum(w * x for w, x in zip(weights, values))
        best = max(scores, key=scores.get)
        return {"action": best if self.enabled else "disabled", "action_scores": scores, "enabled": self.enabled, "validation": self.validation}


def train_offline_action_model(
    market: MarketData,
    config: BacktestConfig,
    horizon: int = 20,
    ridge_lambda: float = 20.0,
    embargo_days: int = 5,
) -> OfflineActionModel:
    import numpy as np

    engine = FactorEngine(config.factors, min_history=config.min_history, sector_neutral=config.sector_neutral)
    dates = market.dates
    rows: List[List[float]] = []
    feature_names = [f.name for f in config.factors] + ["volatility", "score"]
    rewards: Dict[str, List[float]] = {action: [] for action in ACTIONS}
    sample_dates: List[str] = []
    label_end_dates: List[str] = []
    for i in range(0, max(0, len(dates) - horizon), 5):
        date = dates[i]
        future = dates[i + horizon]
        components, diag = engine.components(market, date)
        if diag.scored_count < 5:
            continue
        symbols = set()
        for mapping in components.values():
            symbols.update(mapping)
        for symbol in symbols:
            values = []
            valid = True
            raw_score = 0.0
            used = 0.0
            for spec in config.factors:
                value = components.get(spec.name, {}).get(symbol)
                if value is None:
                    values.append(0.0)
                else:
                    values.append(float(value))
                    raw_score += spec.weight * float(value)
                    used += abs(spec.weight)
            score = raw_score / used if used > 0 else 0.0
            vol = _vol(market, symbol, date)
            values.extend([vol, score])
            ret = _forward_return(market, symbol, date, future)
            cost_buy = 0.0010
            cost_sell = 0.0012
            forward_reward = ret - 0.25 * vol
            action_rewards = {
                "hold": forward_reward,
                "add": forward_reward - cost_buy - 0.10 * vol,
                "reduce": -0.5 * ret - cost_sell,
                "exit": -forward_reward - cost_sell,
            }
            rows.append(values)
            sample_dates.append(date)
            label_end_dates.append(future)
            for action in ACTIONS:
                rewards[action].append(action_rewards[action])
    if len(rows) < 100:
        raise ValueError("Not enough samples for offline action model")
    unique_dates = sorted(set(sample_dates))
    if len(unique_dates) < 10:
        raise ValueError("Not enough unique dates for purged validation")
    val_date_idx = max(1, int(len(unique_dates) * 0.8))
    val_start_date = unique_dates[val_date_idx]
    cutoff_idx = max(0, val_date_idx - max(1, embargo_days) - 1)
    train_cutoff_date = unique_dates[cutoff_idx]
    train_indices = [
        i for i, d in enumerate(sample_dates)
        if d <= train_cutoff_date and label_end_dates[i] < val_start_date
    ]
    valid_indices = [i for i, d in enumerate(sample_dates) if d >= val_start_date]
    if len(train_indices) < 50 or len(valid_indices) < 20:
        raise ValueError("Not enough purged train/validation samples")
    x_train_np = np.asarray([rows[i] for i in train_indices], dtype=float)
    x_valid_np = np.asarray([rows[i] for i in valid_indices], dtype=float)
    mean = x_train_np.mean(axis=0)
    std = x_train_np.std(axis=0)
    std[std < 1e-12] = 1.0
    z_train = (x_train_np - mean) / std
    z_train = np.column_stack([z_train, np.ones(len(z_train))])
    z_valid = (x_valid_np - mean) / std
    z_valid = np.column_stack([z_valid, np.ones(len(z_valid))])
    weights: Dict[str, List[float]] = {}
    penalty = np.eye(z_train.shape[1]) * ridge_lambda
    penalty[-1, -1] = 0.0
    for action in ACTIONS:
        y = np.asarray([rewards[action][i] for i in train_indices], dtype=float)
        try:
            beta = np.linalg.solve(z_train.T @ z_train + penalty, z_train.T @ y)
        except np.linalg.LinAlgError:
            beta = np.linalg.pinv(z_train.T @ z_train + penalty) @ z_train.T @ y
        weights[action] = [float(v) for v in beta]
    valid_rewards = {action: np.asarray([rewards[action][i] for i in valid_indices], dtype=float) for action in ACTIONS}
    predicted = {action: z_valid @ np.asarray(weights[action], dtype=float) for action in ACTIONS}
    chosen = np.argmax(np.column_stack([predicted[a] for a in ACTIONS]), axis=1)
    chosen_reward = np.asarray([valid_rewards[ACTIONS[i]][j] for j, i in enumerate(chosen)], dtype=float)
    hold_reward = valid_rewards["hold"]
    best_reward = np.max(np.column_stack([valid_rewards[a] for a in ACTIONS]), axis=1)
    lift = float(np.mean(chosen_reward - hold_reward))
    action_counts = {ACTIONS[i]: int(np.sum(chosen == i)) for i in range(len(ACTIONS))}
    max_action_share = max(action_counts.values()) / max(1, len(chosen))
    validation = {
        "samples": float(len(rows)),
        "training_samples": float(len(train_indices)),
        "validation_samples": float(len(x_valid_np)),
        "train_cutoff_date": train_cutoff_date,
        "validation_start_date": val_start_date,
        "embargo_days": float(embargo_days),
        "purged": True,
        "lift_vs_hold": lift,
        "oracle_gap": float(np.mean(best_reward - chosen_reward)),
        "positive_lift_rate": float(np.mean(chosen_reward > hold_reward)),
        "max_action_share": float(max_action_share),
        "action_counts": action_counts,
    }
    enabled = lift > 0.005 and validation["positive_lift_rate"] >= 0.5 and max_action_share < 0.8
    return OfflineActionModel(weights, feature_names, mean.tolist(), std.tolist(), enabled=enabled, validation=validation)
