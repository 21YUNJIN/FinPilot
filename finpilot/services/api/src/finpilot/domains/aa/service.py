"""AA 分摊领域（V5 第 9 节 S1-04；验收族 T04/T27）。

口径：

1. **按已确认实际支出分摊**：``final_cost_minor`` 必须是已确认的实际支出，不接受预算或估算；
   分摊用 ``money.largest_remainder_split`` / ``largest_remainder_split_weights``（最大余数法），
   ``Σ share_minor == final_cost_minor`` 严格成立（100 元三人 → 33.34 / 33.33 / 33.33）。
2. **发起人自付份额单列**：``initiator_included`` 为真时，发起人份额单独一行并标记
   ``is_initiator=True``（已由发起人自付，状态 ``paid``；``self_funded_minor`` 单独汇总），
   不进入应收。
3. **只发收款请求、不代扣**：创建与到账登记都不调用转账、不写账本、不改任何参与人余额。
   每份份额的 ``auto_debit_allowed`` 恒为 ``False``；未付款份额只是应收（``status='pending'``），
   在快照的 ``pending_receivables_minor`` 中单列，绝不计入 ``available_minor``。
4. **退款调整可追溯**：原消费退款生成 ``adjustments`` 明细（同一 ``refund_id`` 关联各份额）
   并累加 ``AAShare.adjusted_minor``；已收款份额的退款条目标记为
   ``pending_authorization``（需另行授权），函数只记录、不直接退钱。
5. **幂等**：同一 ``operation_id`` 重复执行返回同一 ``aa_id``，不产生第二条业务记录。

返回结构：处理器返回 DomainResult ``{"type","data","source","version","warnings"}``；
``data`` 是 ``contracts/schemas/aa_request.json`` 的合同对象（``additionalProperties: false``），
界面所需聚合（已收／未收／自付等）放在 DomainResult 顶层与 ``aa_view`` 的只读视图里。
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...errors import FinPilotError
from ...ids import new_id
from ...kernel.identity import AuthContext
from ...kernel.submission import transition
from ...models import AARequest, AAShare, Operation, User
from ...money import (
    CURRENCY,
    format_minor,
    largest_remainder_split,
    largest_remainder_split_weights,
    parse_amount_minor,
)
from ...timeutil import format_iso, utcnow

SOURCE = "aa"
DOMAIN = "aa"
DOMAIN_VERSION = "2026.10"
TERMS_VERSION = "2026.10"

COLLECT_ONLY_NOTICE = "仅创建收款请求，不代扣；未到账份额只计应收，不计入可支配资金。"

# 份额状态（contracts/schemas/aa_share.json 的枚举子集：本版本只使用 pending / paid）
SHARE_PENDING = "pending"
SHARE_PAID = "paid"

# AA 状态（contracts/schemas/aa_request.json 的枚举；collecting / cancelled 保留给发送与撤销流程）
AA_OPEN = "open"
AA_COLLECTING = "collecting"
AA_PARTIALLY_SETTLED = "partially_settled"
AA_SETTLED = "settled"
AA_CANCELLED = "cancelled"

# 合同对象的字段集合（严格按 aa_request.json，additionalProperties=false）
CONTRACT_KEYS = (
    "aa_id",
    "owner_user_id",
    "task_id",
    "title",
    "final_cost_minor",
    "status",
    "participant_shares",
    "linked_orders",
    "adjustments",
    "received_minor",
    "received_minor_is_spendable",
    "reconciled",
    "currency",
    "created_at",
    "updated_at",
)

BUDGET_FIELDS = ("budget_minor", "estimated_cost_minor", "planned_cost_minor", "budget", "estimated_cost")


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _get_aa(session: Session, aa_id: str | None) -> AARequest:
    aa = session.get(AARequest, aa_id) if aa_id else None
    if aa is None:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该 AA 请求。")
    return aa


def _ordered_shares(session: Session, aa_id: str) -> list[AAShare]:
    """稳定顺序：发起人优先，其余按创建顺序（创建时按参与人顺序微秒递增）。"""
    shares = list(
        session.execute(
            select(AAShare).where(AAShare.aa_id == aa_id).order_by(AAShare.created_at, AAShare.share_id)
        ).scalars()
    )
    initiator = [s for s in shares if s.is_initiator]
    others = [s for s in shares if not s.is_initiator]
    return initiator + others


def _int_or_none(value: object) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise FinPilotError("PARAM_INVALID", "权重与金额必须是整数。")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise FinPilotError("PARAM_INVALID", "权重与金额必须是整数。") from exc


def _parse_final_cost(args: dict) -> int:
    """已确认实际支出：不接受预算／估算口径。"""
    raw = args.get("final_cost_minor")
    if raw is None and args.get("final_cost") is not None:
        raw = parse_amount_minor(args["final_cost"])
    if raw is None:
        present = [f for f in BUDGET_FIELDS if args.get(f) is not None]
        if present:
            raise FinPilotError(
                "PARAM_INVALID",
                "AA 必须按已确认的实际支出分摊，不能使用预算或估算金额。",
                details={"reason_codes": ["final_cost_required"], "budget_fields_present": present},
            )
        raise FinPilotError(
            "PARAM_INVALID",
            "缺少已确认实际支出 final_cost_minor。",
            details={"reason_codes": ["final_cost_required"]},
        )
    total = _int_or_none(raw)
    if total is None or total <= 0:
        raise FinPilotError(
            "PARAM_INVALID",
            "已确认实际支出必须大于 0 分。",
            details={"reason_codes": ["invalid_final_cost"]},
        )
    return total


def _normalize_participants(raw: object) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise FinPilotError(
            "PARAM_INVALID",
            "participants 必须是非空列表。",
            details={"reason_codes": ["participants_required"]},
        )
    participants: list[dict] = []
    seen_refs: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise FinPilotError(
                "PARAM_INVALID",
                f"participants[{index}] 必须是对象。",
                details={"reason_codes": ["invalid_participant"], "index": index},
            )
        display_name = str(item.get("display_name") or "").strip()
        participant_ref = str(item.get("participant_ref") or "").strip()
        if not display_name and not participant_ref:
            raise FinPilotError(
                "PARAM_INVALID",
                f"participants[{index}] 缺少 display_name 与 participant_ref。",
                details={"reason_codes": ["participant_identity_required"], "index": index},
            )
        ref = participant_ref or f"name:{display_name}"
        if ref in seen_refs:
            raise FinPilotError(
                "PARAM_INVALID",
                f"参与人标识重复：{ref}",
                details={"reason_codes": ["duplicate_participant"], "participant_ref": ref},
            )
        seen_refs.add(ref)
        weight = _int_or_none(item.get("weight"))
        if weight is not None and weight < 0:
            raise FinPilotError(
                "PARAM_INVALID",
                "分摊权重不能为负。",
                details={"reason_codes": ["negative_weight"], "index": index},
            )
        participants.append(
            {
                "display_name": display_name or ref,
                "participant_ref": ref,
                "participant_user_id": (str(item["participant_user_id"]) if item.get("participant_user_id") else None),
                "weight": weight,
            }
        )
    return participants


def _normalize_linked_orders(args: dict) -> list[dict]:
    entries: list[dict] = []
    raw = args.get("linked_orders")
    if raw is not None:
        if not isinstance(raw, list):
            raise FinPilotError(
                "PARAM_INVALID",
                "linked_orders 必须是列表。",
                details={"reason_codes": ["invalid_linked_orders"]},
            )
        for index, item in enumerate(raw):
            if isinstance(item, str):
                entries.append({"type": "expense", "operation_id": item, "amount_minor": None, "label": ""})
                continue
            if not isinstance(item, dict) or not item.get("operation_id"):
                raise FinPilotError(
                    "PARAM_INVALID",
                    f"linked_orders[{index}] 必须包含 operation_id。",
                    details={"reason_codes": ["invalid_linked_order"], "index": index},
                )
            entries.append(
                {
                    "type": str(item.get("type") or "expense"),
                    "operation_id": str(item["operation_id"]),
                    "amount_minor": _int_or_none(item.get("amount_minor")),
                    "label": str(item.get("label") or item.get("merchant_name") or ""),
                }
            )
    if not entries and args.get("order_operation_id"):
        entries.append(
            {
                "type": "expense",
                "operation_id": str(args["order_operation_id"]),
                "amount_minor": _int_or_none(args.get("order_amount_minor")),
                "label": str(args.get("order_label") or ""),
            }
        )
    return entries


def _refunded_total(aa: AARequest, original_operation_id: str) -> int:
    """同一原订单的累计已登记退款：按 refund_id 去重（每条份额明细共享同一退款编号）。"""
    by_refund: dict[str, int] = {}
    for entry in aa.adjustments or []:
        if entry.get("original_operation_id") != original_operation_id:
            continue
        key = str(entry.get("refund_id") or entry.get("recorded_at") or "")
        by_refund.setdefault(key, int(entry.get("refund_amount_minor") or 0))
    return sum(by_refund.values())


def _split_shares(total_minor: int, parties: list[dict]) -> list[int]:
    """最大余数法；合计严格等于 total_minor（不变量，违反即 SYSTEM_ERROR）。"""
    weights = [p["weight"] for p in parties]
    use_weights = any(w is not None for w in weights)
    if use_weights:
        resolved = [int(w) if w is not None else 1 for w in weights]
        if sum(resolved) <= 0:
            raise FinPilotError(
                "PARAM_INVALID",
                "分摊权重之和必须大于 0。",
                details={"reason_codes": ["invalid_weights"]},
            )
        shares = largest_remainder_split_weights(total_minor, resolved)
    else:
        shares = largest_remainder_split(total_minor, len(parties))
    if sum(shares) != total_minor:
        raise FinPilotError(
            "SYSTEM_ERROR",
            "分摊合计不等于已确认实际支出。",
            details={"reason_codes": ["split_not_conserved"], "expected_minor": total_minor, "actual_minor": sum(shares)},
        )
    return shares


def _cached_result(session: Session, operation: Operation) -> dict | None:
    if operation.domain_result:
        return dict(operation.domain_result)
    row = session.get(Operation, operation.operation_id)
    if row is not None and row.domain_result:
        return dict(row.domain_result)
    return None


def _existing_aa_for_operation(session: Session, operation_id: str) -> AARequest | None:
    share = session.execute(
        select(AAShare).where(AAShare.operation_id == operation_id).order_by(AAShare.created_at)
    ).scalars().first()
    if share is None:
        return None
    return session.get(AARequest, share.aa_id)


def _result(operation: Operation | None, data: dict, warnings: list[str] | None = None, **extra) -> dict:
    result = {
        "type": "aa_request",
        "data": data,
        "source": SOURCE,
        "version": DOMAIN_VERSION,
        "warnings": list(warnings or []),
        # 与 contracts/schemas/domain_result.json 对齐的别名
        "domain": DOMAIN,
        "result_type": "aa_request",
        "terms_version": TERMS_VERSION,
        "observed_at": format_iso(utcnow()),
        "operation_id": operation.operation_id if operation is not None else None,
        "task_id": operation.task_id if operation is not None else None,
    }
    result.update(extra)
    return result


def _remember(session: Session, operation: Operation, result: dict) -> dict:
    operation.domain_result = result
    operation.updated_at = utcnow()
    if operation.status in ("dispatching", "pending"):
        transition(
            session,
            operation,
            "succeeded",
            reason="domain_effect_confirmed",
            detail={"source": SOURCE, "type": result["type"]},
        )
    session.flush()
    return result


# ---------------------------------------------------------------------------
# 视图
# ---------------------------------------------------------------------------


def share_view(share: AAShare) -> dict:
    """AAShare 合同对象（contracts/schemas/aa_share.json）。"""
    return {
        "share_id": share.share_id,
        "aa_id": share.aa_id,
        "participant_ref": share.participant_ref,
        "participant_user_id": share.participant_user_id,
        "display_name": share.display_name,
        "share_minor": share.share_minor,
        "adjusted_minor": share.adjusted_minor,
        "status": share.status,
        "is_initiator": bool(share.is_initiator),
        "auto_debit_allowed": False,
        "operation_id": share.operation_id,
        "paid_at": format_iso(share.paid_at) if share.paid_at else None,
        "created_at": format_iso(share.created_at),
    }


def _collection_events(aa: AARequest) -> list[dict]:
    return [entry for entry in (aa.linked_orders or []) if entry.get("type") == "collection"]


def _expense_links(aa: AARequest) -> list[dict]:
    return [entry for entry in (aa.linked_orders or []) if entry.get("type") != "collection"]


def aa_state(session: Session, aa: AARequest) -> dict:
    """AA 只读视图：合同字段 + 界面聚合（已收／未收／自付／调整待授权）。"""
    shares = _ordered_shares(session, aa.aa_id)
    collected = sum(s.share_minor for s in shares if s.status == SHARE_PAID and not s.is_initiator)
    self_funded = sum(s.share_minor for s in shares if s.is_initiator)
    outstanding = sum(max(0, s.share_minor - s.adjusted_minor) for s in shares if s.status == SHARE_PENDING)
    adjusted_total = sum(s.adjusted_minor for s in shares)
    refund_pending = sum(
        int(a.get("delta_minor") or 0)
        for a in (aa.adjustments or [])
        if a.get("status") == "pending_authorization"
    )
    initiator_share = next((s for s in shares if s.is_initiator), None)
    expense_links = _expense_links(aa)
    return {
        # 合同字段（aa_request.json）
        "aa_id": aa.aa_id,
        "owner_user_id": aa.owner_user_id,
        "task_id": aa.task_id,
        "title": aa.title,
        "final_cost_minor": aa.final_cost_minor,
        "status": aa.status,
        "participant_shares": [share_view(s) for s in shares],
        "linked_orders": [entry["operation_id"] for entry in expense_links],
        "adjustments": list(aa.adjustments or []),
        "received_minor": collected,
        "received_minor_is_spendable": False,
        "reconciled": not any(s.status == SHARE_PENDING for s in shares),
        "currency": CURRENCY,
        "created_at": format_iso(aa.created_at),
        "updated_at": format_iso(aa.updated_at),
        # 界面聚合（只读接口与 DomainResult 顶层使用，不属于 aa_request.json）
        "final_cost": format_minor(aa.final_cost_minor),
        "collected_minor": collected,
        "collected": format_minor(collected),
        "outstanding_minor": outstanding,
        "outstanding": format_minor(outstanding),
        "pending_receivables_minor": outstanding,
        "pending_receivables_are_spendable": False,
        "initiator_included": initiator_share is not None,
        "initiator_share_minor": initiator_share.share_minor if initiator_share else 0,
        "initiator_share": share_view(initiator_share) if initiator_share else None,
        "self_funded_minor": self_funded,
        "adjusted_total_minor": adjusted_total,
        "refund_pending_minor": refund_pending,
        "collection_events": _collection_events(aa),
        "linked_order_details": expense_links,
        "collect_only": True,
        "auto_debit": False,
        "source": SOURCE,
        "version": DOMAIN_VERSION,
    }


def aa_contract_view(state: dict) -> dict:
    """把只读视图投影为 aa_request.json 合同对象。"""
    return {key: state[key] for key in CONTRACT_KEYS}


def _can_view(session: Session, ctx: AuthContext, aa: AARequest) -> bool:
    if ctx.is_operator or aa.owner_user_id == ctx.user_id:
        return True
    shares = _ordered_shares(session, aa.aa_id)
    return any(s.participant_user_id == ctx.user_id for s in shares)


def aa_view(session: Session, ctx: AuthContext, aa_id: str) -> dict:
    """读：发起人、运营角色与参与人本人都可查看自己的 AA。"""
    aa = _get_aa(session, aa_id)
    if not _can_view(session, ctx, aa):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该 AA 请求。")
    return aa_state(session, aa)


def list_aa_requests(session: Session, ctx: AuthContext, status: str | None = None) -> list[dict]:
    stmt = select(AARequest).where(AARequest.owner_user_id == ctx.user_id)
    if status:
        stmt = stmt.where(AARequest.status == status)
    rows = session.execute(stmt.order_by(AARequest.created_at.desc())).scalars().all()
    return [aa_state(session, aa) for aa in rows]


# ---------------------------------------------------------------------------
# 写：创建 AA
# ---------------------------------------------------------------------------


def create_aa_request(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    """``aa.create``：创建收款请求并冻结分摊结果；不产生任何资金变动。"""
    ctx.require_customer()
    cached = _cached_result(session, operation)
    if cached:
        return cached
    existing = _existing_aa_for_operation(session, operation.operation_id)
    if existing is not None:
        state = aa_state(session, existing)
        return _remember(
            session,
            operation,
            _result(
                operation,
                aa_contract_view(state),
                warnings=[COLLECT_ONLY_NOTICE],
                outstanding_minor=state["outstanding_minor"],
                initiator_share_minor=state["initiator_share_minor"],
                already_created=True,
            ),
        )

    args = dict(operation.typed_args or {})
    title = str(args.get("title") or "").strip()
    if not title:
        raise FinPilotError("PARAM_INVALID", "缺少 AA 标题。", details={"reason_codes": ["title_required"]})
    if len(title) > 96:
        raise FinPilotError("PARAM_INVALID", "AA 标题过长。", details={"reason_codes": ["title_too_long"]})

    final_cost_minor = _parse_final_cost(args)
    participants = _normalize_participants(args.get("participants"))
    initiator_included = bool(args.get("initiator_included", True))

    user = session.get(User, ctx.user_id)
    initiator_name = (user.display_name if user is not None else ctx.user_id)
    initiator_ref = str(args.get("initiator_ref") or ctx.user_id)
    initiator_weight = _int_or_none(args.get("initiator_weight"))
    if initiator_weight is not None and initiator_weight < 0:
        raise FinPilotError("PARAM_INVALID", "分摊权重不能为负。", details={"reason_codes": ["negative_weight"]})

    parties: list[dict] = []
    if initiator_included:
        parties.append(
            {
                "display_name": initiator_name,
                "participant_ref": initiator_ref,
                "participant_user_id": ctx.user_id,
                "weight": initiator_weight,
                "is_initiator": True,
            }
        )
    for item in participants:
        parties.append({**item, "is_initiator": False})

    shares = _split_shares(final_cost_minor, parties)

    now = utcnow()
    aa = AARequest(
        owner_user_id=ctx.user_id,
        task_id=operation.task_id,
        title=title,
        final_cost_minor=final_cost_minor,
        status=AA_OPEN,
        linked_orders=_normalize_linked_orders(args),
        adjustments=[],
        created_at=now,
        updated_at=now,
    )
    session.add(aa)
    session.flush()

    for index, (party, amount) in enumerate(zip(parties, shares)):
        is_initiator = bool(party["is_initiator"])
        session.add(
            AAShare(
                aa_id=aa.aa_id,
                participant_ref=party["participant_ref"],
                participant_user_id=party["participant_user_id"],
                display_name=party["display_name"],
                share_minor=amount,
                adjusted_minor=0,
                # 发起人自付份额单列（已由发起人承担）；其他份额为待收应收
                status=SHARE_PAID if is_initiator else SHARE_PENDING,
                is_initiator=is_initiator,
                operation_id=operation.operation_id,
                paid_at=now if is_initiator else None,
                # 按稳定参与人顺序微秒递增：持久层可复现"稳定顺序"（同事务内 created_at 相同）
                created_at=now + timedelta(microseconds=index + 1),
            )
        )
    session.flush()
    _refresh_aa_status(session, aa)
    session.flush()

    state = aa_state(session, aa)
    warnings = [COLLECT_ONLY_NOTICE]
    if initiator_included:
        warnings.append("发起人自付份额单列，不进入应收；未付款份额只标记应收。")
    return _remember(
        session,
        operation,
        _result(
            operation,
            aa_contract_view(state),
            warnings=warnings,
            outstanding_minor=state["outstanding_minor"],
            initiator_share_minor=state["initiator_share_minor"],
            self_funded_minor=state["self_funded_minor"],
        ),
    )


# ---------------------------------------------------------------------------
# 写：到账登记
# ---------------------------------------------------------------------------


def mark_share_paid(session: Session, aa_id: str, share_id: str, *, operation_id: str) -> dict:
    """登记某参与人份额到账（由该参与人自己的转账成功后调用），不移动资金。

    - 幂等：同一 ``operation_id`` 重复调用只登记一次。
    - 参与人身份：份额绑定了 ``participant_user_id`` 时，付款操作必须属于该参与人；
      发起人不能替参与人登记到账。
    """
    aa = _get_aa(session, aa_id)
    share = session.get(AAShare, share_id)
    if share is None or share.aa_id != aa.aa_id:
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该 AA 份额。")
    if not operation_id:
        raise FinPilotError("PARAM_INVALID", "缺少付款 operation_id。", details={"reason_codes": ["operation_id_required"]})

    for entry in _collection_events(aa):
        if entry.get("operation_id") == operation_id:
            return aa_state(session, aa)  # 同一付款操作重复登记：幂等返回

    if share.is_initiator:
        raise FinPilotError(
            "POLICY_DENIED",
            "发起人自付份额不是应收，无需登记到账。",
            details={"reason_codes": ["initiator_share_not_collectable"]},
        )
    if share.status == SHARE_PAID:
        raise FinPilotError(
            "POLICY_DENIED",
            "该份额已登记到账。",
            details={"reason_codes": ["share_already_paid"]},
        )

    operation = session.get(Operation, operation_id)
    if operation is None:
        raise FinPilotError(
            "PARAM_INVALID",
            "付款操作不存在，不能凭空登记到账。",
            details={"reason_codes": ["unknown_payment_operation"]},
        )
    if share.participant_user_id and operation.owner_user_id != share.participant_user_id:
        # 不泄露他人对象是否存在
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "该付款操作不属于本份额的参与人。")
    if operation.status in ("failed", "cancelled"):
        raise FinPilotError(
            "POLICY_DENIED",
            "付款操作已失败或取消，不能登记到账。",
            details={"reason_codes": ["payment_not_effective"], "operation_status": operation.status},
        )

    now = utcnow()
    share.status = SHARE_PAID
    share.paid_at = now
    aa.linked_orders = [
        *(aa.linked_orders or []),
        {
            "type": "collection",
            "share_id": share.share_id,
            "participant_ref": share.participant_ref,
            "participant_user_id": share.participant_user_id,
            "operation_id": operation_id,
            "tool": operation.tool,
            "amount_minor": share.share_minor,
            "recorded_at": format_iso(now),
        },
    ]
    _refresh_aa_status(session, aa)
    session.flush()
    return aa_state(session, aa)


def _refresh_aa_status(session: Session, aa: AARequest) -> None:
    shares = _ordered_shares(session, aa.aa_id)
    pending = [s for s in shares if s.status == SHARE_PENDING]
    collected = [s for s in shares if s.status == SHARE_PAID and not s.is_initiator]
    if aa.status == AA_CANCELLED:
        return
    if not pending:
        aa.status = AA_SETTLED
    elif collected:
        aa.status = AA_PARTIALLY_SETTLED
    else:
        aa.status = AA_OPEN
    aa.updated_at = utcnow()


# ---------------------------------------------------------------------------
# 写：退款调整
# ---------------------------------------------------------------------------


def apply_refund_adjustment(
    session: Session,
    aa_id: str,
    *,
    original_operation_id: str,
    refund_amount_minor: int,
    actor_ctx: AuthContext | None = None,
) -> dict:
    """原消费退款 → 生成可追溯分摊调整。

    - 调整按各份额金额用最大余数法分摊，合计等于退款金额。
    - 已收款份额（不含发起人自付份额）的退款条目只记为 ``pending_authorization``
      （需另行授权退款），本函数不移动任何资金、也不改变份额状态。
    - ``actor_ctx`` 给出时强制归属：只有 AA 发起人本人或运营角色可以登记退款调整。
    """
    aa = _get_aa(session, aa_id)
    if actor_ctx is not None and not (actor_ctx.is_operator or aa.owner_user_id == actor_ctx.user_id):
        raise FinPilotError("OBJECT_NOT_ACCESSIBLE", "无法访问该 AA 请求。")
    if not original_operation_id:
        raise FinPilotError(
            "PARAM_INVALID",
            "缺少原消费 operation_id。",
            details={"reason_codes": ["original_operation_required"]},
        )
    amount = _int_or_none(refund_amount_minor)
    if amount is None or amount <= 0:
        raise FinPilotError("PARAM_INVALID", "退款金额必须大于 0 分。", details={"reason_codes": ["invalid_refund_amount"]})

    order = next(
        (o for o in _expense_links(aa) if o.get("operation_id") == original_operation_id),
        None,
    )
    if order is None:
        raise FinPilotError(
            "PARAM_INVALID",
            "退款必须关联本 AA 已登记的原消费订单。",
            details={"reason_codes": ["unknown_original_order"], "original_operation_id": original_operation_id},
        )

    refunded_before = _refunded_total(aa, original_operation_id)
    original_amount = _int_or_none(order.get("amount_minor")) or aa.final_cost_minor
    cap = min(original_amount, aa.final_cost_minor)
    if refunded_before + amount > cap:
        raise FinPilotError(
            "POLICY_DENIED",
            "退款累计超过原消费可退金额。",
            details={
                "reason_codes": ["refund_exceeds_original"],
                "refunded_before_minor": refunded_before,
                "requested_minor": amount,
                "refundable_cap_minor": cap,
            },
        )

    shares = _ordered_shares(session, aa.aa_id)
    if not shares:
        raise FinPilotError("SYSTEM_ERROR", "AA 缺少份额记录，无法生成退款调整。")

    weights = [int(s.share_minor) for s in shares]
    if sum(weights) <= 0:
        raise FinPilotError("SYSTEM_ERROR", "AA 份额合计为 0，无法生成退款调整。")
    deltas = largest_remainder_split_weights(amount, weights)

    now = utcnow()
    refund_id = new_id("adj")
    entries: list[dict] = []
    for share, delta in zip(shares, deltas):
        share.adjusted_minor += int(delta)
        # 已收款的参与人份额：退款需要其本人另行授权，这里只标记待处理；
        # 发起人自付份额的退款随原订单退款回到发起人账户，不需要 AA 收款授权。
        needs_authorization = share.status == SHARE_PAID and not share.is_initiator
        entry_status = "pending_authorization" if needs_authorization else "applied"
        entries.append(
            {
                "type": "refund_adjustment",
                "refund_id": refund_id,
                "original_operation_id": original_operation_id,
                "refund_amount_minor": int(amount),
                "share_id": share.share_id,
                "participant_ref": share.participant_ref,
                "delta_minor": int(delta),
                "effective_share_minor": share.share_minor - share.adjusted_minor,
                "status": entry_status,
                "requires_separate_authorization": needs_authorization,
                "self_funded": bool(share.is_initiator),
                "recorded_at": format_iso(now),
            }
        )

    aa.adjustments = [*(aa.adjustments or []), *entries]
    aa.updated_at = now
    session.flush()
    return aa_state(session, aa)


# ---------------------------------------------------------------------------
# 执行器入口
# ---------------------------------------------------------------------------


def handle_aa_create(session: Session, ctx: AuthContext, operation: Operation) -> dict:
    return create_aa_request(session, ctx, operation)


TOOL_HANDLERS = {
    "aa.create": handle_aa_create,
}

__all__ = [
    "SOURCE",
    "DOMAIN",
    "DOMAIN_VERSION",
    "TERMS_VERSION",
    "COLLECT_ONLY_NOTICE",
    "TOOL_HANDLERS",
    "CONTRACT_KEYS",
    "SHARE_PENDING",
    "SHARE_PAID",
    "AA_OPEN",
    "AA_COLLECTING",
    "AA_PARTIALLY_SETTLED",
    "AA_SETTLED",
    "AA_CANCELLED",
    "create_aa_request",
    "handle_aa_create",
    "mark_share_paid",
    "apply_refund_adjustment",
    "aa_view",
    "aa_state",
    "aa_contract_view",
    "share_view",
    "list_aa_requests",
]