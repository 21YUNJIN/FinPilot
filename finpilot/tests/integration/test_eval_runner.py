"""端到端评测集成测试（EVAL-02/03）。

跑 **1 个真实任务**（不是打桩）：真实 HTTP 链路 → 真实数据库 → 真实账本 → 判定器，
然后逐项断言 V5 附录 B 的记录字段齐全、判定器输出合法。

为什么把评测放进**子进程**
--------------------------
评测运行器会 ``CREATE DATABASE ... TEMPLATE``、并在任务之间重建库；如果与"整仓 pytest"
共用同一个数据库与同一个进程内的全局引擎（``finpilot.db._engine``），就会出现
"别人的测试清表 → 我方登录失败"的假阴性（实测表现为 `UNAUTHENTICATED`）。

因此本文件的每个用例：
1. 用 ``FINPILOT_EVAL_DATABASE_URL`` 指定**独立评测库**（默认在 ``TEST_DATABASE_URL``
   后加 ``__eval`` 后缀）；
2. 在**子进程**里跑运行器（子进程继承环境变量，不污染父进程的全局引擎）；
3. 父进程只读 JSONL 断言。

这样既不删除/修改他人的断言，也不会让评测库与共享测试库互相清表。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

DEFAULT_SHARED_DB = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test_eval"
PYTHON = sys.executable

ORACLE_OUTCOMES = {
    "completed",
    "partially_completed",
    "clarified",
    "refused",
    "partial_success",
    "unknown",
    "failed",
}
REQUIRED_RECORD_FIELDS = (
    "work_package_id",
    "requirement_ids",
    "test_case_id",
    "commit",
    "dataset_hash",
    "model_version",
    "seed",
    "initial_snapshot",
    "expected",
    "actual",
    "result",
    "evidence_paths",
    "reviewer",
)


def _eval_database_url() -> str:
    """独立评测库 URL（与共享测试库分开，避免互相清表）。"""
    explicit = os.environ.get("FINPILOT_EVAL_DATABASE_URL")
    if explicit:
        return explicit
    base = os.environ.get("TEST_DATABASE_URL") or DEFAULT_SHARED_DB
    head, sep, db_name = base.rpartition("/")
    if not sep:
        return DEFAULT_SHARED_DB
    if db_name.endswith("__eval"):
        return base
    return f"{head}/{db_name}__eval"


@pytest.fixture(scope="module")
def eval_database_url() -> str:
    return _eval_database_url()


def _run_in_subprocess(script: str, *, env_extra: dict | None = None) -> str:
    env = dict(os.environ)
    env.update(
        {
            "PYTHONIOENCODING": "utf-8",
            "MPLBACKEND": "Agg",
        }
    )
    if env_extra:
        env.update(env_extra)
    completed = subprocess.run(
        [PYTHON, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    if completed.returncode != 0:
        pytest.fail(
            "评测子进程失败：\nSTDOUT:\n%s\nSTDERR:\n%s"
            % (completed.stdout[-4000:], completed.stderr[-6000:])
        )
    return completed.stdout


def _run_suite_in_subprocess(out_path: Path, *, limit: int, eval_db: str) -> dict:
    script = f"""
import json, sys
sys.path.insert(0, r"{REPO_ROOT}")
from eval import runner
summary = runner.run_suite(
    config="FULL",
    split="dev",
    repeats=1,
    seed=20261022,
    limit={limit},
    out_path=r"{out_path}",
    mode="mock",
)
print(json.dumps(summary, ensure_ascii=False, default=str))
"""
    stdout = _run_in_subprocess(
        script,
        env_extra={"TEST_DATABASE_URL": eval_db, "DATABASE_URL": eval_db, "APP_MODE": "mock"},
    )
    return json.loads(stdout.strip().splitlines()[-1])


def test_eval_runner_runs_one_real_task_and_emits_complete_record(tmp_path, eval_database_url):
    """端到端跑 1 个任务：记录字段齐全 + 判定器输出合法。"""
    from eval import runner as runner_mod

    out_path = tmp_path / "integration_one_task.jsonl"
    summary = _run_suite_in_subprocess(out_path, limit=1, eval_db=eval_database_url)
    assert summary["records_written"] == 1
    assert out_path.is_file()

    header, records = runner_mod.read_records(out_path)
    for key in ("mode", "config", "split", "dataset_hash", "repeats", "seed", "app_mode"):
        assert key in header, f"JSONL 文件头缺少字段 {key}"
    assert header["mode"] == "mock"
    assert header["config"] == "FULL"
    assert header["dataset_hash"]
    assert header["offline_disclaimer"]

    record = records[0]
    for field in REQUIRED_RECORD_FIELDS:
        assert field in record, f"逐任务记录缺少 V5 附录 B 字段：{field}"
    assert record["config"] == "FULL"
    assert record["mode"] == "mock"
    assert record["review_status"] == "pending_review"
    assert record["initial_snapshot"]["snapshot_ref"]

    judgment = record["judgment"]
    assert judgment["oracle_version"] == "oracle-2026.10"
    assert judgment["outcome"] in ORACLE_OUTCOMES
    assert isinstance(judgment["constraint_violations"], list)
    assert isinstance(judgment["forbidden_effects_triggered"], list)
    assert isinstance(judgment["reason_codes"], list) and judgment["reason_codes"]
    assert isinstance(judgment["evidence"], dict)
    assert "system_claim" in judgment["evidence"]
    assert "not_evaluable" in judgment
    assert 0.0 <= judgment["coverage"]["coverage_ratio"] <= 1.0
    assert record["result"] == judgment["outcome"]


def test_eval_runner_record_state_is_verifiable(tmp_path, eval_database_url):
    """证据自洽：记录里带账户余额快照与应急金底线，供人工复核。"""
    from eval import runner as runner_mod

    out_path = tmp_path / "evidence_check.jsonl"
    _run_suite_in_subprocess(out_path, limit=1, eval_db=eval_database_url)
    _header, records = runner_mod.read_records(out_path)
    state = records[0]["actual"]
    assert isinstance(state["accounts"], list) and state["accounts"]
    salary = next((a for a in state["accounts"] if a["account_id"] == "account_salary"), None)
    assert salary is not None
    assert "posted_balance_minor" in salary
    assert salary["protected_floor_minor"] is not None


def test_runner_uses_independent_database_per_task(tmp_path, eval_database_url):
    """同一个基础任务跑两次：第二次的 operation_id 必须全新（没有读到上一次订单）。

    子进程内直接调用 ``run_one_task``（跳过 ``--limit`` 取样），保证确定性地跑同一个任务。
    """
    from eval import runner as runner_mod

    out_path = tmp_path / "repeat_check.json"
    script = f"""
import json, sys
sys.path.insert(0, r"{REPO_ROOT}")
from fastapi.testclient import TestClient
from eval import runner
from finpilot.app import create_app

index = runner.load_task_index()
tasks = runner.load_split("dev", limit=None)
record = next(t for t in tasks if t.get("template_family") == "s1_exec_transfer_by_name")
app = create_app()
runs = []
for _ in range(2):
    with TestClient(app) as client:
        runs.append(
            runner.run_one_task(
                client,
                record=record,
                config=runner.baseline_mod.get_config("FULL"),
                split="dev",
                repeat_index=0,
                seed_value=20261022,
                dataset_hash=str(index.get("dataset_hash")),
                index=index,
                fault=None,
            )
        )
open(r"{out_path}", "w", encoding="utf-8").write(json.dumps(runs, ensure_ascii=False))
print("OK")
"""
    _run_in_subprocess(
        script,
        env_extra={"TEST_DATABASE_URL": eval_database_url, "DATABASE_URL": eval_database_url, "APP_MODE": "mock"},
    )
    runs = json.loads(out_path.read_text(encoding="utf-8"))
    first, second = runs
    first_ops = {op["operation_id"] for op in first["actual"]["operation_statuses"]}
    second_ops = {op["operation_id"] for op in second["actual"]["operation_statuses"]}
    assert first_ops, "转账任务应当产生至少一个操作"
    assert second_ops, "第二次运行同样应当产生操作"
    assert first_ops.isdisjoint(second_ops), "两次运行出现了相同 operation_id，可能读到了上一次的订单"
    for run in runs:
        succeeded = [
            op
            for op in run["actual"]["operation_statuses"]
            if op["tool"] == "transfer.submit" and op["status"] == "succeeded"
        ]
        # 复式记账：每笔成功转账两条分录（付款账户 −x、清算账户 +x）
        assert run["actual"]["ledger_entry_count"] == 2 * len(succeeded)
        assert run["judgment"]["forbidden_effects_triggered"] == []
        assert run["review_status"] == "pending_review"


def test_state_view_can_consume_runner_state():
    """运行器导出的状态必须能直接被判定器的 StateView 消费（同一契约）。"""
    from eval.oracle import StateView

    sample = {
        "task_status": "succeeded",
        "task_id": "task_x",
        "owner_user_id": "user_demo_01",
        "operations": [
            {
                "operation_id": "op_x_transfer",
                "tool": "transfer.submit",
                "status": "succeeded",
                "required_level": "YELLOW",
                "amount_minor": 25000,
                "fee_minor": 0,
                "typed_args": {"payee_id": "payee_lisi"},
                "parameters_hash": "ph",
            }
        ],
        "accounts": [
            {
                "account_id": "account_salary",
                "owner_user_id": "user_demo_01",
                "posted_balance_minor": 875000,
                "protected_floor_minor": 800000,
            }
        ],
        "initial_accounts": [
            {
                "account_id": "account_salary",
                "owner_user_id": "user_demo_01",
                "posted_balance_minor": 900000,
                "protected_floor_minor": 800000,
            }
        ],
        "protected_floor_minor": 800000,
        "protection_scope": ["account_salary"],
        "ledger_entries": [
            {"account_id": "account_salary", "signed_amount_minor": -25000},
            {"account_id": "account_clearing", "signed_amount_minor": 25000},
        ],
    }
    view = StateView(sample)
    assert view.task_status == "succeeded"
    assert len(view.succeeded_effect_operations()) == 1
    assert view.ledger_effects_for_account("account_salary") == -25000
    ok, reason = view.operation_authorized(view.succeeded_effect_operations()[0])
    assert ok is False and reason == "grant_use_missing"
