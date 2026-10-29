"""StockQuant 只读受限适配器（客户端侧适配器，V5 第 15 节）。

只暴露一个能力：``quant.risk_report`` 的历史统计计算。不暴露证券下单、
交易所凭证或任意脚本执行；所有输出带方法版本、样本量、窗口与 ``source_hash``，
数据不足时输出 ``null`` + 原因，不填零。
"""

from __future__ import annotations

from .risk_report import (
    ADAPTER_ID,
    ADAPTER_VERSION,
    ANNUALIZATION_FACTOR,
    CVAR_CONFIDENCE,
    DISCLAIMER,
    METHOD_VERSION,
    MIN_CVAR_POINTS,
    MIN_VOL_POINTS,
    annualized_volatility,
    historical_cvar,
    max_drawdown,
    risk_rank_without_quant,
    risk_report,
)

__all__ = [
    "ADAPTER_ID",
    "ADAPTER_VERSION",
    "ANNUALIZATION_FACTOR",
    "CVAR_CONFIDENCE",
    "DISCLAIMER",
    "METHOD_VERSION",
    "MIN_CVAR_POINTS",
    "MIN_VOL_POINTS",
    "annualized_volatility",
    "historical_cvar",
    "max_drawdown",
    "risk_rank_without_quant",
    "risk_report",
]