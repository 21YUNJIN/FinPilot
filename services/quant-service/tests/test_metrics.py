"""Metric correctness and the SRS §15.1 guardrails."""
import math
import statistics

import pytest

from stockquant_bank import metrics as m


def test_annual_volatility_matches_population_definition():
    returns = [0.01, -0.005, 0.002, 0.0, -0.01, 0.004]
    expected = statistics.pstdev(returns) * math.sqrt(252)
    assert m.annual_volatility(returns) == pytest.approx(expected)


def test_max_drawdown_uses_whole_window():
    nav = [100, 120, 90, 110, 80]
    assert m.max_drawdown(nav) == pytest.approx(-1 / 3)


def test_cvar_is_mean_of_worst_tail_and_negative():
    returns = [i / 10000 for i in range(100)]  # 0.0000 .. 0.0099
    got = m.historical_cvar(returns, 0.05)
    assert got == pytest.approx(statistics.mean(sorted(returns)[:5]))


def test_cvar_parity_with_stockquant_optimizer():
    """Our tail definition must equal StockQuant's portfolio_cvar."""
    optimizer = pytest.importorskip("stockquant.portfolio.optimizer")
    returns = [((i * 37) % 101 - 50) / 10000 for i in range(300)]
    ours = m.historical_cvar(returns, 0.05)
    theirs = optimizer.portfolio_cvar([[r] for r in returns], [1.0], alpha=0.05)
    assert ours == pytest.approx(theirs, abs=1e-12)


def _nav(n):
    return [1.0 + i / 100000 for i in range(n)]


def test_volatility_withheld_below_60_returns():
    report = m.compute_risk_metrics(_nav(60), as_of="2026-10-23")  # 59 returns
    assert report["annual_volatility"]["value"] is None
    assert report["annual_volatility"]["na_reason"] == "insufficient_history_min_60_returns"
    assert report["max_drawdown"]["value"] is not None


def test_volatility_reported_at_60_returns():
    report = m.compute_risk_metrics(_nav(61), as_of="2026-10-23")
    assert report["annual_volatility"]["value"] is not None


def test_cvar_withheld_below_252_returns():
    report = m.compute_risk_metrics(_nav(252), as_of="2026-10-23")  # 251 returns
    assert report["historical_cvar_95"]["value"] is None
    assert report["historical_cvar_95"]["na_reason"] == "insufficient_history_min_252_returns"

    report = m.compute_risk_metrics(_nav(253), as_of="2026-10-23")
    assert report["historical_cvar_95"]["value"] is not None


def test_risk_band_is_diagnostic_and_bumps_on_deep_drawdown():
    low_vol = {"annual_volatility": {"value": 0.01}, "max_drawdown": {"value": -0.05}}
    assert m.risk_band(low_vol)["band"] == "R1"

    deep = {"annual_volatility": {"value": 0.01}, "max_drawdown": {"value": -0.20}}
    assert m.risk_band(deep)["band"] == "R2"

    missing = {"annual_volatility": {"value": None}, "max_drawdown": {"value": None}}
    assert m.risk_band(missing)["band"] is None


def test_sharpe_uses_stockquant_autocorrelation_adjusted_se():
    returns = [0.001 + (1 if i % 3 else -0.0005) for i in range(300)]
    report = m.compute_risk_metrics([1.0] + [1.0 * (1 + r) for r in [0.0] + returns], as_of="2026-10-23")
    assert report["sharpe"]["value"] is not None
    assert report["sharpe_t_stat"]["value"] is not None
    assert report["autocorrelation_factor"]["value"] >= 1e-6