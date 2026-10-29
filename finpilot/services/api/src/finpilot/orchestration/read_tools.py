"""计划中的只读工具处理器（报价、余额等）。

只读步骤不产生资金效果，但必须有确定性实现，否则计划会以"能力待交付"终止；
这里把只读能力接到适配器与领域只读函数上，禁止伪造结果。
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..errors import FinPilotError
from ..kernel.identity import AuthContext
from ..kernel.provider_adapter import get_adapter
from ..kernel.snapshots import get_snapshot
from ..models import Operation


def _provider_for(session: Session, operation: Operation) -> str:
    if operation.provider_id:
        return operation.provider_id
    from ..kernel.provider_adapter import default_provider_id
    return default_provider_id(operation.tool)


def quote_handler(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """商户报价：结构化金额与条款，自由文本单列 untrusted_text。"""
    args = operation.typed_args or {}
    merchant_id = args.get("merchant_id")
    offer_code = args.get("offer_code")
    if not merchant_id or not offer_code:
        raise FinPilotError("PARAM_INVALID", "缺少商户或报价代码。")
    adapter = get_adapter(_provider_for(session, operation))
    quote = adapter.quote(str(merchant_id), str(offer_code))
    warnings: list[str] = []
    if not quote.get("inventory"):
        warnings.append("当前库存为 0，执行预订会被拒绝")
    return {
        "type": "merchant_quote",
        "data": quote,
        "source": f"adapter:{adapter.provider_id}",
        "version": str(quote.get("terms_version") or "2026.10"),
        "warnings": warnings,
        "untrusted_text": quote.get("untrusted_text"),
        "observed_at": quote.get("observed_at"),
    }


def quote_status_handler(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    adapter = get_adapter(_provider_for(session, operation))
    ref = (operation.typed_args or {}).get("stable_operation_ref") or (operation.typed_args or {}).get("operation_id")
    if not ref:
        raise FinPilotError("PARAM_INVALID", "缺少稳定的业务键。")
    result = adapter.query(stable_operation_ref=str(ref))
    return {
        "type": "merchant_status",
        "data": {"provider_ref": result.provider_ref, "status": result.status, "raw": result.raw},
        "source": f"adapter:{adapter.provider_id}",
        "version": "2026.10",
        "warnings": [],
    }


def account_balance_handler(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    snapshot = get_snapshot(session, ctx)
    return {
        "type": "account_snapshot",
        "data": snapshot,
        "source": "local_ledger",
        "version": str(snapshot.get("policy_version") or ""),
        "warnings": [] if snapshot.get("accounts") else ["没有可访问的账户"],
    }


def card_status_handler(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    from ..domains.cards.service import list_cards
    ctx.require_customer()
    return {"type": "card_status", "data": {"cards": list_cards(session, ctx)},
            "source": "cards", "version": "2026.10", "warnings": []}


def quant_risk_handler(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    from sqlalchemy import select
    from ..models import WealthProduct
    from ..timeutil import business_day, utcnow
    from ..adapters.stockquant.remote import fetch_reports
    args = operation.typed_args or {}
    ids = args.get("asset_ids") or []
    stmt = select(WealthProduct).where(WealthProduct.is_active.is_(True))
    if ids:
        stmt = stmt.where(WealthProduct.product_id.in_(ids))
    products = list(session.scalars(stmt.order_by(WealthProduct.product_id)))
    reports = fetch_reports(session, products, business_day(utcnow()))
    return {"type": "quant_risk_report", "data": {"products": list(reports.values()),
            "portfolio": {"value": None, "reason": "逐产品报告，未合成组合风险"}},
            "source": "stockquant_http", "version": "sq-wealth-risk/1.0.0",
            "warnings": ["合成净值；历史统计不代表未来表现"]}


READ_HANDLERS = {
    "card.status": card_status_handler,
    "quant.risk_report": quant_risk_handler,
    "merchant.quote": quote_handler,
    "merchant.status": quote_status_handler,
    "account.balance": account_balance_handler,
}
