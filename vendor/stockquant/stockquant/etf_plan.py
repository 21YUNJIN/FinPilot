"""Plan identity and hard execution gates for the ETF pilot.

The ETF order list is intentionally boring, but it must never be possible for a
stale data file, an expired plan, or an unconfigured account to appear
executable.  This module keeps that decision in one place so the CLI, the
dashboard API and future tools cannot drift apart.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from zoneinfo import ZoneInfo

from stockquant.data.calendar import DEFAULT_CALENDAR_PATH, next_trading_day

SHANGHAI = ZoneInfo("Asia/Shanghai")
PLAN_SCHEMA_VERSION = 2
DEFAULT_MIN_EXECUTABLE_CAPITAL = 50_000.0


def _aware(value: Optional[Any], default: Optional[datetime] = None) -> datetime:
    """Parse an ISO timestamp and normalise it to Asia/Shanghai.

    Naive timestamps are interpreted as local Shanghai time.  This is
    deliberate: the plan and the dashboard are both single-user tools running
    on an Asia/Shanghai machine, and a missing offset should not silently
    become UTC.
    """
    if value is None:
        return default or datetime.now(SHANGHAI)
    if isinstance(value, datetime):
        out = value
    elif isinstance(value, date):
        out = datetime.combine(value, datetime.min.time())
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            out = datetime.fromisoformat(text)
        except ValueError:
            out = datetime.now(SHANGHAI)
    if out.tzinfo is None:
        out = out.replace(tzinfo=SHANGHAI)
    return out.astimezone(SHANGHAI)


def stable_hash(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def compute_plan_id(*, signal_date: str, execute_on: str, capital: float,
                    data_hash: str, config_hash: str, orders: Iterable[Dict[str, Any]],
                    status: str = "ready", blocked_reasons: Iterable[str] = ()) -> str:
    """Deterministic id for the exact plan, not for the wall-clock generation time."""
    identity = {
        "schema": PLAN_SCHEMA_VERSION,
        "status": status,
        "signal_date": signal_date,
        "execute_on": execute_on,
        "capital": round(float(capital), 2),
        "data_hash": data_hash,
        "config_hash": config_hash,
        "orders": list(orders),
        "blocked_reasons": list(blocked_reasons),
    }
    return "etf-" + stable_hash(identity)[:20]


def plan_expiry(execute_on: str, *, open_grace_minutes: int = 5) -> str:
    """End of the executable opening window, as an ISO timestamp.

    The strategy is explicitly "next trading day open".  A plan that is opened
    at 10:30 is not the same experiment, so the default window closes five
    minutes after the 09:30 open.
    """
    from datetime import timedelta
    d = date.fromisoformat(execute_on)
    expiry = datetime(d.year, d.month, d.day, 9, 30, tzinfo=SHANGHAI) + timedelta(minutes=open_grace_minutes)
    return expiry.isoformat()


def next_execution_date(signal_date: str, calendar_path: str = DEFAULT_CALENDAR_PATH) -> Optional[str]:
    return next_trading_day(signal_date, path=calendar_path)


_REASON_MESSAGES = {
    "missing_plan": "没有计划文件",
    "legacy_plan": "计划是旧格式，缺少硬门控元数据（plan_id/execute_on/expires_at），禁止执行",
    "plan_status_blocked": "计划生成时已被硬门控阻断",
    "plan_status_demo": "这是演示计划，不是可执行计划",
    "plan_expired": "计划已过期，超过下一交易日开盘执行窗口",
    "execute_date_passed": "计划的绝对执行日期已经过去",
    "data_not_fresh": "计划使用的数据不是最新已完成交易日",
    "capital_not_configured": "账户本金未在 settings.json 中设置",
    "capital_below_gate": "账户本金低于经济可执行门槛",
    "plan_capital_below_gate": "计划本金低于经济可执行门槛",
    "plan_capital_missing": "计划没有记录本金",
    "capital_mismatch": "计划本金与当前账户本金不一致，必须重新生成",
    "execution_not_allowed": "计划明确标记为不可执行",
}


def evaluate_plan_gate(plan: Dict[str, Any], settings: Dict[str, Any],
                       now: Optional[Any] = None) -> Dict[str, Any]:
    """Return the single source of truth for whether a plan may be executed.

    ``ok`` is intentionally conservative.  A plan must have schema v2 metadata,
    be status=ready, be inside its opening window, have fresh-data attestation,
    and match a configured account whose capital clears the economic gate.
    """
    now_dt = _aware(now)
    reasons: List[str] = []
    messages: List[str] = []
    plan_block_reasons: List[str] = []
    plan = plan or {}
    settings = settings or {}

    if not plan:
        reasons.append("missing_plan")
    else:
        try:
            schema = int(plan.get("schema_version") or 0)
        except Exception:
            schema = 0
        if schema < PLAN_SCHEMA_VERSION or not plan.get("plan_id") or not plan.get("execute_on") or not plan.get("expires_at"):
            reasons.append("legacy_plan")

        status = str(plan.get("status") or "unknown")
        if status == "blocked":
            reasons.append("plan_status_blocked")
            plan_block_reasons = [str(x) for x in (plan.get("blocked_reasons") or []) if str(x).strip()]
            messages.extend(plan_block_reasons)
        elif status == "demo":
            reasons.append("plan_status_demo")
        elif status != "ready":
            reasons.append("plan_status_blocked")
            messages.append(f"未知计划状态: {status}")

        execute_on = plan.get("execute_on")
        expires_at = plan.get("expires_at")
        if execute_on:
            try:
                if now_dt.date() > date.fromisoformat(str(execute_on)):
                    reasons.append("execute_date_passed")
            except ValueError:
                reasons.append("legacy_plan")
        if expires_at:
            if now_dt > _aware(expires_at):
                reasons.append("plan_expired")

        if plan.get("data_fresh") is not True:
            reasons.append("data_not_fresh")
        if plan.get("execution_allowed") is False:
            reasons.append("execution_not_allowed")

    try:
        min_capital = float(settings.get("min_executable_capital") or DEFAULT_MIN_EXECUTABLE_CAPITAL)
    except Exception:
        min_capital = DEFAULT_MIN_EXECUTABLE_CAPITAL
    configured = settings.get("capital")
    try:
        configured_f = float(configured) if configured not in (None, "") else None
    except Exception:
        configured_f = None
    if configured_f is None:
        reasons.append("capital_not_configured")
    elif configured_f < min_capital:
        reasons.append("capital_below_gate")

    try:
        plan_capital = float(plan.get("capital")) if plan.get("capital") not in (None, "") else None
    except Exception:
        plan_capital = None
    if plan_capital is None:
        reasons.append("plan_capital_missing")
    elif plan_capital < min_capital:
        reasons.append("plan_capital_below_gate")
    if configured_f is not None and plan_capital is not None and abs(configured_f - plan_capital) > 0.01:
        reasons.append("capital_mismatch")

    # Preserve first occurrence order while dropping duplicates.
    reasons = list(dict.fromkeys(reasons))
    for code in reasons:
        if code in {"plan_status_blocked", "execution_not_allowed"} and plan_block_reasons:
            continue
        msg = _REASON_MESSAGES.get(code, code)
        if msg not in messages:
            messages.append(msg)
    return {
        "ok": not reasons,
        "reasons": reasons,
        "messages": messages,
        "status": str(plan.get("status") or "unknown"),
        "now": now_dt.isoformat(),
        "min_capital": min_capital,
        "configured_capital": configured_f,
        "plan_capital": plan_capital,
        "execute_on": plan.get("execute_on"),
        "expires_at": plan.get("expires_at"),
        "data_asof": plan.get("asof") or plan.get("data_asof"),
    }
