"""审计哈希链测试（V5 17.3、SEC-05、T36／T55）。

全部跑真实 PostgreSQL（``TEST_DATABASE_URL``）：篡改通过旁路 SQL 完成，
因此验证必须看到数据库当前内容而不是本会话缓存。
"""

from __future__ import annotations

import pytest
from sqlalchemy import text

from finpilot import ids
from finpilot.config import get_settings
from finpilot.errors import FinPilotError
from finpilot.security import audit
from finpilot.timeutil import format_iso_utc

pytestmark = pytest.mark.security


def _seed_chain(session, count: int = 3) -> list:
    events = []
    for index in range(count):
        events.append(
            audit.append_event(
                session,
                event_type=f"task.step_{index}",
                actor="user_demo_01",
                actor_role="customer",
                aggregate_id="task_demo",
                task_id="task_demo",
                policy_version="policy-2026.10",
                model_version="mock-v1",
                payload={"index": index, "amount_minor": 1000 * (index + 1)},
            )
        )
    session.commit()
    return events


def test_append_event_builds_hash_chain(session):
    events = _seed_chain(session, 3)
    key = get_settings().audit_signing_key

    assert [event.seq for event in events] == [1, 2, 3]
    assert events[0].previous_hash == audit.GENESIS_HASH
    for previous, current in zip(events, events[1:]):
        assert current.previous_hash == previous.event_hash
    for event in events:
        assert event.payload_hash == ids.canonical_hash(event.payload)
        assert event.event_hash == audit.compute_event_hash(
            previous_hash=event.previous_hash,
            payload_hash=event.payload_hash,
            event_type=event.event_type,
            timestamp_iso=format_iso_utc(event.timestamp),
        )
        assert event.signature == ids.hmac_hex(key, event.event_hash)

    report = audit.verify_chain(session)
    assert report["ok"] is True
    assert report["checked"] == 3
    assert report["first_bad_seq"] is None
    assert report["reason"] is None
    assert report["head_seq"] == 3
    assert report["head_hash"] == events[-1].event_hash


def test_verify_chain_empty_chain_is_ok(session):
    report = audit.verify_chain(session)
    assert report["ok"] is True
    assert report["checked"] == 0
    assert report["head_hash"] == audit.GENESIS_HASH


def test_verify_chain_detects_payload_tampering(session):
    _seed_chain(session, 3)
    session.execute(text("UPDATE audit_events SET payload = '{\"index\": 99}'::jsonb WHERE seq = 2"))

    report = audit.verify_chain(session)
    assert report["ok"] is False
    assert report["first_bad_seq"] == 2
    assert report["reason"] == "PAYLOAD_HASH_MISMATCH"
    assert "PAYLOAD_HASH_MISMATCH" in [item["reason"] for item in report["violations"]]


def test_verify_chain_detects_deleted_middle_event(session):
    _seed_chain(session, 4)
    session.execute(text("DELETE FROM audit_events WHERE seq = 2"))

    report = audit.verify_chain(session)
    assert report["ok"] is False
    assert report["first_bad_seq"] == 3
    reasons = [item["reason"] for item in report["violations"]]
    assert "SEQ_GAP" in reasons
    assert "PREVIOUS_HASH_MISMATCH" in reasons


def test_verify_chain_detects_event_reordering(session):
    _seed_chain(session, 3)
    session.execute(text("UPDATE audit_events SET seq = -1 WHERE seq = 1"))
    session.execute(text("UPDATE audit_events SET seq = 1 WHERE seq = 2"))
    session.execute(text("UPDATE audit_events SET seq = 2 WHERE seq = -1"))

    report = audit.verify_chain(session)
    assert report["ok"] is False
    assert report["first_bad_seq"] == 1
    assert report["reason"] == "PREVIOUS_HASH_MISMATCH"


def test_verify_chain_detects_swapped_payloads(session):
    _seed_chain(session, 3)
    session.execute(
        text(
            "UPDATE audit_events SET payload = (SELECT payload FROM audit_events WHERE seq = 1) WHERE seq = 3"
        )
    )
    report = audit.verify_chain(session)
    assert report["ok"] is False
    assert report["first_bad_seq"] == 3
    assert report["reason"] == "PAYLOAD_HASH_MISMATCH"


def test_verify_chain_detects_signature_tampering(session):
    _seed_chain(session, 2)
    session.execute(text("UPDATE audit_events SET signature = :sig WHERE seq = 1"), {"sig": "0" * 64})

    report = audit.verify_chain(session)
    assert report["ok"] is False
    assert report["first_bad_seq"] == 1
    assert report["reason"] == "SIGNATURE_MISMATCH"


def test_signed_digest_is_stable_and_tamper_sensitive(session):
    _seed_chain(session, 3)
    digest = audit.signed_digest(session)
    assert digest["event_count"] == 3
    assert digest["head_seq"] == 3
    assert digest["chain_ok"] is True
    assert len(digest["signature"]) == 64
    assert digest["alg"] == audit.DIGEST_ALG

    again = audit.signed_digest(session)
    assert again["signature"] == digest["signature"]
    assert again["content_hash"] == digest["content_hash"]

    verified = audit.verify_signed_digest(session, digest)
    assert verified["ok"] is True
    assert verified["signature_ok"] is True
    assert verified["chain_matches"] is True
    assert verified["reason_codes"] == []

    session.execute(text("UPDATE audit_events SET payload = '{\"index\": 42}'::jsonb WHERE seq = 1"))
    after = audit.signed_digest(session)
    assert after["signature"] != digest["signature"]
    assert after["content_hash"] != digest["content_hash"]
    assert after["chain_ok"] is False
    # head_hash（末尾事件哈希）不变，这正是需要 content_hash/chain_ok 一并签名的原因
    assert after["head_hash"] == digest["head_hash"]

    check = audit.verify_signed_digest(session, digest)
    assert check["ok"] is False
    assert "CURRENT_CHAIN_INVALID" in check["reason_codes"]


def test_signed_digest_detects_tail_deletion(session):
    _seed_chain(session, 4)
    digest = audit.signed_digest(session)
    session.execute(text("DELETE FROM audit_events WHERE seq = 4"))

    check = audit.verify_signed_digest(session, digest)
    assert check["ok"] is False
    assert "CHAIN_CHANGED_SINCE_DIGEST" in check["reason_codes"]
    assert check["current"]["head_seq"] == 3


def test_append_event_rejects_secrets_and_otp(session):
    with pytest.raises(FinPilotError) as excinfo:
        audit.append_event(session, event_type="auth.challenge", actor="user_demo_01", payload={"password": "p@ss"})
    assert excinfo.value.code == "PARAM_INVALID"
    assert any(reason.startswith("SENSITIVE_KEY:password") for reason in excinfo.value.details["reason_codes"])

    with pytest.raises(FinPilotError) as excinfo:
        audit.append_event(session, event_type="auth.challenge", actor="user_demo_01", payload={"factor_response": "123456"})
    reasons = excinfo.value.details["reason_codes"]
    assert any("factor_response" in reason for reason in reasons)

    with pytest.raises(FinPilotError) as excinfo:
        audit.append_event(session, event_type="auth.challenge", actor="user_demo_01", payload={"otp": "987654"})
    assert excinfo.value.code == "PARAM_INVALID"

    # 金额与计数不是验证码，不能被误判
    event = audit.append_event(
        session,
        event_type="operation.submitted",
        actor="user_demo_01",
        payload={"amount_minor": 900000, "seq_hint": "123456", "count": 654321},
    )
    assert event.payload_hash == ids.canonical_hash(event.payload)

    # 嵌套结构同样拦截
    with pytest.raises(FinPilotError):
        audit.append_event(
            session,
            event_type="operation.submitted",
            actor="user_demo_01",
            payload={"payee": {"card": {"cvv": "123"}}},
        )


def test_append_event_rejects_private_reasoning(session):
    with pytest.raises(FinPilotError) as excinfo:
        audit.append_event(
            session,
            event_type="model.decision",
            actor="model",
            payload={"chain_of_thought": "先查余额再转账"},
        )
    assert excinfo.value.code == "PARAM_INVALID"
    assert any("PRIVATE_REASONING_FORBIDDEN" in reason for reason in excinfo.value.details["reason_codes"])

    event = audit.append_event(
        session,
        event_type="model.decision",
        actor="model",
        payload={"structured_basis": ["snapshot.available_minor", "policy.grade"], "decision": "require_grant"},
    )
    assert event.event_type == "model.decision"


def test_require_audit_writable_ok_within_transaction(session):
    audit.require_audit_writable(session)
    report = audit.audit_health(session)
    assert report["writable"] is True
    assert report["chain"]["ok"] is True
    assert report["key_id"] == audit.signing_key_id()
    # 探针必须回滚，链上不能留下 seq = -1 的行
    assert session.execute(text("SELECT count(*) FROM audit_events WHERE seq < 0")).scalar_one() == 0


def test_require_audit_writable_fails_closed_on_read_only_transaction(session_factory):
    read_only = session_factory()
    try:
        read_only.execute(text("SET TRANSACTION READ ONLY"))
        assert read_only.execute(text("SHOW transaction_read_only")).scalar_one() == "on"

        with pytest.raises(FinPilotError) as excinfo:
            audit.require_audit_writable(read_only)
        assert excinfo.value.code == "AUDIT_UNAVAILABLE"
        assert excinfo.value.http_status == 503
        assert "AUDIT_WRITE_PROBE_FAILED" in excinfo.value.details["reason_codes"]

        health = audit.audit_health(read_only)
        assert health["writable"] is False
        assert health["write_error"] == "AUDIT_UNAVAILABLE"
    finally:
        read_only.rollback()
        read_only.close()


def test_record_and_replay_pending_receipts_is_idempotent(session):
    first = audit.record_pending_receipt(
        session,
        operation_id="op_pending_1",
        provider_id="bank_sandbox_v1",
        payload={"status": "succeeded", "provider_ref": "p_001"},
    )
    second = audit.record_pending_receipt(
        session,
        operation_id="op_pending_2",
        provider_id="merchant_sandbox_v1",
        payload={"status": "confirmed", "provider_ref": "m_002"},
    )
    session.commit()
    assert first and second

    pending = audit.pending_receipts(session)
    assert {item["operation_id"] for item in pending} == {"op_pending_1", "op_pending_2"}

    replayed = audit.replay_pending_receipts(session)
    session.commit()
    assert len(replayed) == 2
    assert {item["operation_id"] for item in replayed} == {"op_pending_1", "op_pending_2"}
    assert all(item["replayed_seq"] > item["pending_seq"] for item in replayed)

    assert audit.pending_receipts(session) == []
    assert audit.replay_pending_receipts(session) == []
    assert audit.verify_chain(session)["ok"] is True

    # 补齐事件本身也带上了可核对的 payload_hash
    replayed_hashes = {
        item["payload_hash"] for item in replayed
    }
    assert len(replayed_hashes) == 2


def test_record_pending_receipt_rejects_secrets(session):
    with pytest.raises(FinPilotError) as excinfo:
        audit.record_pending_receipt(
            session,
            operation_id="op_pending_3",
            provider_id="bank_sandbox_v1",
            payload={"pan": "4111111111111111"},
        )
    assert excinfo.value.code == "PARAM_INVALID"


def test_append_event_rejects_overlong_field(session):
    with pytest.raises(FinPilotError) as excinfo:
        audit.append_event(session, event_type="x" * 100, actor="user_demo_01")
    assert excinfo.value.code == "PARAM_INVALID"
    assert any(reason.startswith("FIELD_TOO_LONG") for reason in excinfo.value.details["reason_codes"])


def test_append_event_normalizes_non_json_values(session):
    """payload 里的 datetime／Decimal／tuple 必须能安全落库，且哈希可重现。"""
    from datetime import datetime, timezone
    from decimal import Decimal

    created_at = datetime(2026, 10, 22, 2, 0, 0, tzinfo=timezone.utc)
    event = audit.append_event(
        session,
        event_type="operation.settled",
        actor="system",
        payload={"created_at": created_at, "fee": Decimal("1.50"), "tags": ("a", "b")},
    )
    session.commit()
    assert event.payload["created_at"] == format_iso_utc(created_at)
    assert event.payload["fee"] == "1.5"
    assert event.payload["tags"] == ["a", "b"]
    assert event.payload_hash == ids.canonical_hash(event.payload)
    assert audit.verify_chain(session)["ok"] is True


@pytest.mark.concurrency
def test_concurrent_appends_keep_chain_linear(session_factory):
    """多连接并发追加：advisory lock 串行化，seq 连续且链不分叉。"""
    import threading

    errors: list[BaseException] = []
    threads_count = 5
    per_thread = 4

    def _worker(worker_index: int) -> None:
        worker_session = session_factory()
        try:
            for index in range(per_thread):
                audit.append_event(
                    worker_session,
                    event_type="operation.submitted",
                    actor=f"user_demo_{worker_index}",
                    operation_id=f"op_{worker_index}_{index}",
                    payload={"worker": worker_index, "index": index},
                )
                worker_session.commit()
        except BaseException as exc:  # pragma: no cover - 并发失败时才触发
            errors.append(exc)
            worker_session.rollback()
        finally:
            worker_session.close()

    threads = [threading.Thread(target=_worker, args=(index,)) for index in range(threads_count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors

    verify_session = session_factory()
    try:
        report = audit.verify_chain(verify_session)
        assert report["ok"] is True, report
        assert report["checked"] == threads_count * per_thread
        seqs = [
            int(value)
            for value in verify_session.execute(
                text("SELECT seq FROM audit_events ORDER BY seq ASC")
            ).scalars()
        ]
        assert seqs == list(range(1, threads_count * per_thread + 1))
    finally:
        verify_session.close()