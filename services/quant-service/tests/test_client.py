"""End-to-end: real HTTP server + FinPilot-side client verification."""
import threading

import pytest
from werkzeug.serving import make_server

from finpilot_adapter.risk_report import (
    QuantContractError,
    QuantUnavailable,
    StockQuantRiskClient,
    nav_source_hash,
)
from stockquant_bank.canonical import series_source_hash
from stockquant_bank.service import create_app


def sample_nav(count=300):
    return [
        {"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "value": 1.0 + i / 5000}
        for i in range(count)
    ]


@pytest.fixture()
def live_server(tmp_path):
    app = create_app(token="live-token", audit_path=str(tmp_path / "audit.jsonl"))
    server = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    thread.join(timeout=5)


def test_hash_definitions_match_across_the_boundary():
    nav = sample_nav(50)
    assert nav_source_hash(nav) == series_source_hash(nav)


def test_client_calls_service_and_verifies_source_hash(live_server):
    client = StockQuantRiskClient(live_server, "live-token")
    product = {
        "product_id": "P1",
        "product_name": "测试",
        "official_risk_level": "R2",
        "total_fee_bps": 10,
        "term_days": 0,
        "settlement_days": 1,
        "min_amount_minor": 100,
        "nav": sample_nav(),
    }
    call = client.risk_report([product], as_of="2026-10-23")
    assert call.data["products"][0]["metrics"]["annual_volatility"]["value"] is not None
    assert call.provenance["engine"]["commit"] in {"unknown", None} or isinstance(call.provenance["engine"]["commit"], str)


def test_wrong_token_is_a_dependency_failure(live_server):
    client = StockQuantRiskClient(live_server, "wrong")
    with pytest.raises(QuantUnavailable):
        client.risk_report([{"product_id": "P1", "nav": sample_nav()}], as_of="2026-10-23")


def test_method_version_pin_is_enforced(live_server):
    client = StockQuantRiskClient(live_server, "live-token", pinned_method_version="sq-wealth-risk/0.9.0")
    with pytest.raises(QuantContractError):
        client.risk_report([{"product_id": "P1", "nav": sample_nav()}], as_of="2026-10-23")


def test_rank_and_stress_round_trip(live_server):
    client = StockQuantRiskClient(live_server, "live-token")
    products = [{
        "product_id": "P1", "official_risk_level": "R2", "total_fee_bps": 10,
        "term_days": 0, "settlement_days": 1, "min_amount_minor": 100, "nav": sample_nav(),
    }]
    ranked = client.rank(products, user={"risk_level": "R3", "preference_mode": "balanced",
                                        "horizon_days": 365, "liquidity_need_days": 5,
                                        "budget_minor": 1_000_000}, as_of="2026-10-23")
    assert ranked.data["ranked"]
    stressed = client.stress(products, as_of="2026-10-23")
    assert stressed.data["products"][0]["scenarios"]


def test_unreachable_service_is_unavailable_not_zero():
    client = StockQuantRiskClient("http://127.0.0.1:1", "x", timeout=0.5)
    with pytest.raises(QuantUnavailable):
        client.risk_report([{"product_id": "P1", "nav": sample_nav()}], as_of="2026-10-23")