# -*- coding: utf-8 -*-
"""冻结数据集、种子与来源清单的来源/拆分不变量测试。

对应 V5 需求 DATA-03、EVAL-01、GOV-02 与任务书工作包 C01（第 4.2、9—15、19.1 节与附录 A/B）。

本文件是**纯数据测试**：只读取 `data/**` 与 `eval/data/**`，不需要 PostgreSQL、
不需要网络。为覆盖 `tests/conftest.py` 中依赖真实数据库的 ``clean_db``（autouse）夹具，
这里以同名夹具替换之（pytest 的夹具解析优先取测试模块内的定义）。
"""

from __future__ import annotations

import functools
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = REPO_ROOT / "data"
EVAL_DIR = REPO_ROOT / "eval" / "data"
GENERATOR = DATA_DIR / "synthetic" / "generate.py"

SEED = 20261022
REFERENCE_TIME = "2026-10-22T10:00:00+08:00"

SCENES = ("S1", "S2", "S3", "S4", "S5", "S6")
SCENE_TOTALS = {"executable": 60, "clarify": 20, "reject": 20}
SPLIT_TOTALS = {"dev": 300, "val": 120, "hidden": 180}
SCENE_SPLIT_TOTALS = {"dev": 50, "val": 20, "hidden": 30}
SCENE_SPLIT_TYPES = {
    "dev": {"executable": 30, "clarify": 10, "reject": 10},
    "val": {"executable": 12, "clarify": 4, "reject": 4},
    "hidden": {"executable": 18, "clarify": 6, "reject": 6},
}

TASK_FIELDS = (
    "task_id", "scene", "type", "template_family", "phrasing_group", "entities", "user_id",
    "initial_snapshot_ref", "utterance", "acceptable_final_states", "forbidden_effects",
    "required_clarifications", "label_reason", "split", "seed",
)
ATTACK_FIELDS = (
    "attack_id", "attack_class", "attacker_controlled_field", "injection_placement",
    "utterance_or_payload", "forbidden_effects", "judge", "split", "template_group",
)
FAULT_FIELDS = (
    "fault_id", "fault_class", "injection_point", "duration_ms", "expected_recoverability",
    "allowed_final_states", "target_task_id",
)

REQUIRED_ATTACK_CLASSES = {
    "merchant_description_injection",
    "bill_memo_injection",
    "tool_return_pseudo_instruction",
    "retrieval_doc_exfiltration",
    "cross_language_encoding_bypass",
    "impersonate_system_operator",
    "cross_user_object_access",
    "authorization_parameter_tampering",
    "grant_or_otp_replay",
    "transfer_split_limit_evasion",
    "forged_or_replayed_callback",
    "generated_code_sandbox_escape",
}

REQUIRED_FAULT_CLASSES = {
    "pre_submit_network_loss",
    "provider_accepted_response_lost",
    "repeated_query_failure",
    "duplicate_callback",
    "out_of_order_late_callback",
    "concurrent_fund_reservation_conflict",
    "quote_inventory_change",
    "terms_policy_version_change",
    "process_crash_at_commit",
    "scheduler_missed_window",
    "compensation_refused_or_refund_delay",
    "model_rate_limit_bad_schema_sandbox_unavailable",
}

TASK_STATUSES = (
    "draft", "clarifying", "planning", "awaiting_authorization", "ready", "running",
    "reconciling", "replanning", "awaiting_human", "succeeded", "partially_succeeded",
    "failed", "cancelled", "blocked", "missed",
)

SEED_SECTION_FIELDS = {
    "users": ("user_id", "display_name", "phone_ref", "password", "roles",
              "totp_secret_base32"),
    "accounts": ("account_id", "owner_user_id", "name", "account_type", "currency",
                 "posted_balance_minor", "masked_number"),
    "protection_policies": ("policy_id", "owner_user_id", "protected_floor_minor",
                            "account_scope", "currency"),
    "payees": ("payee_id", "owner_user_id", "display_name", "phone_ref",
               "destination_account_ref", "bank_name", "aliases"),
    "billing_transactions": ("bt_id", "owner_user_id", "account_id", "posted_at", "business_day",
                             "amount_minor", "direction", "kind", "category", "merchant_name",
                             "counterparty", "memo", "linked_bt_id", "anomaly_label",
                             "untrusted_text", "source"),
    "products": ("product_id", "name", "issuer", "product_type", "terms_version",
                 "issuer_risk_level", "min_minor", "fee_bps", "fee_fixed_minor", "term_days",
                 "open_days", "cutoff_time", "liquidity_terms", "cancel_terms",
                 "cooling_off_days", "settlement_days", "history_ref"),
    "product_navs": ("product_id", "as_of", "nav_e6", "daily_return_bps"),
    "holdings": ("owner_user_id", "account_id", "product_id", "confirmed_shares_e8",
                 "frozen_shares_e8"),
    "cards": ("card_id", "owner_user_id", "account_id", "masked_pan", "lifecycle_status",
              "credit_limit_minor"),
    "card_controls": ("card_id", "control_type", "channel", "limit_minor", "reason",
                      "created_by", "created_by_role", "active"),
    "card_applications": ("application_id", "owner_user_id", "account_id", "kind", "card_id",
                          "requested_limit_minor", "application_status", "decision_reason"),
    "subscriptions": ("subscription_id", "owner_user_id", "merchant_id", "merchant_name",
                      "amount_minor", "period", "next_charge_at", "status", "evidence",
                      "terms_version", "cancel_terms"),
    "mandates": ("mandate_id", "owner_user_id", "subscription_id", "merchant_id", "account_id",
                 "status"),
    "merchant_offers": ("merchant_id", "name", "category", "offer_code", "title", "amount_minor",
                        "fee_minor", "cancel_fee_minor", "refundable", "inventory",
                        "terms_version", "untrusted_text"),
    "aa_requests": ("aa_id", "owner_user_id", "title", "final_cost_minor", "status",
                    "linked_orders", "shares"),
}

AA_SHARE_FIELDS = ("participant_ref", "display_name", "share_minor", "is_initiator", "status")

SEED_MODEL_MAP = {
    "users": ("User", {"password": "password_hash", "totp_secret_base32": "totp_secret_enc"}),
    "accounts": ("Account", {}),
    "protection_policies": ("ProtectionPolicy", {}),
    "payees": ("Payee", {}),
    "billing_transactions": ("BillingTransaction", {}),
    "products": ("WealthProduct", {}),
    "product_navs": ("ProductNav", {}),
    "holdings": ("Holding", {}),
    "cards": ("Card", {}),
    "card_controls": ("CardControl", {}),
    "card_applications": ("CardApplication", {}),
    "subscriptions": ("Subscription", {}),
    "mandates": ("Mandate", {}),
    "aa_requests": ("AARequest", {}),
}

ARTIFACT_RELPATHS = (
    "data/synthetic/seed_app.json",
    "data/synthetic/tasks.json",
    "data/metadata/source_manifest.json",
    "eval/data/attack_catalog.json",
    "eval/data/fault_catalog.json",
    "eval/data/task_index.json",
    "eval/data/dev/tasks.json",
    "eval/data/dev/attacks.json",
    "eval/data/val/tasks.json",
    "eval/data/hidden/tasks.json",
    "eval/data/hidden/attacks.json",
)

#: 生成到临时仓库根时仍然字节一致的产物（不含依赖仓库文档哈希的来源清单与索引）。
PATH_INDEPENDENT_ARTIFACTS = tuple(
    path for path in ARTIFACT_RELPATHS
    if path not in {"data/metadata/source_manifest.json", "eval/data/task_index.json"}
)


@pytest.fixture(autouse=True)
def clean_db():
    """覆盖 tests/conftest.py 的数据库夹具：来源测试不需要 PostgreSQL。"""
    yield


def _load(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _walk(payload, path="$"):
    """产出 (路径, 值) 便于检查浮点与金额类型。"""
    if isinstance(payload, dict):
        for key, value in payload.items():
            yield from _walk(value, f"{path}.{key}")
    elif isinstance(payload, list):
        for idx, value in enumerate(payload):
            yield from _walk(value, f"{path}[{idx}]")
    else:
        yield path, payload


@pytest.fixture(scope="module")
def seed_app() -> dict:
    return _load(DATA_DIR / "synthetic" / "seed_app.json")


@pytest.fixture(scope="module")
def tasks() -> list:
    return _load(DATA_DIR / "synthetic" / "tasks.json")


@pytest.fixture(scope="module")
def attacks() -> list:
    return _load(EVAL_DIR / "attack_catalog.json")


@pytest.fixture(scope="module")
def faults() -> list:
    return _load(EVAL_DIR / "fault_catalog.json")


@pytest.fixture(scope="module")
def index() -> dict:
    return _load(EVAL_DIR / "task_index.json")


@pytest.fixture(scope="module")
def manifest() -> dict:
    return _load(DATA_DIR / "metadata" / "source_manifest.json")


# ---------------------------------------------------------------------------
# 1. 数量、比例与拆分
# ---------------------------------------------------------------------------


def test_task_totals_types_and_splits(tasks, index):
    assert len(tasks) == 600
    assert {t["scene"] for t in tasks} == set(SCENES)

    for scene in SCENES:
        scene_tasks = [t for t in tasks if t["scene"] == scene]
        assert len(scene_tasks) == 100, f"{scene} 任务数应为 100"
        for task_type, expected in SCENE_TOTALS.items():
            got = sum(1 for t in scene_tasks if t["type"] == task_type)
            assert got == expected, f"{scene}/{task_type} 应为 {expected}，实际 {got}"
        for split, expected_total in SCENE_SPLIT_TOTALS.items():
            assert sum(1 for t in scene_tasks if t["split"] == split) == expected_total
            for task_type, expected in SCENE_SPLIT_TYPES[split].items():
                got = sum(1 for t in scene_tasks
                          if t["split"] == split and t["type"] == task_type)
                assert got == expected, f"{scene}/{split}/{task_type} 应为 {expected}，实际 {got}"

    assert sum(1 for t in tasks if t["split"] == "dev") == SPLIT_TOTALS["dev"]
    assert sum(1 for t in tasks if t["split"] == "val") == SPLIT_TOTALS["val"]
    assert sum(1 for t in tasks if t["split"] == "hidden") == SPLIT_TOTALS["hidden"]
    assert sum(1 for t in tasks if t["type"] == "executable") == 360
    assert sum(1 for t in tasks if t["type"] == "clarify") == 120
    assert sum(1 for t in tasks if t["type"] == "reject") == 120

    # 每个拆分内部的类型比例均为 60% / 20% / 20%
    for split in ("dev", "val", "hidden"):
        subset = [t for t in tasks if t["split"] == split]
        ratio = sum(1 for t in subset if t["type"] == "executable") / len(subset)
        assert ratio == pytest.approx(0.6)

    assert index["counts"]["tasks_total"] == 600
    assert index["counts"]["tasks_by_split"] == SPLIT_TOTALS
    assert index["counts"]["tasks_by_type"] == {"executable": 360, "clarify": 120, "reject": 120}
    assert index["counts"]["tasks_by_scene"] == {scene: 100 for scene in SCENES}


def test_task_records_are_complete_and_typed(tasks):
    for task in tasks:
        missing = [field for field in TASK_FIELDS if field not in task]
        assert not missing, f"{task.get('task_id')} 缺字段 {missing}"
        assert task["type"] in {"executable", "clarify", "reject"}
        assert task["split"] in {"dev", "val", "hidden"}
        assert task["scene"] in SCENES
        assert isinstance(task["entities"], dict) and task["entities"]
        assert isinstance(task["acceptable_final_states"], list) and task["acceptable_final_states"]
        assert isinstance(task["forbidden_effects"], list) and task["forbidden_effects"]
        assert isinstance(task["required_clarifications"], list)
        assert isinstance(task["seed"], int) and task["seed"] >= SEED
        assert isinstance(task["utterance"], str) and len(task["utterance"]) >= 6
        assert re.search(r"[\u4e00-\u9fff]", task["utterance"]), "语句应为中文自然语言"
        assert isinstance(task["label_reason"], str) and len(task["label_reason"]) >= 8
        assert isinstance(task["template_family"], str) and task["template_family"]
        assert isinstance(task["phrasing_group"], str) and task["phrasing_group"]
        assert task["initial_snapshot_ref"] in index_snapshots()


@functools.lru_cache(maxsize=1)
def index_snapshots() -> frozenset:
    return frozenset(_load(EVAL_DIR / "task_index.json")["snapshots"])


def test_task_ids_and_utterances_are_unique(tasks):
    ids = [t["task_id"] for t in tasks]
    assert len(set(ids)) == 600
    utterances = [t["utterance"] for t in tasks]
    assert len(set(utterances)) == 600, "600 条语句应互不重复"


def test_task_entities_belong_to_task_owner(seed_app, tasks):
    """任务引用的账户/收款人/卡片必须归属该任务的用户，避免伪可执行任务。"""
    account_owner = {a["account_id"]: a["owner_user_id"] for a in seed_app["accounts"]}
    payee_owner = {p["payee_id"]: p["owner_user_id"] for p in seed_app["payees"]}
    card_owner = {c["card_id"]: c["owner_user_id"] for c in seed_app["cards"]}
    users = {u["user_id"] for u in seed_app["users"]}
    for task in tasks:
        assert task["user_id"] in users, f"{task['task_id']} 的 user_id 不在种子用户中"
        entities = task["entities"]
        for key, owner_map in (("account_id", account_owner), ("payee_id", payee_owner),
                               ("card_id", card_owner), ("old_card_id", card_owner)):
            value = entities.get(key)
            if value is None:
                continue
            if value not in owner_map:
                continue  # 例如 old_card_id/占位提示文本不是真实对象 ID
            assert owner_map[value] == task["user_id"], (
                f"{task['task_id']} 引用了非本用户的 {key}={value}"
            )


def test_clarification_labels_are_consistent(tasks):
    for task in tasks:
        if task["type"] == "clarify":
            assert task["required_clarifications"], f"{task['task_id']} 需澄清任务必须有澄清项"
            assert any("clarifying" in state for state in task["acceptable_final_states"])
        else:
            assert not task["required_clarifications"], (
                f"{task['task_id']} 非需澄清任务不应带澄清项（前置条件写在 label_reason）"
            )
        if task["type"] == "reject":
            assert any(state.startswith("task_refused") or state.startswith("request_rejected")
                       for state in task["acceptable_final_states"]), (
                f"{task['task_id']} 应拒绝任务的终态应体现拒绝"
            )


# ---------------------------------------------------------------------------
# 2. 拆分泄漏检查
# ---------------------------------------------------------------------------


def _group_split_map(items, field):
    mapping: dict[str, set] = {}
    for item in items:
        mapping.setdefault(item[field], set()).add(item["split"])
    return mapping


@pytest.mark.parametrize("field", ["template_family", "phrasing_group"])
def test_groups_never_cross_dev_and_hidden(tasks, field):
    mapping = _group_split_map(tasks, field)
    offenders = {key: sorted(splits) for key, splits in mapping.items() if len(splits) > 1}
    assert offenders == {}, f"{field} 跨拆分：{offenders}"
    for key, splits in mapping.items():
        assert splits <= {"dev"} or splits <= {"val"} or splits <= {"hidden"}


def test_leakage_checks_recorded_in_index(index, tasks):
    checks = index["leakage_checks"]
    assert checks["family_cross_split_count"] == 0
    assert checks["phrasing_group_cross_split_count"] == 0
    assert checks["shared_utterance_count"] == 0
    assert checks["hidden_novelty"], "隐藏集必须记录开发集未出现的组合/依赖结构"
    # 隐藏集必须包含开发集未出现的跨场景组合说明
    assert any("S6" in note for note in checks["hidden_novelty"])


def test_split_files_match_full_catalog(tasks, attacks):
    for split in ("dev", "val", "hidden"):
        subset = _load(EVAL_DIR / split / "tasks.json")
        expected = [t for t in tasks if t["split"] == split]
        assert [t["task_id"] for t in subset] == [t["task_id"] for t in expected]
        assert all(t["split"] == split for t in subset)
    for split in ("dev", "hidden"):
        subset = _load(EVAL_DIR / split / "attacks.json")
        expected = [a for a in attacks if a["split"] == split]
        assert [a["attack_id"] for a in subset] == [a["attack_id"] for a in expected]


# ---------------------------------------------------------------------------
# 3. 金额整数性与词汇表一致性
# ---------------------------------------------------------------------------


def test_no_floats_and_money_is_integer(seed_app, tasks, attacks, faults, manifest):
    payloads = {
        "seed_app": seed_app, "tasks": tasks, "attacks": attacks,
        "faults": faults, "manifest": manifest,
    }
    for label, payload in payloads.items():
        floats = [path for path, value in _walk(payload) if isinstance(value, float)]
        assert floats == [], f"{label} 含浮点数：{floats[:5]}"

    money_patterns = ("_minor", "_e8", "_e6", "_bps")
    for label, payload in payloads.items():
        for path, value in _walk(payload):
            key = path.rsplit(".", 1)[-1].split("[")[0]
            if key.endswith(money_patterns):
                if value is None:
                    # 可空列（如 card_controls.limit_minor 表示“不限额”）允许 null，
                    # 但不允许出现浮点数或字符串金额。
                    continue
                assert isinstance(value, int) and not isinstance(value, bool), (
                    f"{label}{path} 金额/份额/净值必须为整数分/定点整数"
                )


def test_seed_amounts_and_balances(seed_app):
    accounts = {a["account_id"]: a for a in seed_app["accounts"]}
    assert accounts["account_salary"]["posted_balance_minor"] == 900_000  # 9000 元
    assert accounts["account_savings"]["posted_balance_minor"] == 1_500_000
    assert accounts["account_salary"]["currency"] == "CNY"
    policy = seed_app["protection_policies"][0]
    assert policy["protected_floor_minor"] == 800_000  # 8000 元
    assert policy["account_scope"] == ["account_salary"]
    # 可支配 = 900000 - 800000 = 100000 分（V5 第 14.2 节端到端示例）
    assert accounts["account_salary"]["posted_balance_minor"] - policy["protected_floor_minor"] \
        == 100_000
    for row in seed_app["billing_transactions"]:
        assert row["amount_minor"] > 0, "账单流水的金额一律为正，方向由 direction 表达"
        assert row["direction"] in {"in", "out"}
        assert row["source"] == "synthetic"


def test_effect_and_clarification_vocabulary_is_registered(tasks, index):
    final_states = index["final_state_vocabulary"]
    effects = index["forbidden_effect_vocabulary"]
    clarifications = index["clarification_vocabulary"]
    for task in tasks:
        for state in task["acceptable_final_states"]:
            assert state in final_states, f"{task['task_id']} 未登记终态 {state}"
        for effect in task["forbidden_effects"]:
            assert effect in effects, f"{task['task_id']} 未登记禁止效果 {effect}"
        for item in task["required_clarifications"]:
            assert item in clarifications, f"{task['task_id']} 未登记澄清项 {item}"


# ---------------------------------------------------------------------------
# 4. 种子 schema、类型与 models.py 映射
# ---------------------------------------------------------------------------


def test_seed_schema_keys_and_types(seed_app):
    assert seed_app["dataset_version"] == "seed-2026.10"
    assert seed_app["generated_at"] == REFERENCE_TIME
    assert seed_app["seed"] == SEED
    for section, fields in SEED_SECTION_FIELDS.items():
        rows = seed_app.get(section)
        assert isinstance(rows, list) and rows, f"seed_app.{section} 缺失或为空"
        for row in rows:
            assert set(row.keys()) == set(fields), (
                f"seed_app.{section} 键名不符：多 {set(row) - set(fields)} "
                f"缺 {set(fields) - set(row)}"
            )
    for user in seed_app["users"]:
        assert user["roles"] == ["customer"]
        assert isinstance(user["password"], str) and user["password"]
        assert re.fullmatch(r"[A-Z2-7]{16,}", user["totp_secret_base32"])
    for account in seed_app["accounts"]:
        assert isinstance(account["posted_balance_minor"], int)
        assert account["masked_number"], "账户号必须脱敏展示"
    for offer in seed_app["merchant_offers"]:
        assert isinstance(offer["refundable"], bool)
        assert isinstance(offer["inventory"], int)
    for aa in seed_app["aa_requests"]:
        for share in aa["shares"]:
            assert set(share.keys()) == set(AA_SHARE_FIELDS)
            assert isinstance(share["is_initiator"], bool)


def test_seed_fields_map_to_models_columns(seed_app):
    """种子字段必须能被 services/api/src/finpilot/models.py 直接映射。"""
    src = REPO_ROOT / "services" / "api" / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    try:
        from finpilot import models
    except Exception as exc:  # pragma: no cover - 模型暂不可导入时如实跳过
        pytest.skip(f"finpilot.models 不可导入：{exc}")

    for section, (class_name, aliases) in SEED_MODEL_MAP.items():
        model = getattr(models, class_name, None)
        assert model is not None, f"models 缺少 {class_name}"
        columns = {column.key for column in model.__table__.columns}
        for row in seed_app[section]:
            for field in row:
                if section == "aa_requests" and field == "shares":
                    # shares 映射到子表 AAShare，由下方单独断言。
                    continue
                target = aliases.get(field, field)
                assert target in columns, (
                    f"{section}.{field} → {class_name}.{target} 不存在于模型列"
                )
    aashare_columns = {column.key for column in models.AAShare.__table__.columns}
    for field in AA_SHARE_FIELDS:
        assert field in aashare_columns, f"AAShare 缺少列 {field}"


def test_seed_users_accounts_payees_are_as_specified(seed_app):
    users = {u["user_id"] for u in seed_app["users"]}
    assert {"user_demo_01", "user_demo_02", "user_demo_03"} <= users
    assert all(u["roles"] == ["customer"] for u in seed_app["users"])
    owners = {a["owner_user_id"] for a in seed_app["accounts"]}
    assert owners <= users, "账户归属必须是已存在的用户（外键可满足）"
    for control in seed_app["card_controls"]:
        assert control["card_id"] in {c["card_id"] for c in seed_app["cards"]}
    for mandate in seed_app["mandates"]:
        assert mandate["subscription_id"] in {s["subscription_id"] for s in seed_app["subscriptions"]}
    # 两个同名“张三”，脱敏目的账号不同
    zhangsans = [p for p in seed_app["payees"] if p["display_name"] == "张三"]
    assert len(zhangsans) == 2
    assert len({p["destination_account_ref"] for p in zhangsans}) == 2
    assert len({p["payee_id"] for p in zhangsans}) == 2
    for payee in seed_app["payees"]:
        assert "****" in payee["destination_account_ref"], "目的账号必须脱敏"


# ---------------------------------------------------------------------------
# 5. 账单流水构成
# ---------------------------------------------------------------------------


def test_billing_transactions_cover_24_months_within_range(seed_app):
    rows = seed_app["billing_transactions"]
    assert 1200 <= len(rows) <= 1800, f"24 个月流水应在 1200—1800 条，实际 {len(rows)}"
    main = [r for r in rows if r["owner_user_id"] == "user_demo_01"]
    assert 1200 <= len(main) <= 1800
    months = sorted({r["business_day"][:7] for r in main})
    assert len(months) == 24, f"应为 24 个自然月，实际 {len(months)}"
    assert months[0] == "2024-11" and months[-1] == "2026-10"
    for row in rows:
        assert row["posted_at"].startswith(row["business_day"]), "业务日应与带时区时间一致"
        assert row["posted_at"].endswith("+08:00")
        assert len(row["bt_id"]) >= 6


def test_billing_contains_required_business_patterns(seed_app):
    rows = seed_app["billing_transactions"]
    by_kind: dict[str, list] = {}
    for row in rows:
        by_kind.setdefault(row["kind"], []).append(row)

    assert len(by_kind.get("internal_transfer", [])) >= 24, "必须含账户内部互转"
    assert all(r["category"] == "internal_transfer" for r in by_kind["internal_transfer"])
    # 内部互转成对关联
    paired = [r for r in by_kind["internal_transfer"] if r["linked_bt_id"]]
    ids = {r["bt_id"] for r in rows}
    assert paired and all(r["linked_bt_id"] in ids for r in paired)

    assert by_kind.get("wealth_purchase"), "必须含理财申购（资产调拨）"
    assert by_kind.get("wealth_redeem"), "必须含理财赎回（资产调拨）"
    for row in by_kind["wealth_purchase"] + by_kind["wealth_redeem"]:
        assert row["category"] == "asset_allocation"

    fees = by_kind.get("fee", [])
    assert fees, "手续费必须单列"
    assert any(r["linked_bt_id"] for r in fees), "手续费应可追溯到原交易"

    labels = {r["anomaly_label"] for r in rows if r["anomaly_label"]}
    assert {"duplicate_same_amount", "merchant_burst_repeat", "outlier_vs_history"} <= labels
    assert any(r["untrusted_text"] for r in rows), "必须含账单备注注入样本（untrusted_text）"


def test_cross_period_refunds_link_previous_period(seed_app):
    rows = seed_app["billing_transactions"]
    by_id = {r["bt_id"]: r for r in rows}
    refunds = [r for r in rows if r["kind"] == "refund"]
    assert len(refunds) >= 6
    cross_period = []
    for refund in refunds:
        if not refund["linked_bt_id"]:
            continue
        original = by_id[refund["linked_bt_id"]]
        assert original["kind"] == "purchase"
        assert original["business_day"] < refund["business_day"]
        if original["business_day"][:7] != refund["business_day"][:7]:
            cross_period.append(refund)
    assert len(cross_period) >= 6, "跨期退款应为本期退款 + 上期原消费"
    unmatched = [r for r in refunds if not r["linked_bt_id"]]
    assert unmatched and all(r["anomaly_label"] == "unmatched_refund" for r in unmatched)


# ---------------------------------------------------------------------------
# 6. 理财产品、卡片、订阅、商户与 AA
# ---------------------------------------------------------------------------


def test_wealth_products_cover_risk_levels_and_terms(seed_app):
    products = seed_app["products"]
    assert len(products) >= 6
    levels = {p["issuer_risk_level"] for p in products}
    assert {"R1", "R2", "R3", "R4", "R5"} <= levels, "风险等级需覆盖 R1—R5"
    no_history = [p for p in products if not p["history_ref"]]
    assert len(no_history) == 1, "必须有一只历史数据不足的产品"
    navs_by_product: dict[str, int] = {}
    for nav in seed_app["product_navs"]:
        navs_by_product[nav["product_id"]] = navs_by_product.get(nav["product_id"], 0) + 1
    assert navs_by_product.get(no_history[0]["product_id"], 0) == 0, "历史不足产品不得有净值行"
    assert any("不可撤单" in p["cancel_terms"] for p in products), "必须含不可撤单产品"
    assert any(p["cooling_off_days"] > 0 for p in products), "必须含有冷静期的产品"
    for product in products:
        assert product["open_days"] and all(1 <= d <= 7 for d in product["open_days"])
        assert re.fullmatch(r"\d{2}:\d{2}", product["cutoff_time"])
    for nav in seed_app["product_navs"]:
        assert nav["nav_e6"] > 0
        assert nav["product_id"] in {p["product_id"] for p in products}


def test_cards_controls_and_applications(seed_app):
    cards = {c["card_id"]: c for c in seed_app["cards"]}
    assert len(cards) >= 3
    assert {"active", "lost", "inactive"} <= {c["lifecycle_status"] for c in cards.values()}
    controls = seed_app["card_controls"]
    active_card = next(c["card_id"] for c in cards.values() if c["lifecycle_status"] == "active")
    active_types = {c["control_type"] for c in controls if c["card_id"] == active_card and c["active"]}
    assert "user_lock" in active_types and "risk_freeze" in active_types, (
        "active 卡必须同时存在用户锁与风控冻结"
    )
    assert any(not c["active"] for c in controls), "应保留已解除限制的历史记录"
    statuses = {a["application_status"] for a in seed_app["card_applications"]}
    assert statuses & {"submitted", "under_review", "approved", "rejected", "withdrawn"}
    for app in seed_app["card_applications"]:
        assert app["application_status"] in {
            "submitted", "under_review", "approved", "rejected", "withdrawn"
        }
    # 申请状态与卡生命周期分离：inactive 虚拟卡不应被当成“已批准有效卡”
    assert any(a["card_id"] is None for a in seed_app["card_applications"])


def test_subscriptions_and_mandates(seed_app):
    subs = seed_app["subscriptions"]
    periods = [s["period"] for s in subs]
    assert periods.count("monthly") >= 2, "月度订阅至少 2 个"
    assert periods.count("yearly") >= 2, "年度订阅至少 2 个"
    assert any(s["evidence"] == "inferred" for s in subs), "必须含仅有流水证据的疑似周期支出"
    for sub in subs:
        assert sub["evidence"] in {"contract", "inferred"}
        assert sub["status"] in {"active", "cancelled", "pending_cancellation", "ended"}
        assert sub["amount_minor"] > 0
        assert sub["next_charge_at"] is None or sub["next_charge_at"].endswith("+08:00")
    inferred_ids = {s["subscription_id"] for s in subs if s["evidence"] == "inferred"}
    mandates = seed_app["mandates"]
    assert any(m["status"] == "revoked" for m in mandates), "应保留已解除的代扣记录"
    for mandate in mandates:
        assert mandate["status"] in {"active", "revoked", "pending_revocation"}
        if mandate["subscription_id"] in inferred_ids:
            assert mandate["status"] in {"active", "revoked", "pending_revocation"}


def test_merchant_offers_cover_required_categories(seed_app):
    offers = seed_app["merchant_offers"]
    by_category: dict[str, list] = {}
    for offer in offers:
        by_category.setdefault(offer["category"], []).append(offer)
    for category in ("flowers", "cake", "dining"):
        assert len(by_category.get(category, [])) >= 2, f"{category} 报价至少 2 条"
    assert any(o["inventory"] == 0 for o in offers), "必须含缺货报价"
    assert any(not o["refundable"] for o in offers), "必须含不可退报价"
    assert any(o["cancel_fee_minor"] > 0 for o in offers), "必须含取消费 > 0 的报价"
    assert len({o["merchant_id"] for o in offers}) >= 4
    assert any(o["untrusted_text"] and "忽略" in o["untrusted_text"] for o in offers), (
        "商户描述需含不可信注入样本"
    )


def test_aa_request_split_equals_total_with_largest_remainder(seed_app):
    requests = seed_app["aa_requests"]
    assert requests, "必须含一条 AA 历史请求"
    for aa in requests:
        shares = aa["shares"]
        assert sum(s["share_minor"] for s in shares) == aa["final_cost_minor"], (
            "AA 分摊合计必须等于总额"
        )
        assert max(s["share_minor"] for s in shares) - min(s["share_minor"] for s in shares) <= 1
        assert sum(1 for s in shares if s["is_initiator"]) == 1
        assert set(aa["linked_orders"]) <= {
            r["bt_id"] for r in seed_app["billing_transactions"]
        }, "AA 关联订单必须指向真实流水"
        assert all(s["status"] in {"pending", "paid"} for s in shares)
    first = requests[0]
    assert first["final_cost_minor"] == 10_000  # 100 元三人
    assert sorted((s["share_minor"] for s in first["shares"]), reverse=True) == [3334, 3333, 3333]
    initiator = next(s for s in first["shares"] if s["is_initiator"])
    assert initiator["share_minor"] == 3334, "最大余数应给稳定顺序的第一位（发起人）"


# ---------------------------------------------------------------------------
# 7. 攻击与故障目录
# ---------------------------------------------------------------------------


def test_attack_catalog_counts_classes_and_fields(attacks, index):
    assert len(attacks) == 200
    assert sum(1 for a in attacks if a["split"] == "dev") == 100
    assert sum(1 for a in attacks if a["split"] == "hidden") == 100
    classes = {a["attack_class"] for a in attacks}
    assert REQUIRED_ATTACK_CLASSES <= classes, (
        f"缺少攻击类别：{REQUIRED_ATTACK_CLASSES - classes}"
    )
    assert len(classes) >= 12
    judges = index["judge_definitions"]
    groups: dict[str, set] = {}
    for attack in attacks:
        assert set(attack.keys()) == set(ATTACK_FIELDS), f"{attack['attack_id']} 字段不符"
        assert attack["split"] in {"dev", "hidden"}
        assert attack["judge"] in judges, f"未登记判定器 {attack['judge']}"
        assert attack["forbidden_effects"], "攻击必须预定义目标禁止效果"
        assert attack["utterance_or_payload"].strip()
        assert attack["attacker_controlled_field"] and attack["injection_placement"]
        groups.setdefault(attack["template_group"], set()).add(attack["split"])
    offenders = {k: sorted(v) for k, v in groups.items() if len(v) > 1}
    assert offenders == {}, f"攻击 template_group 跨拆分：{offenders}"
    assert len({a["attack_id"] for a in attacks}) == 200
    assert len({a["utterance_or_payload"] for a in attacks}) == 200
    for attack in attacks:
        for effect in attack["forbidden_effects"]:
            assert effect in index["forbidden_effect_vocabulary"], effect


def test_fault_catalog_covers_classes_and_distinct_hidden_tasks(faults, tasks, index):
    assert len(faults) >= 50
    classes = {f["fault_class"] for f in faults}
    assert REQUIRED_FAULT_CLASSES <= classes, f"缺少故障类别：{REQUIRED_FAULT_CLASSES - classes}"
    assert len(classes) >= 12
    hidden_exec = {t["task_id"] for t in tasks
                   if t["split"] == "hidden" and t["type"] == "executable"}
    assert len(hidden_exec) >= 50
    targets = [f["target_task_id"] for f in faults]
    assert len(set(targets)) == len(targets), "故障变体必须绑定互不重复的基础任务"
    for fault in faults:
        assert set(fault.keys()) == set(FAULT_FIELDS), f"{fault['fault_id']} 字段不符"
        assert fault["target_task_id"] in hidden_exec, "故障目标必须是隐藏可执行任务"
        assert isinstance(fault["duration_ms"], int) and fault["duration_ms"] > 0
        assert fault["expected_recoverability"] in {
            "recoverable", "recoverable_after_requery",
            "recoverable_with_reauthorization", "not_recoverable_requires_handoff",
        }
        assert fault["allowed_final_states"]
        assert set(fault["allowed_final_states"]) <= set(TASK_STATUSES)
        assert fault["injection_point"]
    assert index["counts"]["faults_total"] == len(faults)
    assert index["counts"]["fault_classes"] == len(classes)


# ---------------------------------------------------------------------------
# 8. 来源清单（DATA-03 / GOV-02）
# ---------------------------------------------------------------------------


def test_source_manifest_fields_and_declaration(manifest):
    entries = manifest["entries"]
    assert entries
    required = {"name", "type", "license", "synthetic_ratio", "available_at", "sha256",
                "notes", "retention"}
    names = set()
    for entry in entries:
        assert set(entry.keys()) == required, f"{entry.get('name')} 字段不符：{set(entry)}"
        assert entry["type"] in {"original", "synthetic", "public"}
        assert isinstance(entry["synthetic_ratio"], int) and 0 <= entry["synthetic_ratio"] <= 1
        assert entry["license"] and entry["notes"] and entry["retention"]
        assert entry["sha256"] is None or re.fullmatch(r"[0-9a-f]{64}", entry["sha256"])
        names.add(entry["name"])
    assert len(names) == len(entries), "来源条目 name 应唯一"

    declaration = manifest["declaration"]
    assert declaration["all_financial_data_is_synthetic"] is True
    assert declaration["no_real_personal_data"] is True
    assert declaration["no_real_bank_authorization"] is True
    assert declaration["stockquant_license_verified"] is False
    assert "待核验" in declaration["stockquant_license_status"]
    assert "合成" in declaration["notes"]

    synthetic = [e for e in entries if e["type"] == "synthetic"]
    assert synthetic and all(e["synthetic_ratio"] == 1 for e in synthetic)
    assert {"finpilot_seed_app", "finpilot_tasks_600", "finpilot_attack_catalog_200",
            "finpilot_fault_catalog"} <= {e["name"] for e in synthetic}
    assert {"original", "public"} <= {e["type"] for e in entries}
    assert manifest["dataset_version"] == "seed-2026.10"
    assert manifest["seed"] == SEED


def test_stockquant_license_is_registered_as_unverified(manifest):
    entry = next(e for e in manifest["entries"] if "stockquant" in e["name"].lower())
    assert entry["type"] == "public"
    assert "待核验" in entry["license"]
    assert "未核验" in entry["license"] or "禁止声称已核验" in entry["license"]
    assert "未核验" in entry["notes"] or "待核验" in entry["notes"]
    assert entry["synthetic_ratio"] == 0


def test_manifest_hashes_match_files_on_disk(manifest):
    by_name = {e["name"]: e for e in manifest["entries"]}
    seed_entry = by_name["finpilot_seed_app"]
    assert seed_entry["sha256"] == _sha256(DATA_DIR / "synthetic" / "seed_app.json")
    tasks_entry = by_name["finpilot_tasks_600"]
    assert tasks_entry["sha256"] == _sha256(DATA_DIR / "synthetic" / "tasks.json")
    interfaces = by_name["finpilot_interfaces_frozen"]
    assert interfaces["sha256"] == _sha256(REPO_ROOT / "docs" / "INTERFACES.md")


def test_index_artifact_hashes_match_disk_and_dataset_hash(index):
    artifacts = index["artifacts"]
    assert artifacts
    listed = {a["path_rel"] for a in artifacts}
    for relpath in ARTIFACT_RELPATHS:
        if relpath == "eval/data/task_index.json":
            # 索引不能登记自身哈希（否则自引用），其一致性由 dataset_hash 复算保证。
            assert relpath not in listed
            continue
        assert relpath in listed, f"索引缺少产物 {relpath}"
    assert listed <= set(ARTIFACT_RELPATHS)
    for artifact in artifacts:
        path = REPO_ROOT / artifact["path_rel"]
        assert path.is_file(), f"产物不存在：{artifact['path_rel']}"
        assert artifact["sha256"] == _sha256(path), artifact["path_rel"]
        assert artifact["bytes"] == path.stat().st_size, artifact["path_rel"]

    recomputed = hashlib.sha256(
        json.dumps(sorted((a["path_rel"], a["sha256"]) for a in artifacts),
                   ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert index["dataset_hash"] == recomputed
    assert re.fullmatch(r"[0-9a-f]{64}", index["dataset_hash"])
    assert index["seed"] == SEED
    assert index["reference_time"] == REFERENCE_TIME


# ---------------------------------------------------------------------------
# 9. 确定性重放
# ---------------------------------------------------------------------------


def _run_generator(repo_root: Path) -> None:
    out_dir = repo_root / "data"
    log = repo_root / "generate.log"
    cmd = [
        sys.executable, str(GENERATOR),
        "--out-dir", str(out_dir),
        "--repo-root", str(repo_root),
        "--quiet",
    ]
    # 不使用管道捕获输出：日志写文件，避免受限环境下命名管道不可用。
    with log.open("wb") as handle:
        proc = subprocess.run(cmd, stdout=handle, stderr=handle, check=False)
    assert proc.returncode == 0, f"生成器失败：{log.read_text(encoding='utf-8', errors='replace')}"


def _artifact_hashes(repo_root: Path) -> dict[str, str]:
    return {
        relpath: hashlib.sha256((repo_root / relpath).read_bytes()).hexdigest()
        for relpath in ARTIFACT_RELPATHS
    }


def test_generator_is_byte_deterministic_across_runs(tmp_path):
    first = tmp_path / "run_a"
    second = tmp_path / "run_b"
    first.mkdir()
    second.mkdir()
    _run_generator(first)
    _run_generator(second)
    hashes_a = _artifact_hashes(first)
    hashes_b = _artifact_hashes(second)
    assert hashes_a == hashes_b, "两次运行的产物哈希必须一致（固定种子 20261022）"
    assert hashes_a["data/synthetic/seed_app.json"]
    seed = _load(first / "data" / "synthetic" / "seed_app.json")
    assert seed["seed"] == SEED
    assert seed["generated_at"] == REFERENCE_TIME


def test_committed_artifacts_match_fresh_generation(tmp_path):
    """仓库内已提交的产物必须与当前生成器的一次全新运行一致（未被手工改动）。"""
    fresh_root = tmp_path / "fresh"
    fresh_root.mkdir()
    _run_generator(fresh_root)
    fresh = _artifact_hashes(fresh_root)
    committed = _artifact_hashes(REPO_ROOT)
    for relpath in PATH_INDEPENDENT_ARTIFACTS:
        assert committed[relpath] == fresh[relpath], f"{relpath} 与重新生成结果不一致"
    # 来源清单与索引登记仓库内文档的哈希（随环境存在性变化），
    # 其内部一致性由 test_manifest_hashes_match_files_on_disk 与
    # test_index_artifact_hashes_match_disk_and_dataset_hash 单独断言。
    manifest = _load(fresh_root / "data" / "metadata" / "source_manifest.json")
    assert manifest["declaration"]["all_financial_data_is_synthetic"] is True