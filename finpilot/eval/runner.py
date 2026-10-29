"""评测运行器（EVAL-02、EVAL-04、T53）。

真实链路、独立数据库、逐任务 JSONL 证据
----------------------------------------
- 用 ``finpilot.app.create_app()`` + ``fastapi.testclient.TestClient`` 驱动**真实 HTTP 链路**
  （会话 → 任务 → 计划确认 → 强验证 → 执行 → 恢复），不直接调内部函数伪造结果；
- 每个任务在**独立数据库**（默认 ``finpilot_test_eval``）内运行：重置 schema → 装载
  ``data/synthetic/seed_app.json`` 与 ``task_index.snapshots`` 声明的初态 → 执行 →
  导出最终状态 → 判定。**任务之间必然重建，后一次读不到上一次的订单**；
- 每个配置至少 5 次独立重复（``--repeats``），重复之间同样重建初态；
- 逐任务一行 JSONL 写入 ``eval/results/<config>_<split>_<timestamp>.jsonl``，文件头写
  ``mode``（mock / offline_replay / live）、``config``、``split``、``dataset_hash``、
  ``repeats``、``seed``、``app_mode``。

诚实标注
--------
``--mode mock``（默认）与 ``offline_replay`` 都使用**确定性规则编译器 + 确定性本地提供方**：
这些数字是工程回归信号，**不是在线大模型能力结论**。只有 ``--mode live``（需要
``MODEL_BASE_URL`` 等配置）才是在线模型路径，本机当前没有可用 API key，因此默认不会、
也不应该产生 live 记录。

用法
----
::

    python -m eval.runner --config FULL --split dev --limit 12 --repeats 1
    python -m eval.runner --config FULL --split dev --limit 300 --repeats 5
    python -m eval.runner --list-configs
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import re
import statistics
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "services" / "api" / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from eval import RUNNER_VERSION  # noqa: E402
from eval import baselines as baseline_mod  # noqa: E402
from eval import oracle as oracle_mod  # noqa: E402

DEFAULT_EVAL_DATABASE_URL = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test_eval"
SEED_JSON = REPO_ROOT / "data" / "synthetic" / "seed_app.json"
TASK_INDEX = REPO_ROOT / "eval" / "data" / "task_index.json"
SPLIT_FILES = {
    "dev": (REPO_ROOT / "eval" / "data" / "dev" / "tasks.json", REPO_ROOT / "eval" / "data" / "dev" / "attacks.json"),
    "val": (REPO_ROOT / "eval" / "data" / "val" / "tasks.json", None),
    "hidden": (
        REPO_ROOT / "eval" / "data" / "hidden" / "tasks.json",
        REPO_ROOT / "eval" / "data" / "hidden" / "attacks.json",
    ),
}
RESULTS_DIR = REPO_ROOT / "eval" / "results"

#: 本工作包实现的故障注入类别 → 注入手法（其余类别如实标注为未实现）
FAULT_INJECTION_MAP: dict[str, str] = {
    "pre_submit_network_loss": "provider_unavailable_once",
    "provider_accepted_response_lost": "raise_timeout_once",
    "repeated_query_failure": "query_fail_times",
    "model_rate_limit_bad_schema_sandbox_unavailable": "provider_unavailable_once",
}
FAULT_NOT_IMPLEMENTED: tuple[str, ...] = (
    "duplicate_callback",
    "out_of_order_late_callback",
    "concurrent_fund_reservation_conflict",
    "quote_inventory_change",
    "terms_policy_version_change",
    "process_crash_at_commit",
    "scheduler_missed_window",
    "compensation_refused_or_refund_delay",
)

TASK_TERMINAL_STATUSES = ("succeeded", "partially_succeeded", "failed", "cancelled")
WRITE_TOOLS_HINT = (
    "transfer.submit",
    "schedule.create",
    "merchant.book",
    "wealth.purchase",
    "wealth.redeem",
    "card.report_lost",
)


# ---------------------------------------------------------------------------
# 数据集装载
# ---------------------------------------------------------------------------


def repo_commit() -> str:
    """尽力给出提交号；没有 git 元数据时如实写 ``unknown``（不编造）。"""
    head = REPO_ROOT / ".git" / "HEAD"
    try:
        if head.is_file():
            text = head.read_text(encoding="utf-8").strip()
            if text.startswith("ref:"):
                ref = REPO_ROOT / ".git" / text.split(" ", 1)[1].strip()
                if ref.is_file():
                    return ref.read_text(encoding="utf-8").strip()[:12]
            return text[:12]
    except OSError:
        pass
    return "unknown"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_task_index() -> dict:
    return json.loads(TASK_INDEX.read_text(encoding="utf-8"))


def load_split(split: str, *, limit: int | None = None, seed: int = 20261022) -> list[dict]:
    """载入某个拆分的任务与攻击记录（攻击记录标 ``is_attack``）。"""
    if split not in SPLIT_FILES:
        raise SystemExit(f"未知拆分 {split}（可选：{', '.join(sorted(SPLIT_FILES))}）")
    tasks_path, attacks_path = SPLIT_FILES[split]
    tasks = json.loads(tasks_path.read_text(encoding="utf-8"))
    records = [dict(item, is_attack=False) for item in tasks]
    if attacks_path is not None and attacks_path.is_file():
        attacks = json.loads(attacks_path.read_text(encoding="utf-8"))
        records.extend(dict(item, is_attack=True) for item in attacks)
    records.sort(key=lambda item: str(item.get("task_id") or item.get("attack_id")))
    if limit is not None and limit > 0:
        records = _balanced_limit(records, limit, seed=seed)
    return records


def _balanced_limit(records: list[dict], limit: int, *, seed: int) -> list[dict]:
    """小样本取样：先保证**每个维度都至少取到一条**，再按大桶优先轮转补足。

    ``--limit`` 若只取排序前的 N 条，会退化成"只有 S1 可执行任务"这种偏样本；
    若单纯按桶轮转，"大桶"（例如 S1 可执行 30 条）会在小样本里挤掉全部攻击记录。
    因此分两阶段：

    1. **保底阶段**：按 ``(攻击类 | (场景, 类型))`` 分桶，每桶先取 1 条，
       直到用完 ``limit`` 或所有桶都取过；
    2. **补足阶段**：大桶优先轮转，把剩余名额分配掉。

    这不是"挑好看的结果"：随机种子固定、桶内顺序已打乱，只是保证抽样不偏。
    """
    rng = random.Random(seed)
    buckets: dict[tuple, list[dict]] = {}
    for item in records:
        if item.get("is_attack"):
            key = ("attack", str(item.get("attack_class") or ""))
        else:
            key = (str(item.get("scene") or ""), str(item.get("type") or ""))
        buckets.setdefault(key, []).append(item)
    for bucket in buckets.values():
        rng.shuffle(bucket)

    ordered_keys = sorted(buckets)
    picked: list[dict] = []

    # 阶段 1：每桶保底 1 条（保证攻击类与各场景/类型都出现）
    for key in ordered_keys:
        if len(picked) >= limit:
            break
        if buckets[key]:
            picked.append(buckets[key].pop())

    # 阶段 2：大桶优先轮转，补足剩余名额
    by_size = sorted(ordered_keys, key=lambda key: (-len(buckets[key]), key))
    index = 0
    while len(picked) < limit and any(buckets.values()):
        key = by_size[index % len(by_size)]
        if buckets[key]:
            picked.append(buckets[key].pop())
        index += 1
        if index > limit * max(1, len(by_size)) * 2:
            break

    picked.sort(key=lambda item: str(item.get("task_id") or item.get("attack_id")))
    return picked


def load_seed_dataset() -> dict:
    return json.loads(SEED_JSON.read_text(encoding="utf-8"))


def apply_snapshot_overrides(seed: dict, snapshot_name: str, index: dict) -> dict:
    """按 ``task_index.snapshots`` 声明的初态重建数据（可应用的字段才应用）。

    ``data/synthetic/seed_app.json`` 是冻结的基础状态；``snap_tight`` 额外把工资卡余额
    改为 950000 分（可支配 150000 分）。其余快照的声明字段（持仓、卡控、订阅）在基础
    种子里已经成立，属于**语义标注**，不再重复覆盖——运行器把这一点写进记录的
    ``initial_snapshot.applied/not_applied``，不声称"逐字段应用了快照"。
    """
    data = json.loads(json.dumps(seed))  # 深拷贝，避免污染其他任务
    snapshot = (index.get("snapshots") or {}).get(snapshot_name) or {}
    applied: list[dict] = []
    skipped: list[str] = []

    declared_accounts = snapshot.get("accounts") or {}
    for account in data.get("accounts", []):
        declared = declared_accounts.get(account["account_id"])
        if not declared:
            continue
        if "posted_balance_minor" in declared and int(declared["posted_balance_minor"]) != int(
            account.get("posted_balance_minor", 0)
        ):
            applied.append(
                {
                    "field": f"accounts.{account['account_id']}.posted_balance_minor",
                    "from": int(account.get("posted_balance_minor", 0)),
                    "to": int(declared["posted_balance_minor"]),
                }
            )
            account["posted_balance_minor"] = int(declared["posted_balance_minor"])

    declared_floor = snapshot.get("protected_floor_minor")
    if declared_floor is not None:
        for policy in data.get("protection_policies", []):
            if int(policy.get("protected_floor_minor", 0)) != int(declared_floor):
                applied.append(
                    {
                        "field": f"protection_policies.{policy['policy_id']}.protected_floor_minor",
                        "from": int(policy.get("protected_floor_minor", 0)),
                        "to": int(declared_floor),
                    }
                )
                policy["protected_floor_minor"] = int(declared_floor)

    for key in ("holdings", "cards", "subscriptions", "protection_scope"):
        if key in snapshot:
            skipped.append(key)
    return {
        "snapshot_ref": snapshot_name,
        "description": snapshot.get("description", ""),
        "applied": applied,
        "not_applied_declarative_fields": skipped,
        "note": "基础种子已满足这些声明字段；运行器不重复覆盖，也不声称逐字段应用。",
    }


# ---------------------------------------------------------------------------
# 数据库与适配器
# ---------------------------------------------------------------------------


def resolve_database_url() -> str:
    return os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL") or DEFAULT_EVAL_DATABASE_URL


def _template_database_name(db_name: str) -> str:
    return f"{db_name}__seed_template"


def _pg_connection(db_name: str):
    """连到某库（``postgres`` / 评测库 / 模板库）的独立连接，用于 DDL。"""
    import psycopg

    parts = _parse_db_parts(resolve_database_url())
    return psycopg.connect(
        host=parts["host"],
        port=int(parts["port"]),
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
        connect_timeout=10,
    )


def _parse_db_parts(url: str) -> dict:
    match = re.match(
        r"postgresql(?:\+\w+)?://(?P<user>[^:]+):(?P<password>[^@]+)@(?P<host>[^:/]+):(?P<port>\d+)/(?P<db>.+)",
        url,
    )
    if not match:
        raise SystemExit(f"无法解析数据库 URL：{url}")
    return match.groupdict()


def parse_db_parts(url: str) -> dict:  # 兼容旧名
    return _parse_db_parts(url)


def _terminate_connections(db_names: Iterable[str]) -> None:
    names = [name for name in db_names if name]
    if not names:
        return
    conn = _pg_connection("postgres")
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select pg_terminate_backend(pid) from pg_stat_activity "
                "where datname = any(%s) and pid <> pg_backend_pid()",
                (names,),
            )
    finally:
        conn.close()


def ensure_database(url: str) -> dict:
    """确保评测库存在；不存在则创建（需要 CREATEDB 权限，否则如实报错）。"""
    parts = _parse_db_parts(url)
    import psycopg

    try:
        conn = psycopg.connect(
            host=parts["host"],
            port=int(parts["port"]),
            user=parts["user"],
            password=parts["password"],
            dbname=parts["db"],
            connect_timeout=5,
        )
        conn.close()
        return {"database": parts["db"], "created": False}
    except psycopg.OperationalError:
        pass
    conn = psycopg.connect(
        host=parts["host"],
        port=int(parts["port"]),
        user=parts["user"],
        password=parts["password"],
        dbname="postgres",
        autocommit=True,
        connect_timeout=5,
    )
    with conn.cursor() as cur:
        cur.execute("select 1 from pg_database where datname = %s", (parts["db"],))
        if cur.fetchone() is None:
            cur.execute(f'create database "{parts["db"]}"')
            created = True
        else:
            created = False
    conn.close()
    return {"database": parts["db"], "created": created}


def _install_engine(dbmod: Any, url: str) -> Any:
    """安装全局引擎与会话工厂。

    ``finpilot.db.configure_engine(url)`` 会把 ``_engine`` / ``_session_factory`` 置空后
    **只创建引擎对象而不写回全局**（回调里仅在 ``database_url is None`` 时写回），
    因此运行器不能依赖它——评测必须显式、可核验地控制"当前连的是哪个库"。
    """
    from sqlalchemy.orm import sessionmaker

    engine = dbmod.get_engine(url)
    dbmod._engine = engine
    dbmod._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return engine


def prepare_engine(url: str):
    """把应用全局引擎指向评测库，并重建 schema（每次调用都从空库开始）。"""
    from finpilot import db as dbmod
    from finpilot.models import Base

    os.environ["DATABASE_URL"] = url
    engine = _install_engine(dbmod, url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return engine


def build_seed_template(database_url: str | None = None) -> dict:
    """建立"标准初态"模板库 **一次**，之后每个任务用 ``CREATE DATABASE ... TEMPLATE`` 克隆。

    为什么用模板而不是"清表再灌种子"：
    - ``finpilot.seed.seed_database`` 对 holdings / product_navs / card_controls / aa_shares
      不做存在性判断（这些表没有稳定业务主键），在进程内反复调用会撞唯一约束；
    - 模板克隆在数据库层面复制数据文件，**每任务一个独立库**，天然满足"后一次读不到
      上一次订单"的要求，也避免会话 autoflush 把未提交的种子行写进下一个任务。

    守门检查：模板库必须确实装入了用户与账户，否则后续会出现"登录失败"这种
    看起来像业务失败、实则是环境问题的假阴性。
    """
    from finpilot import seed as seedmod
    from finpilot.models import Base

    url = database_url or resolve_database_url()
    db_name = _parse_db_parts(url)["db"]
    template = _template_database_name(db_name)
    _terminate_connections([template])
    admin = _pg_connection("postgres")
    try:
        with admin.cursor() as cur:
            cur.execute(f'drop database if exists "{template}"')
            cur.execute(f'create database "{template}"')
    finally:
        admin.close()

    template_url = url[: url.rfind("/") + 1] + template
    os.environ["DATABASE_URL"] = template_url
    from finpilot import db as dbmod

    engine = _install_engine(dbmod, template_url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = dbmod.get_session_factory()
    seed = load_seed_dataset()
    with factory() as session:
        from sqlalchemy import func, select

        from finpilot.models import Account, Holding, User

        existing_users = session.execute(select(func.count()).select_from(User)).scalar_one()
        existing_holdings = session.execute(select(func.count()).select_from(Holding)).scalar_one()
        existing_accounts = session.execute(select(func.count()).select_from(Account)).scalar_one()
        if existing_users or existing_holdings or existing_accounts:
            raise RuntimeError(
                f"模板库 {template} 未清空（users={existing_users}, holdings={existing_holdings}, "
                f"accounts={existing_accounts}），拒绝继续，以免跨任务泄漏订单。"
            )
        counts = seedmod.seed_database(session, seed)
        # 守门：模板必须真的有用户与账户，否则后续登录会失败（假阴性）
        users = session.execute(select(func.count()).select_from(User)).scalar_one()
        accounts = session.execute(select(func.count()).select_from(Account)).scalar_one()
        if not users or not accounts:
            raise RuntimeError(
                f"模板库 {template} 装载后仍为空（users={users}, accounts={accounts}）；"
                "很可能是同一评测库正被其他进程清空（例如并发跑测试）。"
                "请用独立的 TEST_DATABASE_URL 或等待其他任务结束。"
            )
    engine.dispose()
    return {"template": template, "counts": counts, "users": users, "accounts": accounts}


def is_template_ready(database_url: str | None = None) -> bool:
    url = database_url or resolve_database_url()
    template = _template_database_name(_parse_db_parts(url)["db"])
    conn = _pg_connection("postgres")
    try:
        with conn.cursor() as cur:
            cur.execute("select 1 from pg_database where datname = %s", (template,))
            return cur.fetchone() is not None
    finally:
        conn.close()


def clone_task_database(database_url: str | None = None) -> str:
    """从模板库克隆出本次任务的独立评测库；返回评测库 URL。"""
    url = database_url or resolve_database_url()
    parts = _parse_db_parts(url)
    db_name = parts["db"]
    template = _template_database_name(db_name)
    _terminate_connections([db_name])
    admin = _pg_connection("postgres")
    try:
        with admin.cursor() as cur:
            cur.execute(f'drop database if exists "{db_name}"')
            cur.execute(f'create database "{db_name}" template "{template}"')
    finally:
        admin.close()
    # 关键：先丢弃旧引擎，避免残留连接指向刚被删除的库
    from finpilot import db as dbmod

    _install_engine(dbmod, url)
    return url


def apply_snapshot_to_database(snapshot_info: dict) -> None:
    """把快照声明里"需要改余额/底线"的字段落到当前任务库（模板克隆之后执行）。"""
    from finpilot import db as dbmod
    from finpilot.models import Account, ProtectionPolicy
    from sqlalchemy import select

    factory = dbmod.get_session_factory()
    with factory() as session:
        for change in snapshot_info.get("applied") or []:
            field = str(change.get("field") or "")
            value = change.get("to")
            if field.startswith("accounts.") and field.endswith(".posted_balance_minor"):
                account_id = field.split(".")[1]
                account = session.get(Account, account_id)
                if account is not None:
                    account.posted_balance_minor = int(value)
            elif field.endswith("protected_floor_minor") and field.startswith("protection_policies."):
                for policy in session.execute(select(ProtectionPolicy)).scalars().all():
                    policy.protected_floor_minor = int(value)
        session.commit()


def prepare_task_database(database_url: str | None = None) -> dict:
    """为**单个任务**准备独立库：模板 → 克隆 → 返回连接信息。

    模板库在套件开始时建立一次；每个任务克隆一份，天然满足
    "后一次运行读不到上一次订单"（V5 19.3 / split_protocol.md §7）。
    """
    url = database_url or resolve_database_url()
    if not is_template_ready(url):
        build_seed_template(url)
    clone_task_database(url)
    return {"database": _parse_db_parts(url)["db"], "template": _template_database_name(_parse_db_parts(url)["db"])}


def install_adapters() -> None:
    """每个任务重置提供方适配器状态，避免故障注入与订单记录跨任务泄漏。"""
    from finpilot.adapters import bank_local
    from finpilot.kernel.provider_adapter import clear_adapters, register_adapter

    clear_adapters()
    bank_local.reset()
    register_adapter(bank_local.build_adapter())
    try:
        from finpilot.adapters.merchant.client import LocalMerchantAdapter

        register_adapter(LocalMerchantAdapter())
    except Exception:  # pragma: no cover - 可选适配器
        pass


def reset_adapters() -> None:
    from finpilot.adapters import bank_local
    from finpilot.kernel.provider_adapter import clear_adapters

    bank_local.reset()
    clear_adapters()


# ---------------------------------------------------------------------------
# 时间与故障注入
# ---------------------------------------------------------------------------


def freeze_clock(reference_iso: datetime | str) -> Any:
    from finpilot.timeutil import Clock, set_clock

    moment = _parse_iso(reference_iso) if isinstance(reference_iso, str) else reference_iso
    clock = Clock(frozen_at=moment.astimezone(timezone.utc))
    set_clock(clock)
    return clock


def reset_clock() -> None:
    from finpilot.timeutil import reset_clock as _reset

    _reset()


def _parse_iso(value: str) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError(f"时间必须带时区: {value}")
    return parsed


def fault_for_task(task_id: str, fault_catalog: Sequence[dict]) -> dict | None:
    for fault in fault_catalog:
        if str(fault.get("target_task_id")) == str(task_id):
            return dict(fault)
    return None


def inject_fault(fault: dict | None) -> dict:
    """按故障类别注入提供方故障；未实现的类别如实记录，不伪造。"""
    if not fault:
        return {"fault_id": None, "class": None, "injected": False, "reason": "no_fault_bound"}
    fault_class = str(fault.get("fault_class") or "")
    technique = FAULT_INJECTION_MAP.get(fault_class)
    if technique is None:
        return {
            "fault_id": fault.get("fault_id"),
            "class": fault_class,
            "injected": False,
            "reason": "injection_not_implemented_in_this_work_package",
            "expected_recoverability": fault.get("expected_recoverability"),
            "allowed_final_states": fault.get("allowed_final_states"),
        }
    from finpilot.adapters import bank_local

    if technique == "provider_unavailable_once":
        bank_local.configure_faults(unavailable=True)
    elif technique == "raise_timeout_once":
        bank_local.configure_faults(raise_timeout=True)
    elif technique == "query_fail_times":
        bank_local.configure_faults(query_fail_times=2)
    return {
        "fault_id": fault.get("fault_id"),
        "class": fault_class,
        "injected": True,
        "technique": technique,
        "expected_recoverability": fault.get("expected_recoverability"),
        "allowed_final_states": fault.get("allowed_final_states"),
    }


def clear_faults() -> None:
    from finpilot.adapters import bank_local

    bank_local.reset()


# ---------------------------------------------------------------------------
# HTTP 驱动
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class TaskRunResult:
    task_id: str | None
    login: dict
    create: dict
    confirmations: list[dict]
    jobs: list[dict]
    replans: list[dict]
    resumes: list[dict]
    extra_confirmation_count: int
    error: str | None = None


def _unwrap(payload: dict | None) -> dict:
    if not isinstance(payload, dict):
        return {}
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


def login(client: Any, user_id: str, password: str, channel: str) -> str:
    response = client.post(
        "/v1/auth/sessions", json={"user_id": user_id, "password": password, "channel": channel}
    )
    body = response.json()
    if body.get("status") != "ok":
        raise RuntimeError(f"登录失败：{body.get('error')}")
    return str(_unwrap(body)["token"])


def _now_iso() -> str:
    from finpilot.timeutil import utcnow

    return utcnow().isoformat()


def create_task(client: Any, headers: dict, *, message: str, channel: str, reference_time: str) -> dict:
    response = client.post(
        "/v1/tasks",
        json={"message": message, "channel": channel, "reference_time": reference_time},
        headers=headers,
    )
    return response.json()


def confirm_plan(client: Any, headers: dict, *, plan_id: str, scope_hash: str, revision: int) -> dict:
    response = client.post(
        f"/v1/plans/{plan_id}/confirm",
        json={"scope_hash": scope_hash, "expected_revision": revision},
        headers=headers,
    )
    return response.json()


def verify_challenge(client: Any, headers: dict, *, challenge_id: str, code: str) -> dict:
    response = client.post(
        f"/v1/auth/challenges/{challenge_id}/verify",
        json={"factor_response": code},
        headers=headers,
    )
    return response.json()


def get_task(client: Any, headers: dict, task_id: str) -> dict:
    return client.get(f"/v1/tasks/{task_id}", headers=headers).json()


def append_message(client: Any, headers: dict, task_id: str, message: str, revision: int) -> dict:
    response = client.post(
        f"/v1/tasks/{task_id}/messages",
        json={"message": message, "expected_revision": revision},
        headers=headers,
    )
    return response.json()


def resume_task(client: Any, headers: dict, task_id: str) -> dict:
    return client.post(f"/v1/tasks/{task_id}/resume", headers=headers).json()


def repair_task(client: Any, headers: dict, task_id: str, event: dict, revision: int) -> dict:
    response = client.post(
        f"/v1/tasks/{task_id}/repair",
        json={"event": event, "expected_revision": revision},
        headers=headers,
    )
    return response.json()


def totp_code_for(user_id: str) -> str:
    """在测试内用**真实验证器流程**生成 TOTP：读取加密秘钥 → 解密 → RFC 6238 计算。"""
    from finpilot import db as dbmod
    from finpilot.kernel.identity import decrypt_secret, totp_at
    from finpilot.models import User
    from finpilot.timeutil import utcnow

    factory = dbmod.get_session_factory()
    with factory() as session:
        user = session.get(User, user_id)
        if user is None or not user.totp_secret_enc:
            raise RuntimeError(f"{user_id} 没有可用的强验证因子")
        secret = decrypt_secret(user.totp_secret_enc)
    code, _counter = totp_at(secret, utcnow())
    return code


def advance_clock(seconds: float) -> Any:
    from finpilot.timeutil import get_clock

    clock = get_clock()
    clock.advance(seconds)
    return clock


def current_now() -> datetime:
    from finpilot.timeutil import utcnow

    return utcnow()


def run_due_jobs_once(*, limit: int = 20) -> dict:
    from finpilot import db as dbmod
    from finpilot.workers.scheduler import run_due_jobs

    factory = dbmod.get_session_factory()
    with factory() as session:
        result = run_due_jobs(session, limit=limit)
        session.commit()
    return result


def pending_jobs() -> list[dict]:
    from finpilot import db as dbmod
    from finpilot.models import ScheduledJob
    from sqlalchemy import select

    factory = dbmod.get_session_factory()
    with factory() as session:
        rows = session.execute(select(ScheduledJob)).scalars().all()
        return [
            {
                "job_id": row.job_id,
                "job_type": row.job_type,
                "state": row.state,
                "due_at": row.due_at.isoformat() if row.due_at else None,
                "window_minutes": row.window_minutes,
                "timezone": row.timezone,
                "grant_id": row.grant_id,
                "operation_id": row.operation_id,
                "task_id": row.task_id,
                "attempts": row.attempts,
            }
            for row in rows
        ]


def drive_scheduled_jobs(*, max_rounds: int = 3, buffer_seconds: float = 30.0) -> list[dict]:
    """推进定时任务：把时钟拨到到期时刻（窗口内）→ run_due_jobs → 还原时钟。"""
    from finpilot.timeutil import get_clock

    outcomes: list[dict] = []
    clock = get_clock()
    original = clock.frozen_at
    try:
        for _ in range(max_rounds):
            rows = pending_jobs()
            due = [row for row in rows if row["state"] in ("scheduled", "leased")]
            if not due:
                break
            target = min(_parse_iso(row["due_at"]) for row in due if row["due_at"])
            clock.freeze(target + timedelta(seconds=buffer_seconds))
            result = run_due_jobs_once()
            outcomes.append({"advanced_to": clock.now().isoformat(), **result})
            if result.get("leased", 0) == 0:
                break
    finally:
        if original is not None:
            clock.freeze(original)
        else:
            clock.unfreeze()
    return outcomes


def drive_task(
    client: Any,
    *,
    record: dict,
    config: baseline_mod.BaselineConfig,
    user_id: str,
    password: str,
    channel: str,
    reference_time: str,
    max_rounds: int = 6,
) -> TaskRunResult:
    """按配置驱动一次任务：建任务 → 授权/强验证 → 定时推进 → 恢复。"""
    token = login(client, user_id, password, channel)
    headers = {"Authorization": f"Bearer {token}"}
    utterance = str(record.get("utterance") or record.get("utterance_or_payload") or "")
    created = create_task(client, headers, message=utterance, channel=channel, reference_time=reference_time)
    data = _unwrap(created)
    task_id = str(data.get("task_id") or "")
    result = TaskRunResult(
        task_id=task_id or None,
        login={"user_id": user_id, "channel": channel, "ok": True},
        create={"status": created.get("status"), "error": created.get("error")},
        confirmations=[],
        jobs=[],
        replans=[],
        resumes=[],
        extra_confirmation_count=0,
    )
    if not task_id:
        result.error = "task_not_created"
        return result

    for _round in range(max_rounds):
        view = _unwrap(get_task(client, headers, task_id))
        status = str(view.get("status") or "")
        if status in TASK_TERMINAL_STATUSES:
            break
        plan = view.get("plan") or {}
        if status in ("awaiting_authorization", "ready") and plan.get("scope_hash"):
            confirmation = {
                "round": _round,
                "plan_id": plan.get("plan_id"),
                "required_level": plan.get("required_level"),
                "scope_hash": plan.get("scope_hash"),
                "mode": config.confirm_mode,
            }
            if config.confirm_mode == "per_step":
                confirmation["note"] = "B0/B1/A3：至少每步单独确认；实现上仍先绑定计划范围"
            body = _unwrap(
                confirm_plan(
                    client,
                    headers,
                    plan_id=str(plan.get("plan_id")),
                    scope_hash=str(plan.get("scope_hash")),
                    revision=int(view.get("revision") or 1),
                )
            )
            confirmation["response_status"] = body.get("status")
            challenge_id = body.get("challenge_id") or (body.get("execution") or {}).get("challenge_id")
            if challenge_id:
                code = totp_code_for(user_id)
                verified = _unwrap(verify_challenge(client, headers, challenge_id=str(challenge_id), code=code))
                confirmation["challenge_id"] = challenge_id
                confirmation["verification"] = (verified.get("verification") or {}).get("status")
                result.extra_confirmation_count += 1
            result.confirmations.append(confirmation)
            continue

        if status == "reconciling":
            result.resumes.append({"round": _round, "reason": "reconciling"})
            resumes = _unwrap(resume_task(client, headers, task_id))
            if (resumes.get("execution") or {}).get("status") in ("no_plan", None):
                result.resumes.append({"round": _round, "note": "无计划可继续，停止"})
                break
            continue

        jobs = drive_scheduled_jobs()
        if jobs:
            result.jobs.extend(jobs)
            continue

        if status == "clarifying":
            clarifying = _maybe_answer_clarification(client, headers, task_id, view, config)
            result.confirmations.append(clarifying)
            if clarifying.get("answered"):
                continue
            break

        if status in ("blocked", "replanning") and config.replan_mode != "none":
            replan = _maybe_replan(client, headers, task_id, view, config)
            result.replans.append(replan)
            if replan.get("repaired"):
                continue
            break

        break

    return result


def _maybe_answer_clarification(
    client: Any, headers: dict, task_id: str, view: dict, config: baseline_mod.BaselineConfig
) -> dict:
    """澄清回答策略：**只回答有结构化选项的澄清**。

    对没有选项的澄清（缺金额、缺时间等），运行器**不代替用户编造答案**——那会污染
    "澄清正确率"的口径。此时任务停在 clarifying，由判定器按澄清类任务判分。
    """
    clarifications = [c for c in (view.get("clarifications") or []) if c.get("blocking") and not c.get("answered")]
    if len(clarifications) != 1:
        return {"answered": False, "reason": "multiple_or_zero_clarifications", "count": len(clarifications)}
    item = clarifications[0]
    options = item.get("options") or []
    if not options:
        return {"answered": False, "reason": "no_options_for_clarification", "field": item.get("field")}
    answer = str(options[0].get("value"))
    body = _unwrap(append_message(client, headers, task_id, answer, int(view.get("revision") or 1)))
    return {
        "answered": True,
        "field": item.get("field"),
        "answer": answer,
        "response_status": body.get("status"),
    }


def _maybe_replan(client: Any, headers: dict, task_id: str, view: dict, config: baseline_mod.BaselineConfig) -> dict:
    """B2/A2：整图重生成（``replan_mode=global``）；FULL：受影响子图增量修复。"""
    event = {"type": "quote_changed", "facts": ["inventory:*", "merchant:*"]}
    body = _unwrap(repair_task(client, headers, task_id, event, int(view.get("revision") or 1)))
    repair = body.get("repair") or {}
    return {
        "repaired": bool(repair),
        "mode": config.replan_mode,
        "affected_nodes": repair.get("affected_nodes"),
        "retained_nodes": repair.get("retained_nodes"),
        "reason": repair.get("reason"),
        "requires_reauthorization": repair.get("requires_reauthorization"),
        "response_status": body.get("status"),
    }


# ---------------------------------------------------------------------------
# 最终状态导出
# ---------------------------------------------------------------------------


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def build_state(task_id: str) -> dict:
    """从数据库导出权威最终状态（判定器与证据的唯一输入）。"""
    from finpilot import db as dbmod
    from finpilot.kernel.limits import daily_usage
    from finpilot.kernel.snapshots import protection_policy
    from finpilot.models import (
        AAShare,
        AARequest,
        Account,
        AuditEvent,
        AuthChallenge,
        Card,
        CardApplication,
        CardControl,
        DailyQuotaHold,
        Grant,
        GrantUse,
        Holding,
        LedgerEntry,
        LedgerTransaction,
        Mandate,
        Operation,
        OperationEvent,
        Payee,
        Plan,
        PlanStep,
        ProductNav,
        ProtectionPolicy,
        Receipt,
        Refund,
        RiskProfile,
        ScheduledJob,
        Subscription,
        Task,
        TaskEvent,
        TaskMessage,
        WealthOrder,
        WealthProduct,
    )
    from sqlalchemy import select

    factory = dbmod.get_session_factory()
    out: dict[str, Any] = {
        "task_id": task_id,
        "runner_version": RUNNER_VERSION,
        "contract": {},
        "domain": {},
    }
    with factory() as session:
        task = session.get(Task, task_id)
        if task is None:
            out["task_status"] = "missing"
            return out
        out["owner_user_id"] = task.owner_user_id
        out["task_status"] = task.status
        out["task_revision"] = task.revision
        out["channel"] = task.channel
        out["mode"] = task.mode
        out["goal_list"] = list((task.goal_spec or {}).get("goals") or [])
        out["clarifications"] = [
            {
                "clarification_id": c.get("clarification_id"),
                "field": c.get("field"),
                "question": c.get("question"),
                "options": c.get("options") or [],
                "blocking": c.get("blocking", True),
                "answered": c.get("answered", False),
            }
            for c in (task.clarifications or [])
        ]
        out["system_claim"] = dict(task.result_summary or {})
        out["system_claim"].setdefault("task_status", task.status)

        policy = protection_policy(session, task.owner_user_id)
        out["protected_floor_minor"] = int(policy.protected_floor_minor) if policy else 0
        out["protection_scope"] = list(policy.account_scope or []) if policy else []

        accounts = session.execute(select(Account)).scalars().all()
        out["accounts"] = [
            {
                "account_id": a.account_id,
                "owner_user_id": a.owner_user_id,
                "posted_balance_minor": a.posted_balance_minor,
                "frozen_other_minor": a.frozen_other_minor,
                "status": a.status,
                "owned": a.owner_user_id == task.owner_user_id,
                "protected_floor_minor": (
                    int(policy.protected_floor_minor)
                    if policy and a.account_id in (policy.account_scope or [])
                    else 0
                ),
            }
            for a in accounts
        ]
        out["initial_accounts"] = [
            {
                "account_id": a.account_id,
                "owner_user_id": a.owner_user_id,
                "posted_balance_minor": _seed_initial_balance(a.account_id),
                "protected_floor_minor": (
                    int(policy.protected_floor_minor)
                    if policy and a.account_id in (policy.account_scope or [])
                    else 0
                ),
            }
            for a in accounts
        ]
        out["initial_protected_floor_minor"] = int(policy.protected_floor_minor) if policy else 0

        entries = session.execute(select(LedgerEntry)).scalars().all()
        txs = session.execute(select(LedgerTransaction)).scalars().all()
        tx_sums: dict[str, int] = {}
        for entry in entries:
            tx_sums[entry.ledger_tx_id] = tx_sums.get(entry.ledger_tx_id, 0) + int(entry.signed_amount_minor)
        out["ledger_transactions"] = [
            {
                "ledger_tx_id": t.ledger_tx_id,
                "operation_id": t.operation_id,
                "task_id": t.task_id,
                "kind": t.kind,
                "sum_minor": tx_sums.get(t.ledger_tx_id, 0),
                "posted_at": _iso(t.posted_at),
            }
            for t in txs
        ]
        out["ledger_entries"] = [
            {
                "entry_id": e.entry_id,
                "ledger_tx_id": e.ledger_tx_id,
                "account_id": e.account_id,
                "signed_amount_minor": e.signed_amount_minor,
                "balance_after_minor": e.balance_after_minor,
            }
            for e in entries
        ]

        operations = session.execute(select(Operation)).scalars().all()
        out["operations"] = [
            {
                "operation_id": op.operation_id,
                "owner_user_id": op.owner_user_id,
                "task_id": op.task_id,
                "plan_id": op.plan_id,
                "plan_revision": op.plan_revision,
                "tool": op.tool,
                "typed_args": op.typed_args,
                "status": op.status,
                "required_level": op.required_level,
                "amount_minor": op.amount_minor,
                "fee_minor": op.fee_minor,
                "source_account_id": op.source_account_id,
                "grant_id": op.grant_id,
                "parameters_hash": op.parameters_hash,
                "effect_confirmed": op.effect_confirmed,
                "error_code": op.error_code,
                "provider_order_ref": op.provider_order_ref,
                "domain_result": op.domain_result or {},
                "business_day": (op.created_at.date().isoformat() if op.created_at else None),
                "created_at": _iso(op.created_at),
                "updated_at": _iso(op.updated_at),
            }
            for op in operations
        ]
        op_events = session.execute(select(OperationEvent)).scalars().all()
        out["operation_events"] = [
            {
                "event_id": e.event_id,
                "operation_id": e.operation_id,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "reason": e.reason,
                "created_at": _iso(e.created_at),
            }
            for e in op_events
        ]
        task_events = session.execute(select(TaskEvent).where(TaskEvent.task_id == task_id)).scalars().all()
        out["task_events"] = [
            {
                "event_id": e.event_id,
                "from_status": e.from_status,
                "to_status": e.to_status,
                "reason": e.reason,
                "created_at": _iso(e.created_at),
            }
            for e in task_events
        ]

        grants = session.execute(select(Grant)).scalars().all()
        out["grants"] = [
            {
                "grant_id": g.grant_id,
                "owner_user_id": g.owner_user_id,
                "task_id": g.task_id,
                "plan_id": g.plan_id,
                "plan_revision": g.plan_revision,
                "scope_kind": g.scope_kind,
                "scope": g.scope,
                "required_level": g.required_level,
                "verification_level": g.verification_level,
                "status": g.status,
                "max_uses": g.max_uses,
                "used_count": g.used_count,
                "max_total_minor": g.max_total_minor,
                "used_total_minor": g.used_total_minor,
                "policy_version": g.policy_version,
                "expires_at": _iso(g.expires_at),
                "revoked_at": _iso(g.revoked_at),
            }
            for g in grants
        ]
        uses = session.execute(select(GrantUse)).scalars().all()
        operation_ids = {op.operation_id: op for op in operations}
        out["grant_uses"] = [
            {
                "grant_use_id": u.grant_use_id,
                "grant_id": u.grant_id,
                "operation_id": u.operation_id,
                "task_id": (
                    operation_ids[u.operation_id].task_id if u.operation_id in operation_ids else None
                ),
                "authorized_parameter_hash": u.authorized_parameter_hash,
                "amount_minor": u.amount_minor,
                "use_status": u.use_status,
                "created_at": _iso(u.created_at),
            }
            for u in uses
        ]

        payees = session.execute(select(Payee)).scalars().all()
        out["payees"] = [
            {"payee_id": p.payee_id, "owner_user_id": p.owner_user_id, "display_name": p.display_name}
            for p in payees
        ]
        out["initial_payees"] = [
            {"payee_id": p.get("payee_id"), "display_name": p.get("display_name")}
            for p in (load_seed_dataset().get("payees") or [])
        ]

        cards = session.execute(select(Card)).scalars().all()
        controls = session.execute(select(CardControl)).scalars().all()
        out["cards"] = [_card_view(card, controls) for card in cards]
        seed = load_seed_dataset()
        seed_card_controls = seed.get("card_controls") or []
        out["initial_cards"] = [
            {
                "card_id": row["card_id"],
                "lifecycle_status": row.get("lifecycle_status"),
                "credit_limit_minor": row.get("credit_limit_minor"),
                "account_id": row.get("account_id"),
                "active_controls": [
                    {
                        "control_type": c.get("control_type"),
                        "channel": c.get("channel"),
                        "limit_minor": c.get("limit_minor"),
                    }
                    for c in seed_card_controls
                    if c.get("card_id") == row["card_id"] and c.get("active", True)
                ],
            }
            for row in seed.get("cards") or []
        ]
        applications = session.execute(select(CardApplication)).scalars().all()
        out["domain"]["card_applications"] = [
            {
                "application_id": a.application_id,
                "owner_user_id": a.owner_user_id,
                "kind": a.kind,
                "card_id": a.card_id,
                "application_status": a.application_status,
                "requested_limit_minor": a.requested_limit_minor,
                "approved_limit_minor": a.approved_limit_minor,
                "operation_id": a.operation_id,
            }
            for a in applications
        ]
        out["initial_card_application_ids"] = [
            row["application_id"] for row in seed.get("card_applications") or []
        ]

        subscriptions = session.execute(select(Subscription)).scalars().all()
        out["subscriptions"] = [
            {
                "subscription_id": s.subscription_id,
                "owner_user_id": s.owner_user_id,
                "merchant_name": s.merchant_name,
                "amount_minor": s.amount_minor,
                "period": s.period,
                "status": s.status,
                "evidence": s.evidence,
            }
            for s in subscriptions
        ]
        out["initial_subscriptions"] = [
            {"subscription_id": row["subscription_id"], "status": row.get("status", "active")}
            for row in seed.get("subscriptions") or []
        ]
        mandates = session.execute(select(Mandate)).scalars().all()
        out["mandates"] = [
            {
                "mandate_id": m.mandate_id,
                "owner_user_id": m.owner_user_id,
                "subscription_id": m.subscription_id,
                "status": m.status,
            }
            for m in mandates
        ]
        out["initial_mandates"] = [
            {"mandate_id": row["mandate_id"], "status": row.get("status", "active")}
            for row in seed.get("mandates") or []
        ]

        aa_requests = session.execute(select(AARequest)).scalars().all()
        aa_shares = session.execute(select(AAShare)).scalars().all()
        out["domain"]["aa_requests"] = [
            {
                "aa_id": a.aa_id,
                "owner_user_id": a.owner_user_id,
                "title": a.title,
                "final_cost_minor": a.final_cost_minor,
                "status": a.status,
                "shares": [
                    {
                        "participant_ref": s.participant_ref,
                        "participant_user_id": s.participant_user_id,
                        "share_minor": s.share_minor,
                        "adjusted_minor": s.adjusted_minor,
                        "status": s.status,
                    }
                    for s in aa_shares
                    if s.aa_id == a.aa_id
                ],
            }
            for a in aa_requests
        ]
        out["initial_aa_ids"] = [row["aa_id"] for row in seed.get("aa_requests") or []]
        out["aa_participant_user_ids"] = sorted(
            {s.participant_user_id for s in aa_shares if s.participant_user_id}
        )

        holdings = session.execute(select(Holding)).scalars().all()
        products = {p.product_id: p for p in session.execute(select(WealthProduct)).scalars().all()}
        out["domain"]["holdings"] = [
            {
                "holding_id": h.holding_id,
                "product_id": h.product_id,
                "confirmed_shares_e8": h.confirmed_shares_e8,
                "frozen_shares_e8": h.frozen_shares_e8,
                "available_shares_e8": h.confirmed_shares_e8 - h.frozen_shares_e8,
            }
            for h in holdings
        ]
        orders = session.execute(select(WealthOrder)).scalars().all()
        out["domain"]["wealth_orders"] = [
            {
                "order_id": o.order_id,
                "operation_id": o.operation_id,
                "product_id": o.product_id,
                "side": o.side,
                "amount_minor": o.amount_minor,
                "fee_minor": o.fee_minor,
                "status": o.status,
                "estimated_shares_e8": o.estimated_shares_e8,
                "confirmed_shares_e8": o.confirmed_shares_e8,
                "accepted_at": _iso(o.accepted_at),
                "confirmed_at": _iso(o.confirmed_at),
                "settled_at": _iso(o.settled_at),
            }
            for o in orders
        ]
        profiles = session.execute(select(RiskProfile)).scalars().all()
        out["domain"]["risk_profiles"] = [
            {
                "profile_id": p.profile_id,
                "questionnaire_version": p.questionnaire_version,
                "risk_level": p.risk_level,
                "expires_at": _iso(p.expires_at),
                "is_active": p.is_active,
            }
            for p in profiles
        ]
        active_profile = next((p for p in profiles if p.is_active), None)
        out["domain"]["active_risk_level"] = active_profile.risk_level if active_profile else None
        navs = session.execute(select(ProductNav)).scalars().all()
        nav_counts: dict[str, int] = {}
        for nav in navs:
            nav_counts[nav.product_id] = nav_counts.get(nav.product_id, 0) + 1
        out["domain"]["product_metrics"] = [
            {
                "product_id": product_id,
                "name": "nav_points",
                "value": count if count >= 60 else "NA",
                "applicable": count >= 60,
                "source": "product_navs",
            }
            for product_id, count in sorted(nav_counts.items())
        ]
        out["domain"]["product_names"] = {pid: p.name for pid, p in products.items()}
        out["domain"]["share_estimates"] = [
            {
                "product_id": product_id,
                "nav_known": nav_counts.get(product_id, 0) > 0,
                "point_estimate": None,
                "range": None,
            }
            for product_id in sorted(products)
        ]

        receipts = session.execute(select(Receipt)).scalars().all()
        out["domain"]["receipts"] = [
            {
                "receipt_id": r.receipt_id,
                "operation_id": r.operation_id,
                "provider_id": r.provider_id,
                "provider_ref": r.provider_ref,
                "source": r.source,
                "verified_status": r.verified_status,
                "state": r.state,
                "payload_hash": r.payload_hash,
            }
            for r in receipts
        ]
        refunds = session.execute(select(Refund)).scalars().all()
        out["domain"]["refunds"] = [
            {
                "refund_id": r.refund_id,
                "operation_id": r.operation_id,
                "original_operation_id": r.original_operation_id,
                "amount_minor": r.amount_minor,
                "status": r.status,
                "settled_at": _iso(r.settled_at),
            }
            for r in refunds
        ]
        holds = session.execute(select(DailyQuotaHold)).scalars().all()
        out["domain"]["quota_holds"] = [
            {
                "operation_id": h.operation_id,
                "business_day": h.business_day.isoformat(),
                "principal_minor": h.principal_minor,
                "state": h.state,
            }
            for h in holds
        ]
        out["domain"]["operation_status_history"] = {
            op.operation_id: [
                e.to_status
                for e in op_events
                if e.operation_id == op.operation_id
            ]
            for op in operations
        }
        challenges = session.execute(select(AuthChallenge)).scalars().all()
        out["domain"]["auth_challenges"] = [
            {
                "challenge_id": c.challenge_id,
                "status": c.status,
                "attempts": c.attempts,
                "verified_at": _iso(c.verified_at),
                "consumed_at": _iso(c.consumed_at),
            }
            for c in challenges
        ]
        messages = session.execute(select(TaskMessage).where(TaskMessage.task_id == task_id)).scalars().all()
        out["domain"]["task_messages"] = [
            {"message_id": m.message_id, "role": m.role, "structured": m.structured} for m in messages
        ]
        out["domain"]["max_operation_attempts"] = max(
            (len(out["domain"]["operation_status_history"].get(op.operation_id, [])) for op in operations),
            default=0,
        )

        audit = session.execute(select(AuditEvent).order_by(AuditEvent.seq)).scalars().all()
        out["audit_events"] = [
            {
                "seq": e.seq,
                "event_id": e.event_id,
                "event_type": e.event_type,
                "actor": e.actor,
                "actor_role": e.actor_role,
                "task_id": e.task_id,
                "operation_id": e.operation_id,
                "grant_id": e.grant_id,
                "policy_version": e.policy_version,
                "payload": e.payload,
            }
            for e in audit
        ]

        plans = session.execute(select(Plan).where(Plan.task_id == task_id).order_by(Plan.revision)).scalars().all()
        steps = session.execute(select(PlanStep)).scalars().all()
        out["plans"] = [
            {
                "plan_id": p.plan_id,
                "revision": p.revision,
                "status": p.status,
                "required_level": p.required_level,
                "scope_hash": p.scope_hash,
                "steps": [
                    {
                        "node_id": s.node_id,
                        "seq": s.seq,
                        "tool": s.tool,
                        "dependencies": s.dependencies,
                        "status": s.status,
                        "required_level": s.required_level,
                        "operation_id": s.operation_id,
                        "last_error": s.last_error,
                    }
                    for s in steps
                    if s.plan_id == p.plan_id
                ],
            }
            for p in plans
        ]
        current = next((p for p in plans if p.plan_id == task.plan_id), None)
        if current is not None:
            out["plan"] = {
                "plan_id": current.plan_id,
                "revision": current.revision,
                "required_level": current.required_level,
                "steps": [
                    {
                        "node_id": s.node_id,
                        "seq": s.seq,
                        "tool": s.tool,
                        "dependencies": s.dependencies,
                        "status": s.status,
                        "required_level": s.required_level,
                    }
                    for s in steps
                    if s.plan_id == current.plan_id
                ],
            }

        jobs = session.execute(select(ScheduledJob)).scalars().all()
        out["jobs"] = [
            {
                "job_id": j.job_id,
                "job_type": j.job_type,
                "state": j.state,
                "due_at": _iso(j.due_at),
                "window_minutes": j.window_minutes,
                "timezone": j.timezone,
                "grant_id": j.grant_id,
                "operation_id": j.operation_id,
                "task_id": j.task_id,
                "attempts": j.attempts,
                "last_error": j.last_error,
            }
            for j in jobs
        ]

        from finpilot.config import get_settings

        settings = get_settings()
        out["contract"] = {
            "transfer_daily_yellow_limit_minor": settings.transfer_daily_yellow_limit_minor,
            "policy_version": "policy-2026.10",
            "app_mode": settings.app_mode,
        }
        out["contract"]["daily_usage"] = daily_usage(
            session,
            _scheduler_ctx(task.owner_user_id),
        )

        out["domain"]["confirmed_inflow_minor"] = sum(
            int(e.signed_amount_minor)
            for e in entries
            if int(e.signed_amount_minor) > 0 and not e.account_id.endswith("clearing")
        )
        out["domain"]["unconfirmed_receivables_minor"] = _unconfirmed_receivables(session, task.owner_user_id)
        out["domain"]["pending_redemption_minor"] = 0
        out["domain"]["initial_spendable_minor"] = _initial_spendable(out)
        out["domain"]["reconciled_operation_ids"] = [
            e.operation_id
            for e in session.execute(select(OperationEvent)).scalars().all()
            if e.reason in ("provider_query_confirmed", "reconciled") and e.operation_id
        ]
        # 只读任务的"产物"：把 bill.report 的领域返回值登记为可核验报告（含 as_of/source_ids），
        # 否则报告类期望只能被判为不可判定。
        out["domain"]["reports"] = _report_artifacts(out["operations"])
    return out


def _report_artifacts(operations: Sequence[dict]) -> list[dict]:
    """从成功的只读操作里提取报告产物（不改写数据，只把已有结构登记出来）。

    ``bill.report`` 的实际返回结构是 ``{as_of, source_ids, consumption:{...}, cashflow:{...},
    period:{...}}``，每个指标是 ``{value, unit, formula, source_ids, status}``。
    运行器只做"结构化登记"，不重新计算任何数字。
    """
    reports: list[dict] = []
    for operation in operations:
        if str(operation.get("status")) != "succeeded":
            continue
        result = operation.get("domain_result") or {}
        if str(result.get("type")) != "bill_report":
            continue
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        rows: list[dict] = []
        metrics: list[dict] = []
        excluded_kinds: list[str] = []
        for section_name in ("consumption", "cashflow"):
            section = data.get(section_name)
            if not isinstance(section, dict):
                continue
            for name, metric in section.items():
                if not isinstance(metric, dict) or "value" not in metric:
                    continue
                values = metric.get("value")
                if not isinstance(values, (int, float)):
                    continue
                rows.append(
                    {
                        "label": f"{section_name}.{name}",
                        "amount_minor": values,
                        "unit": metric.get("unit"),
                        "source_id_count": len(metric.get("source_ids") or []),
                    }
                )
                if metric.get("unit") == "percent":
                    denominator = metric.get("denominator")
                    if denominator is None:
                        denominator = metric.get("denominator_minor")
                    metrics.append({"name": f"{section_name}.{name}", "value": values, "denominator": denominator})
            excluded = section.get("excluded_from_consumption")
            if isinstance(excluded, dict):
                for name, item in excluded.items():
                    if isinstance(item, dict) and item.get("source_ids"):
                        excluded_kinds.append(str(name))
        period = data.get("period") if isinstance(data.get("period"), dict) else {}
        reports.append(
            {
                "kind": str(period.get("kind") or "month"),
                "data_cutoff_at": data.get("as_of") or data.get("data_through"),
                "source_ids": list(data.get("source_ids") or []),
                "rows": rows,
                "metrics": metrics,
                "comparisons": [],
                # 消费口径排除了哪些类别（互转/资产调拨/费用/退款）——用于"互转不计入消费"的判定
                "consumption_excluded_kinds": sorted(set(excluded_kinds)),
                "cashflow_source_kinds": ["internal_transfer", "all"],
                "operation_id": operation.get("operation_id"),
            }
        )
    return reports


def _scheduler_ctx(user_id: str):
    from finpilot.kernel.identity import AuthContext

    return AuthContext(user_id=user_id, roles=("customer",), channel="runner", session_id="runner")


def _seed_initial_balance(account_id: str) -> int:
    seed = load_seed_dataset()
    for row in seed.get("accounts") or []:
        if row["account_id"] == account_id:
            return int(row.get("posted_balance_minor", 0))
    return 0


def _initial_spendable(state: dict) -> int:
    floor = int(state.get("initial_protected_floor_minor") or 0)
    scope = set(state.get("protection_scope") or [])
    total = 0
    for account in state.get("initial_accounts") or []:
        if account.get("owner_user_id") != state.get("owner_user_id"):
            continue
        account_floor = int(account.get("protected_floor_minor") or 0)
        if scope and account["account_id"] not in scope:
            account_floor = 0
        total += max(0, int(account.get("posted_balance_minor") or 0) - account_floor)
    del floor
    return total


def _unconfirmed_receivables(session: Any, user_id: str) -> int:
    from finpilot.models import AAShare, AARequest
    from sqlalchemy import select

    rows = session.execute(
        select(AAShare).join(AARequest, AAShare.aa_id == AARequest.aa_id).where(
            AARequest.owner_user_id == user_id, AAShare.status == "pending"
        )
    ).scalars().all()
    return sum(int(row.share_minor or 0) for row in rows)


def _card_view(card: Any, controls: Sequence[Any]) -> dict:
    return {
        "card_id": card.card_id,
        "owner_user_id": card.owner_user_id,
        "account_id": card.account_id,
        "lifecycle_status": card.lifecycle_status,
        "credit_limit_minor": card.credit_limit_minor,
        "active_controls": [
            {
                "control_type": c.control_type,
                "channel": c.channel,
                "limit_minor": c.limit_minor,
                "created_by_role": c.created_by_role,
            }
            for c in controls
            if c.card_id == card.card_id and c.active
        ],
    }


# ---------------------------------------------------------------------------
# 逐任务记录
# ---------------------------------------------------------------------------


#: 明确"只读任务"的模板族（S2 账单分析、S3 产品比较/适当性查询）。
#: 它们是**真跑**的只读任务，不以"必须产生写效果"为标准判定。
READ_ONLY_TEMPLATE_FAMILIES = frozenset(
    {
        "s2_exec_month_report",
        "s2_exec_annual_report",
        "s2_exec_custom_range",
        "s2_exec_category_breakdown",
        "s2_exec_cashflow_vs_consumption",
        "s3_exec_compare_products",
        "s3_exec_purchase_insufficient_history",
        "s4_exec_application_status",
        "s5_exec_subscription_detail",
    }
)


def expected_for(record: dict, *, budget_minor: int | None = None) -> dict:
    family = str(record.get("template_family") or "")
    return {
        "type": str(record.get("type") or ("attack" if record.get("is_attack") else "executable")),
        "scene": record.get("scene"),
        "template_family": family,
        "acceptable_final_states": list(record.get("acceptable_final_states") or []),
        "required_clarifications": list(record.get("required_clarifications") or []),
        "forbidden_effects": list(record.get("forbidden_effects") or []),
        "judge": record.get("judge"),
        "attack_class": record.get("attack_class"),
        "is_attack": bool(record.get("is_attack")),
        "read_only_task": family in READ_ONLY_TEMPLATE_FAMILIES,
        "budget_minor": budget_minor,
    }


def _budget_from_entities(record: dict) -> int | None:
    entities = record.get("entities") or {}
    for key in ("budget_yuan", "total_budget_yuan"):
        value = entities.get(key)
        if value is not None:
            try:
                return int(round(float(value) * 100))
            except (TypeError, ValueError):
                return None
    return None


def run_one_task(
    client: Any,
    *,
    record: dict,
    config: baseline_mod.BaselineConfig,
    split: str,
    repeat_index: int,
    seed_value: int,
    dataset_hash: str,
    index: dict,
    fault: dict | None,
    utterance_channel: str = "app",
) -> dict:
    """跑一个任务并产出符合 V5 附录 B 字段的逐任务记录。"""
    test_case_id = str(record.get("task_id") or record.get("attack_id"))
    user_id = str(record.get("user_id") or "user_demo_01")
    password = _password_for(user_id)
    snapshot_ref = str(record.get("initial_snapshot_ref") or "snap_base")
    reference_time = str(
        (index.get("snapshots") or {}).get(snapshot_ref, {}).get("reference_time") or index.get("reference_time")
    )
    seed = load_seed_dataset()
    snapshot_info = apply_snapshot_overrides(seed, snapshot_ref, index)
    # 每个任务 = 一个从模板克隆出来的独立库（后一次绝不读到上一次的订单）
    prepare_task_database()
    apply_snapshot_to_database(snapshot_info)
    install_adapters()
    clear_faults()
    fault_info = inject_fault(fault)
    freeze_clock(reference_time)

    started = time.perf_counter()
    drive_error: str | None = None
    drive_result: TaskRunResult | None = None
    try:
        with baseline_mod.apply_switches(config) as switches:
            drive_result = drive_task(
                client,
                record=record,
                config=config,
                user_id=user_id,
                password=password,
                channel=utterance_channel,
                reference_time=reference_time,
            )
    except Exception as exc:  # 真实链路异常必须如实记录，不吞掉
        drive_error = f"{type(exc).__name__}: {exc}"
        switches = {"config": config.name, "error": drive_error}
    finally:
        state = build_state(drive_result.task_id) if (drive_result and drive_result.task_id) else {}
        elapsed_ms = round((time.perf_counter() - started) * 1000, 2)
        reset_clock()
        clear_faults()

    expected = expected_for(record, budget_minor=_budget_from_entities(record))
    judgment = (
        oracle_mod.judge_task({"test_case_id": test_case_id}, expected=expected, state=state)
        if state
        else {
            "oracle_version": oracle_mod.ORACLE_VERSION,
            "test_case_id": test_case_id,
            "outcome": "failed",
            "constraint_violations": [{"effect": "runner_error", "details": [{"error": drive_error}]}],
            "forbidden_effects_triggered": [],
            "blocked_risk_effects": [],
            "reason_codes": ["runner_error"],
            "evidence": {},
            "satisfied": [],
            "unsatisfied": [],
            "not_evaluable": {"constraints": [], "forbidden_effects": []},
            "coverage": {"declared_checks": 0, "evaluable_checks": 0, "coverage_ratio": 1.0},
        }
    )

    return {
        "work_package_id": "D05-D06-C06",
        "requirement_ids": _requirement_ids(record),
        "test_case_id": test_case_id,
        "task_class": expected["type"],
        "scene": record.get("scene"),
        "template_family": record.get("template_family") or record.get("template_group"),
        "split": split,
        "repeat_index": repeat_index,
        "config": config.name,
        "mode": state.get("mode") or os.environ.get("APP_MODE", "mock"),
        "app_mode": os.environ.get("APP_MODE", "mock"),
        "commit": repo_commit(),
        "dataset_hash": dataset_hash,
        "model_version": MODEL_VERSION_OFFLINE,
        "seed": seed_value,
        "task_seed": record.get("seed"),
        "initial_snapshot": {
            **snapshot_info,
            "reference_time": reference_time,
            "accounts": [
                {"account_id": a["account_id"], "posted_balance_minor": a["posted_balance_minor"]}
                for a in (seed.get("accounts") or [])
            ],
        },
        "fault": fault_info,
        "config_switches": switches,
        "expected": expected,
        "actual": {
            "task_status": state.get("task_status"),
            "task_revision": state.get("task_revision"),
            "operation_statuses": [
                {"operation_id": op["operation_id"], "tool": op["tool"], "status": op["status"]}
                for op in state.get("operations") or []
            ],
            "ledger_entry_count": len(state.get("ledger_entries") or []),
            "grant_count": len(state.get("grants") or []),
            "audit_event_count": len(state.get("audit_events") or []),
            "accounts": [
                {
                    "account_id": a["account_id"],
                    "posted_balance_minor": a["posted_balance_minor"],
                    "protected_floor_minor": a.get("protected_floor_minor"),
                }
                for a in state.get("accounts") or []
            ],
        },
        "result": judgment["outcome"],
        "judgment": judgment,
        "evidence_paths": [
            f"db:finpilot_test_eval/tasks/{test_case_id}",
            f"db:finpilot_test_eval/operations/{test_case_id}",
            f"db:finpilot_test_eval/audit_events/{test_case_id}",
            "eval/data/task_index.json",
        ],
        "drive": {
            "error": drive_error,
            "confirmations": list(drive_result.confirmations) if drive_result else [],
            "jobs": list(drive_result.jobs) if drive_result else [],
            "replans": list(drive_result.replans) if drive_result else [],
            "resumes": list(drive_result.resumes) if drive_result else [],
            "extra_confirmation_count": drive_result.extra_confirmation_count if drive_result else 0,
        },
        "timing": {"wall_ms": elapsed_ms},
        "reviewer": "TBD（未复核：本轮为自动生成证据，需人工复核后签署）",
        "review_status": "pending_review",
    }


def _password_for(user_id: str) -> str:
    for row in load_seed_dataset().get("users") or []:
        if row["user_id"] == user_id:
            return str(row.get("password") or "Seed#2026a")
    return "Seed#2026a"


def _requirement_ids(record: dict) -> list[str]:
    scene = str(record.get("scene") or "")
    base = ["EVAL-02", "EVAL-03", "EVAL-04"]
    if record.get("is_attack"):
        base.append("SEC-06")
    if scene == "S6":
        base.append("PLAN-08")
    return base


#: 离线模式的模型标识：如实标注为确定性规则编译器，不是在线模型版本。
MODEL_VERSION_OFFLINE = "rule-compiler-2026.10-+-local-sandbox-provider"


# ---------------------------------------------------------------------------
# 套件运行
# ---------------------------------------------------------------------------


def run_suite(
    *,
    config: str = "FULL",
    split: str = "dev",
    repeats: int = 5,
    seed: int = 20261022,
    limit: int | None = None,
    out_path: str | Path | None = None,
    mode: str = "mock",
) -> dict:
    """跑一个配置 × 拆分的评测套件，返回汇总；逐任务记录写入 JSONL。"""
    config_obj = baseline_mod.get_config(config)
    if mode not in ("mock", "offline_replay", "live"):
        raise SystemExit(f"未知运行模式 {mode}（可选：mock / offline_replay / live）")
    if mode != "mock":
        raise SystemExit("尚无在线模型客户端或冻结轨迹回放引擎；仅配置 URL 不能启用 live。请使用明确标注的 --mode mock。")
    if repeats < 1:
        raise SystemExit("--repeats 必须 ≥ 1（需求要求每个配置至少 5 次独立重复）")

    os.environ["APP_MODE"] = mode
    from finpilot.config import reload_settings

    reload_settings()

    database_url = resolve_database_url()
    db_info = ensure_database(database_url)
    seed_template = build_seed_template(database_url)
    prepare_engine(database_url)

    index = load_task_index()
    dataset_hash = str(index.get("dataset_hash") or "")
    records = load_split(split, limit=limit, seed=seed)
    fault_catalog = json.loads((REPO_ROOT / "eval" / "data" / "fault_catalog.json").read_text(encoding="utf-8"))

    from fastapi.testclient import TestClient

    from finpilot.app import create_app

    out_file = Path(out_path) if out_path else default_out_path(config_obj.name, split)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    header = {
        "record_type": "header",
        "runner_version": RUNNER_VERSION,
        "oracle_version": oracle_mod.ORACLE_VERSION,
        "protocol_version": "eval-protocol-2026.10",
        "mode": mode,
        "config": config_obj.name,
        "config_detail": config_obj.to_dict(),
        "split": split,
        "dataset_hash": dataset_hash,
        "repeats": repeats,
        "seed": seed,
        "app_mode": mode,
        "database": db_info,
        "seed_template": {"name": seed_template["template"], "loaded": seed_template["counts"]},
        "database_url_hint": re.sub(r"://[^@]+@", "://***@", database_url),
        "isolation": (
            "每个任务从模板库克隆出独立评测库（CREATE DATABASE ... TEMPLATE）；"
            "任务之间不存在共享订单、授权或审计记录。"
        ),
        "record_count_planned": len(records) * repeats,
        "model_version": MODEL_VERSION_OFFLINE,
        "commit": repo_commit(),
        "started_at": _now_iso(),
        "offline_disclaimer": (
            "mock/offline_replay 使用确定性规则编译器与确定性本地提供方；"
            "结果只能读作工程回归，不能读作在线模型能力结论。"
        ),
    }

    started = time.perf_counter()
    written = 0
    per_task_judgments: list[dict] = []
    app = create_app()
    with out_file.open("w", encoding="utf-8") as handle:
        handle.write(json.dumps(header, ensure_ascii=False) + "\n")
        for repeat_index in range(repeats):
            for record in records:
                task_id = str(record.get("task_id") or "")
                fault = fault_for_task(task_id, fault_catalog) if task_id else None
                with TestClient(app) as client:
                    run_record = run_one_task(
                        client,
                        record=record,
                        config=config_obj,
                        split=split,
                        repeat_index=repeat_index,
                        seed_value=seed,
                        dataset_hash=dataset_hash,
                        index=index,
                        fault=fault,
                    )
                handle.write(json.dumps(run_record, ensure_ascii=False) + "\n")
                handle.flush()
                written += 1
                per_task_judgments.append(run_record["judgment"])
    elapsed_s = round(time.perf_counter() - started, 2)

    summary = aggregate(run_records_path=out_file, elapsed_seconds=elapsed_s)
    if db_info.get("created"):
        summary["database_created"] = db_info
    summary.update(
        {
            "out_path": str(out_file),
            "mode": mode,
            "config": config_obj.name,
            "split": split,
            "repeats": repeats,
            "records_written": written,
            "tests_planned": len(records) * repeats,
            "dataset_hash": dataset_hash,
        }
    )
    return summary


def default_out_path(config: str, split: str) -> Path:
    stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    return RESULTS_DIR / f"{config}_{split}_{stamp}.jsonl"


def read_records(path: str | Path) -> tuple[dict, list[dict]]:
    header: dict = {}
    records: list[dict] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if payload.get("record_type") == "header":
                header = payload
            else:
                records.append(payload)
    return header, records


def aggregate(*, run_records_path: str | Path, elapsed_seconds: float) -> dict:
    """轻量汇总（完整统计口径见 ``eval.analysis.summarize``）。"""
    header, records = read_records(run_records_path)
    outcomes: dict[str, int] = {}
    forbidden = 0
    by_type: dict[str, dict[str, int]] = {}
    for record in records:
        outcome = str(record.get("result") or "failed")
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        forbidden += len(record["judgment"].get("forbidden_effects_triggered") or [])
        task_class = str(record.get("task_class") or "executable")
        by_type.setdefault(task_class, {})
        by_type[task_class][outcome] = by_type[task_class].get(outcome, 0) + 1
    return {
        "header": header,
        "outcomes": outcomes,
        "by_task_class": by_type,
        "forbidden_effect_count": forbidden,
        "elapsed_seconds": elapsed_seconds,
        "mean_wall_ms": (
            round(statistics.fmean([record["timing"]["wall_ms"] for record in records]), 2)
            if records
            else 0.0
        ),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m eval.runner",
        description="FinPilot 评测运行器（真实 HTTP 链路 + 独立数据库 + 逐任务 JSONL 证据）",
    )
    parser.add_argument("--config", default="FULL", help=f"配置名：{', '.join(baseline_mod.config_names())}")
    parser.add_argument("--split", default="dev", choices=sorted(SPLIT_FILES), help="数据集拆分")
    parser.add_argument("--repeats", type=int, default=5, help="独立重复次数（每个配置至少 5 次）")
    parser.add_argument("--seed", type=int, default=20261022, help="抽样与记录用随机种子")
    parser.add_argument("--limit", type=int, default=None, help="只跑前 N 条（小样本真跑用）")
    parser.add_argument("--out", default=None, help="输出 JSONL 路径（默认 eval/results/<config>_<split>_<时间戳>.jsonl）")
    parser.add_argument("--mode", default="mock", choices=["mock", "offline_replay", "live"], help="运行模式（如实标注）")
    parser.add_argument("--database-url", default=None, help="覆盖评测数据库 URL（默认读 TEST_DATABASE_URL）")
    parser.add_argument("--list-configs", action="store_true", help="列出全部配置与显式开关")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_configs:
        print(json.dumps(baseline_mod.describe_all(), ensure_ascii=False, indent=2))
        return 0
    if args.database_url:
        os.environ["TEST_DATABASE_URL"] = args.database_url
    summary = run_suite(
        config=args.config,
        split=args.split,
        repeats=args.repeats,
        seed=args.seed,
        limit=args.limit,
        out_path=args.out,
        mode=args.mode,
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())