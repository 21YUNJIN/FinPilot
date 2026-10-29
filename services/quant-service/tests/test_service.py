"""HTTP contract: auth, envelope, error codes, audit trail."""
import json

import pytest

from stockquant_bank.service import create_app


def payload():
    return {
        "as_of": "2026-10-23",
        "products": [
            {
                "product_id": "P1",
                "product_name": "测试",
                "official_risk_level": "R2",
                "total_fee_bps": 10,
                "term_days": 0,
                "settlement_days": 1,
                "min_amount_minor": 100,
                "nav": [
                    {"date": f"2025-{1 + i // 28:02d}-{1 + i % 28:02d}", "value": 1.0 + i / 5000}
                    for i in range(300)
                ],
            }
        ],
    }


@pytest.fixture()
def app(tmp_path):
    return create_app(token="secret-token", audit_path=str(tmp_path / "audit.jsonl"))


@pytest.fixture()
def client(app):
    return app.test_client()


def auth():
    return {"Authorization": "Bearer secret-token"}


def test_requires_token(app):
    with pytest.raises(RuntimeError):
        create_app(token="", dev_open=False, audit_path=None)


def test_missing_token_is_403(client):
    response = client.post("/v1/quant/risk-report", json=payload())
    assert response.status_code == 403
    body = response.get_json()
    assert body["status"] == "error"
    assert body["error"]["code"] == "UNAUTHORIZED"


def test_happy_path_envelope_and_provenance(client):
    response = client.post("/v1/quant/risk-report", json=payload(), headers=auth())
    assert response.status_code == 200
    body = response.get_json()
    assert body["status"] == "ok"
    assert body["schema_version"] == "1.0"
    assert body["request_id"].startswith("req_sq_")
    assert body["provenance"]["method_version"] == "sq-wealth-risk/1.0.0"
    assert body["provenance"]["engine"]["module"] == "stockquant.backtest.metrics"
    assert body["data"]["products"][0]["product_id"] == "P1"


def test_missing_as_of_is_400(client):
    bad = payload()
    bad.pop("as_of")
    response = client.post("/v1/quant/risk-report", json=bad, headers=auth())
    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "INVALID_REQUEST"


def test_bad_nav_is_isolated_as_failure(client):
    bad = payload()
    bad["products"].append({"product_id": "BAD", "nav": [{"date": "2026-10-01", "value": 0}]})
    response = client.post("/v1/quant/risk-report", json=bad, headers=auth())
    assert response.status_code == 200
    body = response.get_json()
    assert body["data"]["summary"]["failed"] == 1
    assert body["data"]["failures"][0]["code"] == "UNPROCESSABLE_DATA"


def test_rank_endpoint_returns_baseline_and_changes(client):
    body = payload()
    body["user"] = {"risk_level": "R3", "preference_mode": "stability",
                    "horizon_days": 365, "liquidity_need_days": 5, "budget_minor": 1_000_000}
    response = client.post("/v1/quant/rank", json=body, headers=auth())
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["ranked"] and data["baseline_ranked"]
    assert data["rank_changes"]


def test_stress_endpoint(client):
    response = client.post("/v1/quant/stress", json=payload(), headers=auth())
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["products"][0]["scenarios"][0]["not_a_loss_limit"]


def test_meta_exposes_guardrails(client):
    response = client.get("/v1/meta")
    data = response.get_json()["data"]
    assert data["guardrails"]["min_returns_for_volatility"] == 60
    assert data["guardrails"]["min_returns_for_cvar_95"] == 252
    assert "quant.risk_report" in data["capabilities"]


def test_audit_log_written(client, tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    client.post("/v1/quant/risk-report", json=payload(), headers=auth())
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    assert lines
    event = json.loads(lines[-1])
    assert event["endpoint"] == "/v1/quant/risk-report"
    assert event["status"] == "ok"
    assert event["response_hash"]
    assert "secret-token" not in json.dumps(event)


def test_healthz(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"