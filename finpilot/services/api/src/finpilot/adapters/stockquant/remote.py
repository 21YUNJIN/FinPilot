"""Map bank NAVs to the independent StockQuant service without granting write access."""
from __future__ import annotations

import statistics
from datetime import date
from sqlalchemy import select

from ...config import get_settings
from ...models import ProductNav
from .http_client import StockQuantRiskClient, QuantContractError, QuantUnavailable
from .risk_report import _is_non_nav


def unavailable(product_id: str, code: str, message: str) -> dict:
    return {"product_id": product_id, "status": "unavailable", "reason_code": code,
            "message": message, "metrics": {}, "warnings": [message],
            "provenance": {"adapter": "stockquant_http", "method_version": "sq-wealth-risk/1.0.0"}}


def fetch_reports(session, products, as_of: date) -> dict:
    """Send catalog facts only; never transmit accounts, profiles, credentials or orders."""
    results, payloads = {}, []
    for product in products:
        non_nav, reason = _is_non_nav(product)
        if non_nav:
            results[product.product_id] = unavailable(product.product_id, "NON_NAV_PRODUCT", reason)
            continue
        navs = list(session.scalars(select(ProductNav).where(
            ProductNav.product_id == product.product_id, ProductNav.as_of <= as_of
        ).order_by(ProductNav.as_of, ProductNav.nav_id)))
        gaps = [(b.as_of-a.as_of).days for a,b in zip(navs, navs[1:])]
        if gaps and (statistics.median(gaps) > 5 or max(gaps) > 10):
            results[product.product_id] = unavailable(product.product_id, "NON_DAILY_OR_GAPPED_NAV",
                "净值不是连续日频或存在超过 10 天的缺口，不套用 252 日年化。")
            continue
        payloads.append({"product_id": product.product_id, "product_name": product.name,
            "product_type": product.product_type, "official_risk_level": product.issuer_risk_level,
            "issuer": product.issuer, "nav_source": product.history_ref or "finpilot_synthetic_product_nav",
            "nav": [{"date": n.as_of.isoformat(), "value": n.nav_e6 / 1_000_000} for n in navs]})
    if not payloads:
        return results
    settings = get_settings()
    if not settings.stockquant_service_token:
        for p in payloads:
            results[p['product_id']] = unavailable(p['product_id'], 'QUANT_NOT_CONFIGURED', '量化服务令牌未配置；风险数据为 NA。')
        return results
    client = StockQuantRiskClient(settings.stockquant_service_url, settings.stockquant_service_token,
                                 timeout=settings.stockquant_timeout_seconds)
    try:
        for start in range(0, len(payloads), 50):
            batch = payloads[start:start + 50]
            try:
                call = client.risk_report(batch, as_of=as_of.isoformat())
                for report in call.data['products']:
                    report = dict(report)
                    report.update(status='ok', request_id=call.request_id, latency_ms=call.latency_ms,
                                  engine=call.provenance.get('engine'), source_verified=True)
                    results[report['product_id']] = report
                for failure in call.data['failures']:
                    pid = failure['product_id']
                    results[pid] = unavailable(pid, failure.get('code', 'QUANT_DATA_INVALID'),
                                             failure.get('safe_message', '净值不可计算'))
            except (QuantUnavailable, QuantContractError) as exc:
                for p in batch:
                    results[p['product_id']] = unavailable(p['product_id'], type(exc).__name__,
                        '量化服务或数据校验不可用；仅保留产品条款比较，风险数据为 NA。')
    finally:
        client.session.close()
    return results


def history_view(report: dict, navs) -> dict:
    metric = report.get('metrics', {}).get('cagr', {})
    value = metric.get('value')
    window = report.get('data_window')
    reason = report.get('message') or metric.get('na_reason') or '历史数据不足'
    return {"applicable": report.get('status') == 'ok', "nav_points": len(navs),
        "window": window, "as_of": window.get('end') if window else None,
        "history_metric": {"value": None if value is None else round(value * 10000, 4),
            "unit": "bps_annualized", "method": metric.get('method', 'unavailable'),
            "status": 'ok' if value is not None else 'not_applicable',
            "reason_code": None if value is not None else report.get('reason_code', metric.get('na_reason')),
            "reason": None if value is not None else reason, "sample_count": metric.get('sample_count', 0),
            "sample_unit": "daily_returns", "window": window},
        "note": "真实 HTTP 调用 StockQuant；历史统计不是预期收益；252 日年化假定日频。",
        "provenance": report.get('provenance', {}), "request_id": report.get('request_id')}
