"""产品条款、开放日、截止时间、到账与撤单（V5 第 11.2 节 S3-05、T11／T43）。

原则：

- **条款驱动**：撤单／冷静期只看该产品版本的条款，不普遍承诺"所有理财都能撤回"，
  也不承诺"全部有冷静期"。没有条款依据时返回不可撤。
- **不承诺到账**：预计确认／到账日期按交易日历与截止时间推算，输出里明确
  ``guaranteed: False``；实际以产品确认结果为准。
- **整数定点**：份额用 1e-8 定点整数计算，估算份额与正式确认份额分开保存，
  估算结果永不写进 ``confirmed_shares_e8``。
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime, time, timedelta
from typing import Any

from ...errors import FinPilotError
from ...money import fee_for, format_minor, format_shares_e8
from ...timeutil import SHANGHAI, format_iso, to_shanghai, utcnow

TERMS_ENGINE_VERSION = "wealth-terms-2026.10"
DEFAULT_CUTOFF_TIME = "15:00"
DEFAULT_SETTLEMENT_DAYS = 1

#: 条款里出现这些片段即视为"不可撤单"。
CANCEL_DENY_TOKENS: tuple[str, ...] = (
    "不可撤",
    "不支持撤",
    "不得撤",
    "无法撤",
    "不可撤回",
    "不可取消",
    "no_cancel",
    "not_cancellable",
)

#: 没有任何可撤依据时的默认结论。
NOT_CANCELLABLE_NOTE = "未找到支持撤单的条款，按不可撤处理；不得普遍承诺理财可撤回。"

_TIME_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::(\d{2}))?$")

_WEEKDAY_TOKENS: dict[str, int] = {
    "mon": 0, "monday": 0, "周一": 0, "星期一": 0,
    "tue": 1, "tuesday": 1, "周二": 1, "星期二": 1,
    "wed": 2, "wednesday": 2, "周三": 2, "星期三": 2,
    "thu": 3, "thursday": 3, "周四": 3, "星期四": 3,
    "fri": 4, "friday": 4, "周五": 4, "星期五": 4,
    "sat": 5, "saturday": 5, "周六": 5, "星期六": 5,
    "sun": 6, "sunday": 6, "周日": 6, "星期日": 6, "星期天": 6,
}
_ALL_DAYS_TOKENS = frozenset({"daily", "everyday", "all", "all_days", "每天", "每日", "全周"})
_WEEKDAY_ONLY_TOKENS = frozenset({"workday", "weekday", "workdays", "weekdays", "工作日", "交易日", "trading_day"})


def _get(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, Mapping):
        return obj.get(name, default)
    return getattr(obj, name, default)


# ---------------------------------------------------------------------------
# 开放日与截止时间
# ---------------------------------------------------------------------------


def normalize_open_days(product: Any) -> dict[str, Any]:
    """把 ``open_days`` 规范化为 ``{"kind","weekdays","raw"}``。

    支持：整数 0—6（0=周一，与 ``datetime.weekday()`` 一致）、英文／中文星期、
    ``daily``（每日）、``workday``／``trading_day``（工作日）。空列表按工作日处理，
    这是最保守的默认，避免把周末当成开放日。
    """
    raw = _get(product, "open_days", []) or []
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, Sequence):
        raise FinPilotError("PARAM_INVALID", "open_days 必须是列表。")

    weekdays: set[int] = set()
    force_all = False
    for item in raw:
        if isinstance(item, bool):
            raise FinPilotError("PARAM_INVALID", "open_days 不接受布尔值。")
        if isinstance(item, int):
            if not 0 <= item <= 6:
                raise FinPilotError(
                    "PARAM_INVALID", "open_days 的整数取值必须在 0—6。",
                    details={"reason_codes": ["bad_open_day"], "value": item},
                )
            weekdays.add(item)
            continue
        token = str(item).strip().lower()
        if not token:
            continue
        if token in _ALL_DAYS_TOKENS:
            force_all = True
            continue
        if token in _WEEKDAY_ONLY_TOKENS:
            weekdays |= {0, 1, 2, 3, 4}
            continue
        if token in _WEEKDAY_TOKENS:
            weekdays.add(_WEEKDAY_TOKENS[token])
            continue
        raise FinPilotError(
            "PARAM_INVALID", "无法识别的开放日取值。",
            details={"reason_codes": ["bad_open_day"], "value": token},
        )

    if force_all:
        return {"kind": "all_days", "weekdays": [0, 1, 2, 3, 4, 5, 6], "raw": list(raw)}
    if not weekdays:
        return {"kind": "weekdays", "weekdays": [0, 1, 2, 3, 4], "raw": list(raw)}
    if weekdays == {0, 1, 2, 3, 4}:
        # 显式列出周一到周五与"工作日"等价，归一化后按工作日解释
        return {"kind": "weekdays", "weekdays": [0, 1, 2, 3, 4], "raw": list(raw)}
    if weekdays == {0, 1, 2, 3, 4, 5, 6}:
        return {"kind": "all_days", "weekdays": [0, 1, 2, 3, 4, 5, 6], "raw": list(raw)}
    return {"kind": "explicit", "weekdays": sorted(weekdays), "raw": list(raw)}


def is_open_day(product: Any, day: date) -> bool:
    rule = normalize_open_days(product)
    if rule["kind"] == "all_days":
        return True
    return day.weekday() in rule["weekdays"]


def next_open_day(product: Any, day: date, *, inclusive: bool = True) -> date:
    """返回 day 当天或之后的第一个开放日；日历最多向前找 400 天。"""
    cursor = day if inclusive else day + timedelta(days=1)
    for _ in range(400):
        if is_open_day(product, cursor):
            return cursor
        cursor += timedelta(days=1)
    raise FinPilotError("SYSTEM_ERROR", "交易日历无法解析出开放日。", details={"reason_codes": ["empty_calendar"]})


def cutoff_time(product: Any) -> time:
    raw = str(_get(product, "cutoff_time", DEFAULT_CUTOFF_TIME) or DEFAULT_CUTOFF_TIME).strip()
    match = _TIME_RE.match(raw)
    if not match:
        raise FinPilotError(
            "PARAM_INVALID", "cutoff_time 格式必须为 HH:MM。",
            details={"reason_codes": ["bad_cutoff_time"], "cutoff_time": raw},
        )
    hour, minute = int(match.group(1)), int(match.group(2))
    second = int(match.group(3) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise FinPilotError(
            "PARAM_INVALID", "cutoff_time 超出有效时间范围。",
            details={"reason_codes": ["bad_cutoff_time"], "cutoff_time": raw},
        )
    return time(hour, minute, second)


def cutoff_datetime(product: Any, day: date) -> datetime:
    return datetime.combine(day, cutoff_time(product), tzinfo=SHANGHAI)


def settlement_days(product: Any) -> int:
    value = _get(product, "settlement_days", DEFAULT_SETTLEMENT_DAYS)
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise FinPilotError("PARAM_INVALID", "settlement_days 必须是整数。") from exc
    if days < 0:
        raise FinPilotError("PARAM_INVALID", "settlement_days 不能为负。")
    return days


def trade_day_for(product: Any, submitted_at: datetime) -> date:
    """提交时刻对应的交易日：开放日且不晚于截止时间算当日，否则顺延。"""
    local = to_shanghai(submitted_at)
    today = local.date()
    if is_open_day(product, today) and local <= cutoff_datetime(product, today):
        return today
    return next_open_day(product, today, inclusive=False)


def expected_settlement_date(product: Any, submitted_at: datetime) -> date:
    """按交易日历与截止时间推算预计到账日（非承诺）。"""
    return settlement_explanation(product, submitted_at)["expected_settlement_date"]


def settlement_explanation(product: Any, submitted_at: datetime) -> dict[str, Any]:
    detail = _settlement_detail(product, submitted_at)
    return {
        **detail,
        "guaranteed": False,
        "note": "预计日期按产品条款与交易日历推算，不构成到账承诺；以产品确认结果为准。",
    }


def _settlement_detail(product: Any, submitted_at: datetime) -> dict[str, Any]:
    local = to_shanghai(submitted_at)
    today = local.date()
    trading = trade_day_for(product, submitted_at)
    days = settlement_days(product)
    settle = trading
    for _ in range(days):
        settle = next_open_day(product, settle, inclusive=False)
    return {
        "submitted_at": format_iso(submitted_at),
        "submitted_local_date": today.isoformat(),
        "cutoff_time": cutoff_time(product).strftime("%H:%M"),
        "cutoff_at": format_iso(cutoff_datetime(product, today)),
        "open_day_rule": normalize_open_days(product),
        "trade_day": trading.isoformat(),
        "deferred_to_next_open_day": trading != today,
        "settlement_days": days,
        "expected_settlement_date": settle,
        "expected_settlement_date_iso": settle.isoformat(),
        "terms_version": str(_get(product, "terms_version", "") or ""),
    }


# ---------------------------------------------------------------------------
# 撤单与冷静期
# ---------------------------------------------------------------------------


def _denies_cancel(terms_text: str) -> bool:
    lowered = terms_text.lower()
    return any(token.lower() in lowered for token in CANCEL_DENY_TOKENS)


def can_cancel(product: Any, submitted_at: datetime, now: datetime | None = None) -> dict[str, Any]:
    """是否可撤：只有条款支持且仍在窗口内才返回 True。

    规则（版本化，随条款文本一起冻结）：

    1. 没有 ``cancel_terms`` 依据 → 不可撤（``terms_not_supported``）。
    2. 条款文本含"不可撤/不支持撤/不可取消"等否定片段 → 不可撤（``terms_disallow_cancel``）。
    3. ``cooling_off_days > 0``：窗口为提交时刻起 N 个自然日（``within_cooling_off``）。
    4. ``cooling_off_days == 0``：只能在提交当日截止时间前撤单（``within_cutoff_window``）。
    """
    reference = to_shanghai(now or utcnow())
    submitted_local = to_shanghai(submitted_at)
    terms_text = str(_get(product, "cancel_terms", "") or "").strip()
    cooling = int(_get(product, "cooling_off_days", 0) or 0)

    base = {
        "cooling_off_days": cooling,
        "cancel_terms": terms_text,
        "terms_version": str(_get(product, "terms_version", "") or ""),
        "terms_engine_version": TERMS_ENGINE_VERSION,
        "submitted_at": format_iso(submitted_at),
        "checked_at": format_iso(reference),
        "basis": "product_terms",
    }

    if not terms_text:
        return {
            **base,
            "can_cancel": False,
            "reason_code": "terms_not_supported",
            "window_end": None,
            "note": NOT_CANCELLABLE_NOTE,
        }

    if _denies_cancel(terms_text):
        return {
            **base,
            "can_cancel": False,
            "reason_code": "terms_disallow_cancel",
            "window_end": None,
            "note": f"产品条款明确不可撤：{terms_text}",
        }

    if cooling > 0:
        window_end = submitted_local + timedelta(days=cooling)
        allowed = reference <= window_end
        return {
            **base,
            "can_cancel": allowed,
            "reason_code": "within_cooling_off" if allowed else "window_closed",
            "window_end": format_iso(window_end),
            "note": (
                f"冷静期 {cooling} 个自然日，窗口内可撤。"
                if allowed
                else f"冷静期已于 {format_iso(window_end)} 结束，不可撤。"
            ),
        }

    deadline = cutoff_datetime(product, submitted_local.date())
    allowed = is_open_day(product, submitted_local.date()) and reference <= deadline
    return {
        **base,
        "can_cancel": allowed,
        "reason_code": "within_cutoff_window" if allowed else "window_closed",
        "window_end": format_iso(deadline),
        "note": (
            f"无冷静期，仅在提交当日 {cutoff_time(product).strftime('%H:%M')} 截止时间前可撤。"
            if allowed
            else f"提交当日截止时间 {format_iso(deadline)} 已过，不可撤。"
        ),
    }


# ---------------------------------------------------------------------------
# 份额估算（整数定点，估算 ≠ 正式）
# ---------------------------------------------------------------------------


def estimate_shares(amount_minor: int, nav_e6: int | None, fee_minor: int = 0) -> dict[str, Any]:
    """估算份额：``shares_e8 = (amount - fee) * 1e14 // nav_e6``（向下取整）。

    返回结构里 ``is_estimate`` 恒为 True，``confirmed_shares_e8`` 恒为 None：
    估算结果永远不会被当成正式确认份额。
    """
    amount = int(amount_minor)
    fee = int(fee_minor)
    if amount <= 0:
        raise FinPilotError("PARAM_INVALID", "申购金额必须大于 0。")
    if fee < 0:
        raise FinPilotError("PARAM_INVALID", "手续费不能为负。")
    net = amount - fee
    if net <= 0:
        raise FinPilotError(
            "PARAM_INVALID",
            "手续费不低于申购金额，无法形成份额。",
            details={"reason_codes": ["fee_not_less_than_amount"], "amount_minor": amount, "fee_minor": fee},
        )

    result: dict[str, Any] = {
        "amount_minor": amount,
        "fee_minor": fee,
        "net_amount_minor": net,
        "nav_e6": None if nav_e6 is None else int(nav_e6),
        "formula": "shares_e8 = floor((amount_minor - fee_minor) * 1e14 / nav_e6)",
        "rounding": "floor",
        "is_estimate": True,
        "confirmed_shares_e8": None,
        "terms_engine_version": TERMS_ENGINE_VERSION,
    }

    if nav_e6 is None:
        return {
            **result,
            "estimated_shares_e8": None,
            "estimated_shares": None,
            "status": "not_applicable",
            "reason_code": "nav_unavailable",
            "note": "净值未知，只能给估算区间；不得编造精确持仓。",
        }
    nav = int(nav_e6)
    if nav <= 0:
        raise FinPilotError(
            "PARAM_INVALID", "净值必须为正数。",
            details={"reason_codes": ["bad_nav"], "nav_e6": nav},
        )

    shares_e8 = net * (10**14) // nav
    return {
        **result,
        "estimated_shares_e8": shares_e8,
        "estimated_shares": format_shares_e8(shares_e8),
        "status": "ok",
        "reason_code": None,
        "note": "估算份额与正式确认份额分开保存；受理不等于确认份额。",
    }


def estimate_shares_range(
    amount_minor: int,
    nav_min_e6: int | None,
    nav_max_e6: int | None,
    fee_minor: int = 0,
) -> dict[str, Any]:
    """按观察到的净值区间给份额估算范围（低净值对应高份额）。"""
    if nav_min_e6 is None or nav_max_e6 is None:
        return {
            "min_shares_e8": None,
            "max_shares_e8": None,
            "status": "not_applicable",
            "reason_code": "nav_unavailable",
            "note": "没有可核对的净值数据，不给估算区间。",
        }
    low = estimate_shares(amount_minor, int(nav_max_e6), fee_minor)["estimated_shares_e8"]
    high = estimate_shares(amount_minor, int(nav_min_e6), fee_minor)["estimated_shares_e8"]
    return {
        "min_shares_e8": low,
        "max_shares_e8": high,
        "min_shares": format_shares_e8(low),
        "max_shares": format_shares_e8(high),
        "nav_min_e6": int(nav_min_e6),
        "nav_max_e6": int(nav_max_e6),
        "status": "ok",
        "is_estimate": True,
        "note": "区间仅由已观察净值上下限与费用推算，不是收益或份额承诺。",
    }


# ---------------------------------------------------------------------------
# 条款视图
# ---------------------------------------------------------------------------


def product_fee_minor(product: Any, amount_minor: int) -> int:
    return fee_for(
        int(amount_minor),
        int(_get(product, "fee_bps", 0) or 0),
        int(_get(product, "fee_fixed_minor", 0) or 0),
    )


def product_terms(product: Any) -> dict[str, Any]:
    """产品条款视图：风险、费用、期限、开放时间、到账与撤单规则。"""
    open_rule = normalize_open_days(product)
    return {
        "product_id": _get(product, "product_id"),
        "name": _get(product, "name"),
        "issuer": _get(product, "issuer"),
        "product_type": _get(product, "product_type"),
        "issuer_risk_level": _get(product, "issuer_risk_level"),
        "terms_version": str(_get(product, "terms_version", "") or ""),
        "min_minor": int(_get(product, "min_minor", 0) or 0),
        "min_amount": format_minor(int(_get(product, "min_minor", 0) or 0)),
        "fee_bps": int(_get(product, "fee_bps", 0) or 0),
        "fee_fixed_minor": int(_get(product, "fee_fixed_minor", 0) or 0),
        "fee_formula": "fee = floor(amount_minor * fee_bps / 10000) + fee_fixed_minor",
        "term_days": int(_get(product, "term_days", 0) or 0),
        "open_days": open_rule,
        "open_day_rule": {
            "kind": open_rule["kind"],
            "weekdays": open_rule["weekdays"],
            "text": {
                "all_days": "每个自然日均为开放日",
                "weekdays": "工作日（周一至周五）为开放日",
                "explicit": "按产品登记的开放日",
            }[open_rule["kind"]],
        },
        "cutoff_time": cutoff_time(product).strftime("%H:%M"),
        "settlement_days": settlement_days(product),
        "liquidity_terms": str(_get(product, "liquidity_terms", "") or ""),
        "cancel_terms": str(_get(product, "cancel_terms", "") or ""),
        "cooling_off_days": int(_get(product, "cooling_off_days", 0) or 0),
        "history_ref": str(_get(product, "history_ref", "") or ""),
        "is_active": bool(_get(product, "is_active", True)),
        "terms_engine_version": TERMS_ENGINE_VERSION,
        "return_guaranteed": False,
        "settlement_guaranteed": False,
        "note": "条款按产品版本解释；预计到账时间不是承诺，撤单只在条款支持的窗口内可用。",
    }


__all__ = [
    "CANCEL_DENY_TOKENS",
    "DEFAULT_CUTOFF_TIME",
    "DEFAULT_SETTLEMENT_DAYS",
    "NOT_CANCELLABLE_NOTE",
    "TERMS_ENGINE_VERSION",
    "can_cancel",
    "cutoff_datetime",
    "cutoff_time",
    "estimate_shares",
    "estimate_shares_range",
    "expected_settlement_date",
    "is_open_day",
    "next_open_day",
    "normalize_open_days",
    "product_fee_minor",
    "product_terms",
    "settlement_days",
    "settlement_explanation",
    "trade_day_for",
]