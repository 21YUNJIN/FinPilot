"""追加写审计链、独立签名摘要与待补回执队列（V5 17.3、SEC-05、T36／T55）。

设计口径
--------
1. **只追加**：``append_event`` 只插入新事件，从不修改或删除既有事件；``seq`` 在链内
   单调递增（同一事务内取 ``max(seq)+1``，并用事务级 advisory lock 串行化并发追加）。
2. **可检测删改**：每条事件保存 ``payload_hash``（``sha256(canonical_json(payload))``）、
   ``previous_hash``（上一条 ``event_hash``）与 ``event_hash
   = sha256(previous_hash + payload_hash + event_type + timestamp_iso)``。
   ``verify_chain`` 逐条重算并检测：payload 被改、中间事件被删、事件被重排／搬移、
   哈希被替换、签名被替换。
3. **独立签名摘要**：``signed_digest`` 在链头之外再签一份"整链内容摘要"
   （``content_hash`` 覆盖每条 ``(seq, payload_hash, event_hash)``），由
   ``HMAC-SHA256(settings.audit_signing_key, ...)`` 签名。把这份摘要周期性保存到
   审计库之外（对象存储／只写介质／外部对账系统），即可检测"整链重写"与"尾部删除"。
4. **不写入秘密**：``assert_no_secrets`` 拒绝 password／otp／totp／code／secret／pan／cvv／
   token 等敏感键与疑似 6 位验证码的值；``assert_no_private_reasoning`` 拒绝模型私有
   思维链字段。审计只保存结构化依据与可检查决定（工具、参数摘要、策略与模型版本、
   授权引用、验证结果、资金效果、回执与恢复决定）。
5. **失败关闭**：``require_audit_writable`` 在高风险提交前做真实的写探针（保存点内插入
   再回滚），审计不可写时抛 ``FinPilotError("AUDIT_UNAVAILABLE")``，调用方必须中止新提交；
   已在途的回执用 ``record_pending_receipt`` 落入可靠待补队列，恢复后由
   ``replay_pending_receipts`` 补齐。

边界（如实披露，不得声称超出实际能力）
--------------------------------------
* **单库哈希链无法抵抗同时掌握全部密钥与存储的管理员**：能改 payload、能重算
  ``payload_hash``／``event_hash``、并且持有 ``audit_signing_key`` 的人可以重写整条链
  并重新签名，使 ``verify_chain`` 与 ``signed_digest`` 都自洽。要收紧这个边界必须引入
  本模块之外的控制：审计库独立凭证与独立的仅追加权限、签名密钥放在审计进程不可读的
  KMS／HSM、把 ``signed_digest`` 输出定期写入外部只写介质（WORM）或第三方时间戳服务。
  本模块只提供"检测"能力与可外送的摘要，不宣称能阻止具备上述全部权限的攻击者。
* 尾部截断对链内自检不可见（链本身仍然自洽）：这正是需要独立保存 ``signed_digest``
  的原因；``verify_signed_digest`` 提供与已保存摘要的比对。
* 审计表与业务表同库同事务，因此"审计写成功但业务提交失败"不会发生；反过来，审计不可
  写时业务写入也会被阻断（fail-closed）。跨库异步审计会牺牲这一性质，本实现不做。
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .. import db as dbmod
from .. import ids
from ..config import get_settings
from ..errors import FinPilotError
from ..models import AuditEvent
from ..timeutil import format_iso_utc, utcnow

logger = logging.getLogger(__name__)

# --- 常量 -----------------------------------------------------------------

GENESIS_HASH = "0" * 64
"""链首事件的 ``previous_hash``（64 个 0），使第一条事件也有确定的"前驱"。"""

CHAIN_LOCK_KEY = "finpilot.audit.append"
"""事务级 advisory lock 键：串行化同一库上的审计追加，保证 seq 单调且不断链。"""

DIGEST_ALG = "HMAC-SHA256/audit-digest-v1"
DIGEST_VERSION = "audit-digest-v1"

EVENT_RECEIPT_PENDING = "receipt_pending"
"""在途回执落入待补队列的事件类型（审计中断时的可靠落点）。"""

EVENT_RECEIPT_REPLAYED = "receipt_replayed"
"""待补回执被补齐后追加的事件类型；同一 (operation_id, payload_hash) 只补一次。"""

EVENT_WRITE_PROBE = "audit.write_probe"
"""审计可写性探针事件类型；探针在保存点内插入并回滚，正常情况下不会留在链上。"""

# 敏感键：精确命中（避免 "company" 命中 "pan" 这类子串误伤）
SENSITIVE_KEY_EXACT = frozenset(
    {
        "password",
        "passwd",
        "pwd",
        "pass",
        "otp",
        "totp",
        "totpsecret",
        "otpsecret",
        "code",
        "secret",
        "clientsecret",
        "pan",
        "cardpan",
        "cvv",
        "cvv2",
        "cvc",
        "cvc2",
        "pin",
        "token",
        "accesstoken",
        "refreshtoken",
        "idtoken",
        "apikey",
        "privatekey",
        "signingkey",
        "encryptionkey",
        "cardnumber",
        "cardno",
        "creditcard",
        "debitcard",
        "idcard",
        "idnumber",
        "securitycode",
        "verificationcode",
        "authcode",
        "smscode",
        "factorresponse",
        "privatekeypem",
        # 中文键名
        "密码",
        "口令",
        "验证码",
        "动态码",
        "卡号",
        "完整卡号",
        "身份证",
        "身份证号",
        "密钥",
        "私钥",
    }
)

# 敏感键：子串命中（这些词根几乎不会出现在非敏感字段名里）
SENSITIVE_KEY_SUBSTRINGS = (
    "password",
    "passwd",
    "totp",
    "otpcode",
    "otpsecret",
    "cvv",
    "cvc",
    "secret",
    "apikey",
    "api_key",
    "privatekey",
    "private_key",
    "cardnumber",
    "card_number",
    "idcard",
    "id_card",
    "idnumber",
    "securitycode",
    "security_code",
    "verificationcode",
    "verification_code",
    "authcode",
    "auth_code",
    "smscode",
    "sms_code",
    "factorresponse",
    "factor_response",
    "access_token",
    "refresh_token",
    "id_token",
    "验证码",
    "动态口令",
    "身份证",
    "私钥",
    "密钥",
)

# 模型私有思维链：明确拒绝写入审计（SEC-05 要求只保存结构化依据）
PRIVATE_REASONING_KEYS = frozenset(
    {
        "chainofthought",
        "chain_of_thought",
        "cot",
        "reasoningtrace",
        "reasoning_trace",
        "reasoningcontent",
        "reasoning_content",
        "scratchpad",
        "internalmonologue",
        "internal_monologue",
        "privaterreasoning",
        "private_reasoning",
        "hiddenreasoning",
        "hidden_reasoning",
        "thoughtprocess",
        "thought_process",
    }
)

_SIX_DIGIT_RE = re.compile(r"^\d{6}$")
_KEY_SPLIT_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff]+")

# 6 位数字出现在这些语义的键上时按业务数值处理，不当作验证码
_NUMERIC_BUSINESS_HINTS = (
    "amount",
    "minor",
    "balance",
    "price",
    "fee",
    "total",
    "count",
    "seq",
    "limit",
    "shares",
    "qty",
    "quantity",
    "page",
    "size",
    "version",
    "revision",
    "seconds",
    "days",
    "bytes",
    "bps",
    "rate",
    "percent",
    "pct",
    "id",
)

# 审计表列长度（models.AuditEvent），超长直接拒绝而不是静默截断
_MAX_LEN = {
    "event_type": 64,
    "actor": 64,
    "actor_role": 32,
    "aggregate_id": 64,
    "task_id": 48,
    "operation_id": 64,
    "grant_id": 48,
    "policy_version": 32,
    "model_version": 64,
}

_MAX_VIOLATIONS = 20


# --- 工具 -----------------------------------------------------------------


def _normalized_key(key: Any) -> str:
    return _KEY_SPLIT_RE.sub("", str(key).lower())


def _is_sensitive_key(key: Any) -> bool:
    norm = _normalized_key(key)
    if not norm:
        return False
    if norm in SENSITIVE_KEY_EXACT:
        return True
    return any(token.replace("_", "") in norm for token in SENSITIVE_KEY_SUBSTRINGS)


def _is_private_reasoning_key(key: Any) -> bool:
    norm = _normalized_key(key)
    return bool(norm) and (norm in PRIVATE_REASONING_KEYS or norm in {"cot", "思维链", "私有思维链"})


def _looks_like_business_number(key: Any) -> bool:
    norm = _normalized_key(key)
    return any(hint in norm for hint in _NUMERIC_BUSINESS_HINTS)


def _iter_leaves(node: Any, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], Any]]:
    if isinstance(node, dict):
        for key, value in node.items():
            child = path + (str(key),)
            yield child, value
            yield from _iter_leaves(value, child)
    elif isinstance(node, (list, tuple)):
        for index, value in enumerate(node):
            child = path + (f"[{index}]",)
            yield from _iter_leaves(value, child)


def _path_text(path: tuple[str, ...]) -> str:
    out = ""
    for part in path:
        out += part if part.startswith("[") else ("." + part if out else part)
    return out


def assert_no_secrets(payload: Any) -> None:
    """拒绝把敏感键或疑似验证码写入审计（抛 ``PARAM_INVALID``）。

    规则：
    - 键名命中 :data:`SENSITIVE_KEY_EXACT` / :data:`SENSITIVE_KEY_SUBSTRINGS`
      （password、otp、totp、code、secret、pan、cvv、token、验证码、私钥等）；
    - 字符串值形如 6 位数字，且键名不是金额／计数类语义（``*_minor``、``amount``、
      ``count``、``seq``、``limit`` 等）——金额 900000 这类业务数值不会被误判。

    只回报键路径与原因码，不回显任何值。调用方不得据此认为 payload 已经"安全"，
    这只保证验证码与私钥不进入审计存储。

    注意：键名**恰好**是 ``code`` 也会被拒绝（V5 明确列出的敏感键）；请改用
    ``error_code``／``reason_codes``／``event_code`` 这类不会与验证码混淆的名字。
    回执类 payload 若本身含完整卡号或验证码，调用方必须先脱敏再写入，本函数不会
    "顺手"脱敏（失败关闭比静默降级更安全）。
    """
    reasons: list[str] = []
    for path, value in _iter_leaves(payload):
        if not path:
            continue
        leaf_key = path[-1]
        if leaf_key.startswith("["):
            continue
        if _is_sensitive_key(leaf_key):
            reasons.append(f"SENSITIVE_KEY:{_path_text(path)}")
            continue
        if isinstance(value, str) and _SIX_DIGIT_RE.match(value.strip()) and not _looks_like_business_number(leaf_key):
            reasons.append(f"OTP_LIKE_VALUE:{_path_text(path)}")
    if reasons:
        raise FinPilotError(
            "PARAM_INVALID",
            message="审计负载包含敏感字段，已拒绝写入。",
            details={"reason_codes": reasons[:_MAX_VIOLATIONS]},
        )


def assert_no_private_reasoning(payload: Any) -> None:
    """拒绝把模型私有思维链写入审计（抛 ``PARAM_INVALID``）。

    审计只保存结构化依据与可检查决定（工具、参数摘要、策略版本、授权引用、效果），
    不保存模型内部推理草稿。
    """
    reasons: list[str] = []
    for path, _value in _iter_leaves(payload):
        if path and not path[-1].startswith("[") and _is_private_reasoning_key(path[-1]):
            reasons.append(f"PRIVATE_REASONING_FORBIDDEN:{_path_text(path)}")
    if reasons:
        raise FinPilotError(
            "PARAM_INVALID",
            message="审计负载包含模型私有推理字段，已拒绝写入。",
            details={"reason_codes": reasons[:_MAX_VIOLATIONS]},
        )


def _fit(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    limit = _MAX_LEN[field]
    if len(value) > limit:
        raise FinPilotError(
            "PARAM_INVALID",
            message=f"审计字段 {field} 超长（>{limit}）。",
            details={"reason_codes": [f"FIELD_TOO_LONG:{field}"], "length": len(value)},
        )
    return value


def signing_key_id() -> str:
    """签名密钥标识（密钥哈希前缀，不含密钥本身），便于轮换与摘要核对。"""
    return hashlib.sha256(get_settings().audit_signing_key.encode("utf-8")).hexdigest()[:12]


def compute_event_hash(*, previous_hash: str, payload_hash: str, event_type: str, timestamp_iso: str) -> str:
    """``event_hash = sha256(previous_hash + payload_hash + event_type + timestamp_iso)``。"""
    return ids.sha256_hex(f"{previous_hash}{payload_hash}{event_type}{timestamp_iso}")


def _timestamp_iso(value: datetime) -> str:
    return format_iso_utc(value)


def _chain_lock(session: Session) -> None:
    """事务级 advisory lock；同一库上的并发追加串行化，避免 seq 冲突与分叉。"""
    dbmod.advisory_xact_lock(session, CHAIN_LOCK_KEY)


def _next_seq(session: Session) -> int:
    current = session.execute(select(func.coalesce(func.max(AuditEvent.seq), 0))).scalar_one()
    return int(current) + 1


def _previous_hash(session: Session) -> str:
    row = session.execute(
        select(AuditEvent.event_hash).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    return row or GENESIS_HASH


# --- 追加写 ---------------------------------------------------------------


def append_event(
    session: Session,
    *,
    event_type: str,
    actor: str,
    actor_role: str = "system",
    aggregate_id: str | None = None,
    task_id: str | None = None,
    operation_id: str | None = None,
    grant_id: str | None = None,
    policy_version: str = "",
    model_version: str = "",
    payload: dict[str, Any] | None = None,
) -> AuditEvent:
    """追加一条审计事件并返回它（调用方负责提交事务）。

    - ``seq`` 单调递增；``previous_hash`` 取上一条 ``event_hash``；
      ``payload_hash``／``event_hash``／``signature`` 见模块 docstring。
    - payload 若含敏感键、6 位验证码或模型私有思维链 → ``PARAM_INVALID``（不写入）。
    - 时间取 ``timeutil.utcnow()``（受可注入时钟控制），持久化为 UTC。
    """
    if not event_type or not isinstance(event_type, str):
        raise FinPilotError("PARAM_INVALID", message="event_type 不能为空。", details={"reason_codes": ["EVENT_TYPE_REQUIRED"]})
    body: dict[str, Any]
    if payload is None:
        body = {}
    elif isinstance(payload, dict):
        body = dict(payload)
    else:
        raise FinPilotError(
            "PARAM_INVALID",
            message="审计 payload 必须是 JSON 对象。",
            details={"reason_codes": ["PAYLOAD_NOT_OBJECT"]},
        )
    assert_no_secrets(body)
    assert_no_private_reasoning(body)
    # JSON 安全化：datetime → ISO、Decimal → 字符串、元组 → 列表、未知对象 → str。
    # 与 payload_hash 使用同一套 canonical_json 规范化，因此哈希不变且可重现；
    # 存储前规范化可避免 JSONB 序列化失败把审计写入变成 500。
    safe_body = json.loads(ids.canonical_json(body))

    event_type = _fit(event_type, "event_type") or ""
    actor = _fit(actor or "system", "actor") or "system"
    actor_role = _fit(actor_role or "system", "actor_role") or "system"
    aggregate_id = _fit(aggregate_id, "aggregate_id")
    task_id = _fit(task_id, "task_id")
    operation_id = _fit(operation_id, "operation_id")
    grant_id = _fit(grant_id, "grant_id")
    policy_version = _fit(policy_version or "", "policy_version") or ""
    model_version = _fit(model_version or "", "model_version") or ""

    timestamp = utcnow()
    payload_hash = ids.canonical_hash(body)
    _chain_lock(session)
    seq = _next_seq(session)
    previous_hash = _previous_hash(session)
    event_hash = compute_event_hash(
        previous_hash=previous_hash,
        payload_hash=payload_hash,
        event_type=event_type,
        timestamp_iso=_timestamp_iso(timestamp),
    )
    event = AuditEvent(
        event_id=f"aud_{uuid.uuid4().hex[:20]}",
        seq=seq,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor=actor,
        actor_role=actor_role,
        task_id=task_id,
        operation_id=operation_id,
        grant_id=grant_id,
        policy_version=policy_version,
        model_version=model_version,
        payload=safe_body,
        payload_hash=payload_hash,
        previous_hash=previous_hash,
        event_hash=event_hash,
        signature=ids.hmac_hex(get_settings().audit_signing_key, event_hash),
        timestamp=timestamp,
    )
    session.add(event)
    session.flush()
    return event


# --- 校验 -----------------------------------------------------------------

_AUDIT_COLUMNS = (
    AuditEvent.seq,
    AuditEvent.event_id,
    AuditEvent.event_type,
    AuditEvent.actor,
    AuditEvent.actor_role,
    AuditEvent.aggregate_id,
    AuditEvent.task_id,
    AuditEvent.operation_id,
    AuditEvent.grant_id,
    AuditEvent.policy_version,
    AuditEvent.model_version,
    AuditEvent.payload,
    AuditEvent.payload_hash,
    AuditEvent.previous_hash,
    AuditEvent.event_hash,
    AuditEvent.signature,
    AuditEvent.timestamp,
)


def _load_rows(session: Session, *, up_to_seq: int | None = None) -> list[Any]:
    """按 seq 升序读取审计列。

    只取列不取 ORM 实体：刻意绕过 identity map，使校验始终看到数据库当前内容，
    而不是本会话缓存的对象（篡改正是通过旁路 SQL 发生的）。
    """
    stmt = select(*_AUDIT_COLUMNS).order_by(AuditEvent.seq.asc())
    if up_to_seq is not None:
        stmt = stmt.where(AuditEvent.seq <= up_to_seq)
    return list(session.execute(stmt).all())


def _verify_rows(rows: list[Any]) -> dict[str, Any]:
    key = get_settings().audit_signing_key
    violations: list[dict[str, Any]] = []
    last_seq = 0
    expected_prev = GENESIS_HASH
    for row in rows:
        seq = int(row.seq)
        if seq <= last_seq:
            violations.append(
                {"seq": seq, "reason": "SEQ_NOT_MONOTONIC", "detail": f"seq {seq} 未大于前一条 {last_seq}"}
            )
        elif seq != last_seq + 1:
            violations.append({"seq": seq, "reason": "SEQ_GAP", "detail": f"seq 从 {last_seq} 跳到 {seq}"})
        if row.previous_hash != expected_prev:
            violations.append(
                {
                    "seq": seq,
                    "reason": "PREVIOUS_HASH_MISMATCH",
                    "detail": "previous_hash 与上一条 event_hash 不一致（存在删除或重排）",
                }
            )
        recomputed_payload = ids.canonical_hash(row.payload)
        if recomputed_payload != row.payload_hash:
            violations.append(
                {"seq": seq, "reason": "PAYLOAD_HASH_MISMATCH", "detail": "payload 内容与 payload_hash 不一致"}
            )
        recomputed_event = compute_event_hash(
            previous_hash=row.previous_hash,
            payload_hash=row.payload_hash,
            event_type=row.event_type,
            timestamp_iso=_timestamp_iso(row.timestamp),
        )
        if recomputed_event != row.event_hash:
            violations.append(
                {"seq": seq, "reason": "EVENT_HASH_MISMATCH", "detail": "event_hash 与重算结果不一致"}
            )
        if not hmac.compare_digest(ids.hmac_hex(key, row.event_hash), row.signature or ""):
            violations.append({"seq": seq, "reason": "SIGNATURE_MISMATCH", "detail": "签名与审计签名密钥不匹配"})
        last_seq = seq
        expected_prev = row.event_hash

    head = rows[-1] if rows else None
    return {
        "ok": not violations,
        "checked": len(rows),
        "first_bad_seq": violations[0]["seq"] if violations else None,
        "reason": violations[0]["reason"] if violations else None,
        "detail": violations[0]["detail"] if violations else None,
        "violations": violations[:_MAX_VIOLATIONS],
        "head_seq": int(head.seq) if head is not None else 0,
        "head_hash": head.event_hash if head is not None else GENESIS_HASH,
    }


def verify_chain(session: Session) -> dict[str, Any]:
    """校验整条审计链，返回 ``{ok, checked, first_bad_seq, reason, ...}``。

    检测能力：
    - payload 被改 → ``PAYLOAD_HASH_MISMATCH``；
    - 中间事件被删 → ``SEQ_GAP`` 与 ``PREVIOUS_HASH_MISMATCH``；
    - 事件被重排／搬移 → ``SEQ_NOT_MONOTONIC`` 或 ``PREVIOUS_HASH_MISMATCH``；
    - 哈希或签名被替换 → ``EVENT_HASH_MISMATCH``／``SIGNATURE_MISMATCH``。

    不检测：尾部整体删除，以及"掌握签名密钥的整链重写"（见模块 docstring 的边界说明）；
    这两项要靠独立保存的 :func:`signed_digest` 对比（:func:`verify_signed_digest`）。
    """
    rows = _load_rows(session)
    return _verify_rows(rows)


def _content_hash(rows: list[Any]) -> str:
    """整链内容摘要：覆盖每条事件的实际 payload、payload_hash 与 event_hash。

    用"按实际 payload 重算的哈希"而不是库里的 ``payload_hash``，这样"只改 payload、
    不改 payload_hash"的篡改也会改变内容摘要（否则内容摘要会被这种改法绕过）。
    """
    return ids.sha256_hex(
        ids.canonical_json([[int(r.seq), ids.canonical_hash(r.payload), r.event_hash] for r in rows])
    )


def _digest_content(*, head_seq: int, head_hash: str, event_count: int, chain_ok: bool, content_hash: str) -> dict[str, Any]:
    return {
        "v": DIGEST_VERSION,
        "head_seq": head_seq,
        "head_hash": head_hash,
        "event_count": event_count,
        "chain_ok": chain_ok,
        "content_hash": content_hash,
    }


def signed_digest(session: Session) -> dict[str, Any]:
    """生成可独立保存的签名摘要。

    返回 ``{head_seq, head_hash, signature, signed_at, event_count, chain_ok,
    content_hash, alg, key_id}``。``signature`` 覆盖 ``head_seq``／``head_hash``／
    ``event_count``／``chain_ok``／``content_hash``，因此链上任何一条事件的 payload、
    payload_hash 或 event_hash 被改动（以及尾部被删）都会改变摘要。

    使用方式：把返回的 dict 原样保存到审计库之外（只写介质、外部对象存储、对账系统），
    之后用 :func:`verify_signed_digest` 比对即可发现"整链重写／尾部删除"。
    """
    rows = _load_rows(session)
    verification = _verify_rows(rows)
    head = rows[-1] if rows else None
    content = _digest_content(
        head_seq=int(head.seq) if head is not None else 0,
        head_hash=head.event_hash if head is not None else GENESIS_HASH,
        event_count=len(rows),
        chain_ok=bool(verification["ok"]),
        content_hash=_content_hash(rows),
    )
    signature = ids.hmac_hex(get_settings().audit_signing_key, ids.canonical_json(content))
    return {
        "head_seq": content["head_seq"],
        "head_hash": content["head_hash"],
        "signature": signature,
        "signed_at": format_iso_utc(utcnow()),
        "event_count": content["event_count"],
        "chain_ok": content["chain_ok"],
        "content_hash": content["content_hash"],
        "alg": DIGEST_ALG,
        "key_id": signing_key_id(),
    }


def verify_signed_digest(session: Session, digest: dict[str, Any]) -> dict[str, Any]:
    """把"外部保存的签名摘要"与当前链比对。

    返回 ``{ok, signature_ok, chain_matches, current_chain_ok, reason_codes}``：
    - ``signature_ok``：摘要自身签名是否由当前 ``audit_signing_key`` 正确签发；
    - ``chain_matches``：当前链的 head／事件数／内容摘要是否与保存的摘要一致
      （尾部删除或整链重写会在这里暴露）；
    - ``current_chain_ok``：当前链逐条校验是否通过；任一为假都会 ``ok=False``
      （``CHAIN_CHANGED_SINCE_DIGEST``／``CURRENT_CHAIN_INVALID``）。
    """
    reasons: list[str] = []
    if not isinstance(digest, dict):
        raise FinPilotError("PARAM_INVALID", message="digest 必须是字典。", details={"reason_codes": ["DIGEST_NOT_OBJECT"]})
    content = _digest_content(
        head_seq=int(digest.get("head_seq") or 0),
        head_hash=str(digest.get("head_hash") or ""),
        event_count=int(digest.get("event_count") or 0),
        chain_ok=bool(digest.get("chain_ok")),
        content_hash=str(digest.get("content_hash") or ""),
    )
    expected = ids.hmac_hex(get_settings().audit_signing_key, ids.canonical_json(content))
    signature_ok = hmac.compare_digest(expected, str(digest.get("signature") or ""))
    if not signature_ok:
        reasons.append("DIGEST_SIGNATURE_MISMATCH")
    if digest.get("key_id") and digest.get("key_id") != signing_key_id():
        reasons.append("DIGEST_KEY_ID_MISMATCH")
    if not digest.get("chain_ok"):
        reasons.append("DIGEST_CHAIN_NOT_OK")

    current = signed_digest(session)
    chain_matches = (
        current["head_seq"] == content["head_seq"]
        and current["head_hash"] == content["head_hash"]
        and current["event_count"] == content["event_count"]
        and current["content_hash"] == content["content_hash"]
    )
    if not chain_matches:
        reasons.append("CHAIN_CHANGED_SINCE_DIGEST")
    if not current["chain_ok"]:
        reasons.append("CURRENT_CHAIN_INVALID")
    return {
        "ok": signature_ok and chain_matches and bool(digest.get("chain_ok")) and bool(current["chain_ok"]),
        "signature_ok": signature_ok,
        "chain_matches": chain_matches,
        "current_chain_ok": bool(current["chain_ok"]),
        "reason_codes": reasons,
        "current": current,
    }


# --- 可写性与失败关闭 ------------------------------------------------------


def require_audit_writable(session: Session) -> None:
    """审计可写性检查；不可写时抛 ``FinPilotError("AUDIT_UNAVAILABLE")``。

    做法：在保存点内真实插入一条 ``audit.write_probe`` 行再回滚——因此能同时发现
    "表不存在／权限被回收／只读事务或只读副本／连接已失效"，而不仅是"表能读"。
    探针使用 ``seq = -1`` 且立即回滚，不会进入哈希链。

    高风险新提交必须先调用本函数（fail-closed）：审计不可写时不得提交资金或授权变更。
    """
    probe_event_id = f"aud_probe_{uuid.uuid4().hex[:16]}"
    probe = {
        "event_id": probe_event_id,
        "seq": -1,
        "aggregate_id": None,
        "event_type": EVENT_WRITE_PROBE,
        "actor": "system",
        "actor_role": "system",
        "task_id": None,
        "operation_id": None,
        "grant_id": None,
        "policy_version": "",
        "model_version": "",
        "payload": {"probe": "write"},
        "payload_hash": ids.canonical_hash({"probe": "write"}),
        "previous_hash": GENESIS_HASH,
        "event_hash": hashlib.sha256(probe_event_id.encode("utf-8")).hexdigest(),
        "signature": "",
        "timestamp": utcnow(),
    }
    try:
        nested = session.begin_nested()
    except Exception as exc:  # pragma: no cover - 会话不可用时才会走到
        raise FinPilotError(
            "AUDIT_UNAVAILABLE",
            message="审计会话不可用，高风险提交已暂停。",
            details={"reason_codes": ["AUDIT_SESSION_UNAVAILABLE"], "probe_error": type(exc).__name__},
        ) from exc
    try:
        session.execute(AuditEvent.__table__.insert().values(**probe))
    except Exception as exc:
        with contextlib.suppress(Exception):
            nested.rollback()
        logger.error("审计写探针失败，进入失败关闭：%s", type(exc).__name__)
        raise FinPilotError(
            "AUDIT_UNAVAILABLE",
            message="审计记录不可写，高风险新提交已暂停。",
            details={"reason_codes": ["AUDIT_WRITE_PROBE_FAILED"], "probe_error": type(exc).__name__},
        ) from exc
    else:
        nested.rollback()


def audit_health(session: Session) -> dict[str, Any]:
    """运维视图：审计是否可写＋链校验结果（不抛异常，供健康检查与运营看板）。"""
    writable = True
    write_error = None
    try:
        require_audit_writable(session)
    except FinPilotError as exc:
        writable = False
        write_error = exc.code
    except SQLAlchemyError as exc:  # pragma: no cover - 连接级故障
        writable = False
        write_error = type(exc).__name__
    try:
        chain = verify_chain(session)
    except SQLAlchemyError as exc:  # pragma: no cover - 连接级故障
        chain = {"ok": False, "checked": 0, "first_bad_seq": None, "reason": "AUDIT_UNREADABLE", "detail": type(exc).__name__}
    return {"writable": writable, "write_error": write_error, "chain": chain, "key_id": signing_key_id()}


# --- 待补回执队列 ----------------------------------------------------------


def record_pending_receipt(
    session: Session,
    *,
    operation_id: str,
    provider_id: str,
    payload: dict[str, Any],
) -> str:
    """把在途回执写入可靠待补队列（审计中断／回调处理中断时不丢在途结果）。

    实现：追加一条 ``event_type="receipt_pending"`` 的审计事件，payload 内含
    ``operation_id``／``provider_id``／``payload``／``payload_hash``；返回该事件的
    ``event_id`` 作为待补引用。同一 ``(operation_id, payload_hash)`` 重复登记是幂等的
    （``replay_pending_receipts`` 只补一次）。

    ``payload`` 同样经过 :func:`assert_no_secrets`：回执里若含完整卡号、验证码等，
    必须先脱敏再登记（审计不保存完整 PAN 与验证码）。调用方必须先确认审计可写
    （:func:`require_audit_writable`）；若连待补队列也写不进去，只能落到本模块之外的
    持久队列（如 ``outbox``／``provider_events``），这一情形由调用方负责上报，
    不能静默丢弃。
    """
    if not operation_id or not provider_id:
        raise FinPilotError(
            "PARAM_INVALID",
            message="待补回执必须带 operation_id 与 provider_id。",
            details={"reason_codes": ["RECEIPT_REF_REQUIRED"]},
        )
    if not isinstance(payload, dict):
        raise FinPilotError("PARAM_INVALID", message="回执 payload 必须是对象。", details={"reason_codes": ["RECEIPT_NOT_OBJECT"]})
    body = {
        "operation_id": operation_id,
        "provider_id": provider_id,
        "payload": payload,
        "payload_hash": ids.canonical_hash(payload),
        "state": "pending",
    }
    event = append_event(
        session,
        event_type=EVENT_RECEIPT_PENDING,
        actor="system",
        actor_role="system",
        aggregate_id=operation_id,
        operation_id=operation_id,
        payload=body,
    )
    return event.event_id


def _replayed_keys(session: Session) -> set[tuple[str, str]]:
    rows = session.execute(
        select(AuditEvent.payload).where(AuditEvent.event_type == EVENT_RECEIPT_REPLAYED)
    ).scalars()
    keys: set[tuple[str, str]] = set()
    for payload in rows:
        if isinstance(payload, dict):
            keys.add((str(payload.get("operation_id") or ""), str(payload.get("payload_hash") or "")))
    return keys


def replay_pending_receipts(session: Session) -> list[dict[str, Any]]:
    """补齐待补队列：为尚未补齐的 ``receipt_pending`` 追加 ``receipt_replayed`` 事件。

    返回已补齐的回执列表（``{pending_event_id, pending_seq, operation_id, provider_id,
    payload, payload_hash, replayed_event_id, replayed_seq}``）。重复调用是幂等的：
    第二次返回空列表。调用方拿到返回列表后再把回执落到业务表（``receipts`` / Outbox），
    本模块只负责"在途结果不丢"的可靠落点，不越权写业务状态。
    """
    pending_rows = session.execute(
        select(AuditEvent.seq, AuditEvent.event_id, AuditEvent.payload)
        .where(AuditEvent.event_type == EVENT_RECEIPT_PENDING)
        .order_by(AuditEvent.seq.asc())
    ).all()
    if not pending_rows:
        return []
    already = _replayed_keys(session)
    replayed: list[dict[str, Any]] = []
    for seq, event_id, payload in pending_rows:
        body = payload if isinstance(payload, dict) else {}
        operation_id = str(body.get("operation_id") or "")
        provider_id = str(body.get("provider_id") or "")
        payload_hash = str(body.get("payload_hash") or "")
        if not operation_id or not payload_hash:
            logger.warning("跳过结构不完整的待补回执：seq=%s event_id=%s", seq, event_id)
            continue
        if (operation_id, payload_hash) in already:
            continue
        receipt_payload = body.get("payload") if isinstance(body.get("payload"), dict) else {}
        replayed_event = append_event(
            session,
            event_type=EVENT_RECEIPT_REPLAYED,
            actor="system",
            actor_role="system",
            aggregate_id=operation_id,
            operation_id=operation_id,
            payload={
                "operation_id": operation_id,
                "provider_id": provider_id,
                "payload": receipt_payload,
                "payload_hash": payload_hash,
                "state": "replayed",
                "replayed_from_event_id": str(event_id),
                "replayed_from_seq": int(seq),
            },
        )
        already.add((operation_id, payload_hash))
        replayed.append(
            {
                "pending_event_id": str(event_id),
                "pending_seq": int(seq),
                "operation_id": operation_id,
                "provider_id": provider_id,
                "payload": receipt_payload,
                "payload_hash": payload_hash,
                "replayed_event_id": replayed_event.event_id,
                "replayed_seq": int(replayed_event.seq),
            }
        )
    return replayed


def pending_receipts(session: Session) -> list[dict[str, Any]]:
    """列出仍未补齐的在途回执（运营与恢复入口只读视图）。"""
    already = _replayed_keys(session)
    rows = session.execute(
        select(AuditEvent.seq, AuditEvent.event_id, AuditEvent.payload, AuditEvent.timestamp)
        .where(AuditEvent.event_type == EVENT_RECEIPT_PENDING)
        .order_by(AuditEvent.seq.asc())
    ).all()
    out: list[dict[str, Any]] = []
    for seq, event_id, payload, timestamp in rows:
        body = payload if isinstance(payload, dict) else {}
        key = (str(body.get("operation_id") or ""), str(body.get("payload_hash") or ""))
        if key in already:
            continue
        out.append(
            {
                "pending_event_id": str(event_id),
                "pending_seq": int(seq),
                "operation_id": key[0],
                "provider_id": str(body.get("provider_id") or ""),
                "payload": body.get("payload") if isinstance(body.get("payload"), dict) else {},
                "payload_hash": key[1],
                "recorded_at": format_iso_utc(timestamp),
            }
        )
    return out


__all__ = [
    "CHAIN_LOCK_KEY",
    "DIGEST_ALG",
    "EVENT_RECEIPT_PENDING",
    "EVENT_RECEIPT_REPLAYED",
    "EVENT_WRITE_PROBE",
    "GENESIS_HASH",
    "append_event",
    "assert_no_private_reasoning",
    "assert_no_secrets",
    "audit_health",
    "compute_event_hash",
    "pending_receipts",
    "record_pending_receipt",
    "replay_pending_receipts",
    "require_audit_writable",
    "signed_digest",
    "signing_key_id",
    "verify_chain",
    "verify_signed_digest",
]