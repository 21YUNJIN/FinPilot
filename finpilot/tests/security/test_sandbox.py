"""受限沙箱测试（V5 17.2、SEC-02、T37／T52）。

每个用例都真实拉起沙箱子进程（``sys.executable -I -S``）。合法分析与恶意分析各至少
一条；恶意样例除了断言被阻止，还断言宿主文件系统没有变化。
"""

from __future__ import annotations

import os
import sys

import pytest

from finpilot.config import get_settings
from finpilot.security import sandbox

pytestmark = pytest.mark.security


PAYROLL_DATA = {
    "rows": [
        {"category": "餐饮", "amount_minor": 12000, "merchant": "云栖咖啡"},
        {"category": "餐饮", "amount_minor": 8000, "merchant": "小面馆"},
        {"category": "交通", "amount_minor": 5000, "merchant": "地铁"},
        {"category": "交通", "amount_minor": 15000, "merchant": "打车"},
        {"category": "购物", "amount_minor": 60000, "merchant": "电商"},
    ]
}
# 手算期望：合计 100000 分；均值 20000；中位数 12000；餐饮 20000（占比 20.0%）
EXPECTED_TOTAL = 100_000
EXPECTED_MEAN = 20_000
EXPECTED_MEDIAN = 12_000
EXPECTED_FOOD = 20_000

LEGIT_CODE = """
import statistics
from collections import defaultdict

amounts = [row["amount_minor"] for row in data["rows"]]
total = sum(amounts)
by_category = defaultdict(int)
for row in data["rows"]:
    by_category[row["category"]] += row["amount_minor"]
result = {
    "count": len(amounts),
    "total": total,
    "mean": statistics.mean(amounts),
    "median": statistics.median(amounts),
    "by_category": {key: value for key, value in sorted(by_category.items())},
    "food_share_pct": round(by_category["餐饮"] / total * 100, 2),
}
"""

MALICIOUS_CASES = {
    "read_host_file": "handle = open(r'{host_file}')\nresult = handle.read(32)",
    "write_host_file": "handle = open(r'{host_write}', 'w')\nhandle.write('pwned')\nhandle.close()\nresult = 'written'",
    "import_os": "import os\nresult = os.listdir('.')",
    "import_subprocess": "import subprocess\nresult = subprocess.run([sys.executable, '-c', 'pass'])",
    "import_socket": "import socket\nresult = socket.gethostbyname('example.com')",
    "call_open": "result = open(r'{host_write}', 'w')",
    "eval_expression": "result = eval('1 + 1')",
    "dunder_attribute": "result = ().__class__.__bases__",
    "unbounded_while_true": "while True:\n    pass",
}

EMPTY_EXPECTATIONS = {}


def _policy(**overrides):
    base = sandbox.SandboxPolicy(wall_seconds=15, output_bytes=65536)
    return base.replace(**overrides) if overrides else base


# --- 静态校验 -------------------------------------------------------------


def test_validate_code_accepts_legitimate_analysis():
    assert sandbox.validate_code(LEGIT_CODE) == []


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("import os\nresult = 1", "IMPORT_FORBIDDEN:os"),
        ("import sys\nresult = 1", "IMPORT_FORBIDDEN:sys"),
        ("import subprocess\nresult = 1", "IMPORT_FORBIDDEN:subprocess"),
        ("import socket\nresult = 1", "IMPORT_FORBIDDEN:socket"),
        ("import shutil\nresult = 1", "IMPORT_FORBIDDEN:shutil"),
        ("import pathlib\nresult = 1", "IMPORT_FORBIDDEN:pathlib"),
        ("import tempfile\nresult = 1", "IMPORT_FORBIDDEN:tempfile"),
        ("import threading\nresult = 1", "IMPORT_FORBIDDEN:threading"),
        ("import requests\nresult = 1", "IMPORT_FORBIDDEN:requests"),
        ("from os import path\nresult = 1", "IMPORT_FORBIDDEN:os"),
        ("from . import sibling\nresult = 1", "IMPORT_RELATIVE_FORBIDDEN"),
        ("result = open('x')", "CALL_FORBIDDEN:open"),
        ("result = exec('x = 1')", "CALL_FORBIDDEN:exec"),
        ("result = eval('1')", "CALL_FORBIDDEN:eval"),
        ("result = compile('1', '<s>', 'eval')", "CALL_FORBIDDEN:compile"),
        ("result = __import__('os')", "CALL_FORBIDDEN:__import__"),
        ("result = globals()", "CALL_FORBIDDEN:globals"),
        ("result = locals()", "CALL_FORBIDDEN:locals"),
        ("result = vars()", "CALL_FORBIDDEN:vars"),
        ("result = input()", "CALL_FORBIDDEN:input"),
        ("result = breakpoint()", "CALL_FORBIDDEN:breakpoint"),
        ("result = memoryview(b'x')", "CALL_FORBIDDEN:memoryview"),
        ("result = ().__class__", "DUNDER_ATTRIBUTE_FORBIDDEN:__class__"),
        ("result = result.__globals__", "DUNDER_ATTRIBUTE_FORBIDDEN:__globals__"),
        ("result = __builtins__", "DUNDER_NAME_FORBIDDEN:__builtins__"),
        ("result = '{0.__class__}'.format(1)", "DUNDER_IN_STRING_LITERAL"),
        ("while True:\n    pass", "UNBOUNDED_LOOP"),
        ("import numpy\nresult = 1", "IMPORT_UNAVAILABLE:numpy"),
        ("import pandas\nresult = 1", "IMPORT_UNAVAILABLE:pandas"),
        ("import unknown_module\nresult = 1", "IMPORT_NOT_ALLOWED:unknown_module"),
        ("result = os.listdir('.')", "ATTR_FORBIDDEN:listdir"),
        ("result = data.__class__.__mro__", "DUNDER_ATTRIBUTE_FORBIDDEN:__mro__"),
        ("result = data.__dict__", "DUNDER_ATTRIBUTE_FORBIDDEN:__dict__"),
        # 通过已导入模块的属性绕到 os 也不允许
        ("result = statistics.sys.modules['os'].listdir('.')", "ATTR_FORBIDDEN:listdir"),
    ],
)
def test_validate_code_reports_reason_codes(code, expected):
    reasons = sandbox.validate_code(code)
    assert expected in reasons, f"expected {expected} in {reasons}"


def test_validate_code_rejects_syntax_error_and_oversized_code():
    assert sandbox.validate_code("def broken(:\n")[0].startswith("SYNTAX_ERROR")
    assert sandbox.validate_code("x = 1\n" * 20000)[0].startswith("CODE_TOO_LARGE")


# --- 合法分析 -------------------------------------------------------------


def test_run_analysis_returns_correct_statistics():
    outcome = sandbox.run_analysis(LEGIT_CODE, PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_OK, outcome
    result = outcome["result"]
    assert result["count"] == 5
    assert result["total"] == EXPECTED_TOTAL
    assert result["mean"] == EXPECTED_MEAN
    assert result["median"] == EXPECTED_MEDIAN
    assert result["by_category"] == {"交通": 20000, "购物": 60000, "餐饮": 20000}
    assert result["food_share_pct"] == 20.0

    metrics = outcome["resources"]
    assert metrics["exit_code"] == 0
    assert metrics["truncated"] is False
    assert "ast_validation" in metrics["limits_enforced"]
    assert "audit_hook" in metrics["limits_enforced"]
    assert "wall_timeout" in metrics["limits_enforced"]
    assert metrics["limits_declared"]["wall_seconds"] == 15
    assert outcome["policy_version"] == sandbox.POLICY_VERSION
    assert outcome["code_hash"]
    if os.name != "posix":
        # 如实标注 Windows 未生效的硬限额，不得声称与容器等价
        assert "cpu_rlimit" in metrics["limits_not_enforced"]
        assert "memory_rlimit" in metrics["limits_not_enforced"]
        assert metrics["platform"] == "nt"
    assert any("audit hook" in note for note in outcome["notes"])


def test_run_analysis_takes_last_expression_as_result():
    outcome = sandbox.run_analysis("import math\nmath.factorial(6)", PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_OK
    assert outcome["result"] == 720


def test_run_analysis_reports_user_code_error():
    outcome = sandbox.run_analysis("result = 1 / 0", PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_ERROR
    assert outcome["error"]["code"] == "ANALYSIS_ERROR"
    assert outcome["error"]["type"] == "ZeroDivisionError"


def test_run_analysis_captures_stdout_with_limit():
    outcome = sandbox.run_analysis("print('分析开始')\nresult = 1", PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_OK
    assert "分析开始" in outcome["stdout"]


# --- 恶意分析：静态与运行时拦截 -------------------------------------------


def test_run_analysis_blocks_static_violations(tmp_path):
    host_write = (tmp_path / "static-violation.txt").as_posix()
    code = MALICIOUS_CASES["write_host_file"].format(host_write=host_write, host_file=host_write)
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_VIOLATION
    assert "CALL_FORBIDDEN:open" in outcome["blocked"]
    assert outcome["engine"] == "ast-validator"
    assert not (tmp_path / "static-violation.txt").exists()


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    [
        ("import_os", "IMPORT_FORBIDDEN:os"),
        ("import_subprocess", "IMPORT_FORBIDDEN:subprocess"),
        ("import_socket", "IMPORT_FORBIDDEN:socket"),
        ("call_open", "CALL_FORBIDDEN:open"),
        ("eval_expression", "CALL_FORBIDDEN:eval"),
        ("dunder_attribute", "DUNDER_ATTRIBUTE_FORBIDDEN:__class__"),
        ("unbounded_while_true", "UNBOUNDED_LOOP"),
    ],
)
def test_run_analysis_blocks_malicious_samples(case, expected_reason, tmp_path):
    host_write = (tmp_path / f"{case}.txt").as_posix()
    host_file = (tmp_path / "host-source.txt").as_posix()
    (tmp_path / "host-source.txt").write_text("host-secret", encoding="utf-8")
    code = MALICIOUS_CASES[case].format(host_write=host_write, host_file=host_file)
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, _policy())
    assert outcome["status"] == sandbox.STATUS_VIOLATION
    assert expected_reason in outcome["blocked"]
    assert outcome["result"] is None
    assert not (tmp_path / f"{case}.txt").exists()
    assert (tmp_path / "host-source.txt").read_text(encoding="utf-8") == "host-secret"


def test_run_analysis_times_out_on_unbounded_loop():
    # 静态分析无法证明该循环不终止（x 恒 > 0），必须由运行时墙钟兜底
    code = "x = 1\nwhile x > 0:\n    x = x + 0\n"
    assert sandbox.validate_code(code) == []
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, _policy(wall_seconds=2))
    assert outcome["status"] == sandbox.STATUS_TIMEOUT
    assert "WALL_TIMEOUT" in outcome["blocked"]
    assert outcome["resources"]["wall_ms"] >= 1500


def test_run_analysis_blocks_output_explosion():
    code = "for _ in range(200000):\n    print('x' * 100)\nresult = 1\n"
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, _policy(output_bytes=4096))
    assert outcome["status"] == sandbox.STATUS_VIOLATION
    assert "OUTPUT_LIMIT_EXCEEDED" in outcome["blocked"]
    assert outcome["resources"]["truncated"] is True
    assert outcome["resources"]["output_bytes"] <= 4096


def test_run_analysis_blocks_oversized_result():
    code = "result = {'blob': 'y' * 50000}"
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, _policy(output_bytes=8192))
    assert outcome["status"] == sandbox.STATUS_OK
    assert outcome["resources"]["result_truncated"] is True
    assert outcome["result"]["_truncated"] is True


def test_blocked_attempts_are_recorded(tmp_path):
    sandbox.clear_blocked_attempts()
    host_write = (tmp_path / "recorded.txt").as_posix()
    code = MALICIOUS_CASES["write_host_file"].format(host_write=host_write, host_file=host_write)
    sandbox.run_analysis(code, PAYROLL_DATA, _policy())

    attempts = sandbox.blocked_attempts()
    assert attempts, "恶意分析必须留下被阻止记录（审计/告警用）"
    latest = attempts[-1]
    assert latest["source"].startswith("run_analysis")
    assert "CALL_FORBIDDEN:open" in latest["reason_codes"]
    assert latest["code_hash"]
    assert latest["policy_version"] == sandbox.POLICY_VERSION


# --- 不可用：失败关闭，绝不回退宿主执行 ------------------------------------


def test_run_analysis_unavailable_when_disabled_and_host_untouched(tmp_path):
    target = tmp_path / "disabled-policy-should-not-exist.txt"
    code = MALICIOUS_CASES["write_host_file"].format(host_write=target.as_posix(), host_file=target.as_posix())

    # 对照组：同一段样例在测试进程里执行确实会写文件（证明样例本身有效，不是空跑）
    control = tmp_path / "control-written-by-test.txt"
    with open(control, "w", encoding="utf-8") as handle:
        handle.write("pwned")
    assert control.exists()

    sandbox.clear_blocked_attempts()
    outcome = sandbox.run_analysis(code, PAYROLL_DATA, sandbox.SandboxPolicy(enabled=False))
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE
    assert outcome["blocked"] == ["SANDBOX_DISABLED"]
    assert outcome["engine"] == "unavailable"
    assert outcome["result"] is None
    assert outcome["error"]["code"] == "SANDBOX_UNAVAILABLE"
    assert not target.exists(), "沙箱禁用时宿主绝不能执行生成代码"
    assert any(item["reason_codes"] == ["SANDBOX_DISABLED"] for item in sandbox.blocked_attempts())


def test_run_analysis_unavailable_on_policy_load_failure(tmp_path):
    missing = tmp_path / "no-such-policy.yaml"
    outcome = sandbox.run_analysis("result = 1", PAYROLL_DATA, missing)
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE
    assert outcome["blocked"] == ["POLICY_LOAD_FAILED:FileNotFoundError"]

    broken = tmp_path / "broken-policy.yaml"
    broken.write_text("limits: []\n", encoding="utf-8")
    outcome = sandbox.run_analysis("result = 1", PAYROLL_DATA, broken)
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE
    assert outcome["blocked"][0].startswith("POLICY_LOAD_FAILED")

    outcome = sandbox.run_analysis("result = 1", PAYROLL_DATA, sandbox.SandboxPolicy(wall_seconds=0))
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE
    assert "POLICY_INVALID:wall_seconds" in outcome["blocked"]


def test_run_analysis_unavailable_when_settings_loader_fails(monkeypatch):
    def _boom():
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(sandbox.SandboxPolicy, "from_settings", classmethod(lambda cls: _boom()))
    outcome = sandbox.run_analysis("result = 1", PAYROLL_DATA)
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE
    assert outcome["blocked"] == ["POLICY_LOAD_FAILED:RuntimeError"]


def test_child_environment_is_minimized(tmp_path):
    env = sandbox._child_env(str(tmp_path))
    assert "DATABASE_URL" not in env
    assert "TEST_DATABASE_URL" not in env
    assert "MODEL_API_KEY" not in env
    assert "AUDIT_SIGNING_KEY" not in env
    assert "TOTP_ENCRYPTION_KEY" not in env
    assert env["TEMP"] == str(tmp_path)
    assert env["PYTHONDONTWRITEBYTECODE"] == "1"


# --- 运行时 hook 自检（沙箱内真实探测） ------------------------------------


def test_sandbox_selfcheck_confirms_runtime_hook():
    outcome = sandbox.sandbox_selfcheck(_policy())
    assert outcome["status"] == sandbox.STATUS_OK, outcome
    assert outcome["audit_hook_active"] is True
    assert outcome["unexpected_success"] == []
    probes = {item["name"]: item for item in outcome["probes"]}
    assert set(probes) == {
        "host_file_read",
        "workspace_file_write",
        "network_connect",
        "subprocess_spawn",
        "dynamic_import_forbidden",
        "os_listdir",
    }
    for name, item in probes.items():
        assert item["blocked"] is True, f"{name} 未被运行时 hook 拦截：{item}"
        assert item["code"]
    assert probes["host_file_read"]["code"].startswith("OPEN_FORBIDDEN")
    assert probes["workspace_file_write"]["code"].startswith("OPEN_WRITE_FORBIDDEN")
    assert probes["network_connect"]["code"].startswith("NETWORK_FORBIDDEN")
    assert probes["subprocess_spawn"]["code"].startswith("SUBPROCESS_FORBIDDEN")
    # 自检关闭字节码写入，确保导入确实被导入/文件访问控制拦截，而非偶然被缓存目录创建拦截。
    assert probes["dynamic_import_forbidden"]["code"].startswith(("IMPORT_FORBIDDEN", "FILE_FORBIDDEN"))
    assert probes["dynamic_import_forbidden"].get("outcome") is None
    assert probes["os_listdir"]["code"].startswith("FILE_FORBIDDEN")


def test_sandbox_selfcheck_unavailable_when_disabled():
    outcome = sandbox.sandbox_selfcheck(sandbox.SandboxPolicy(enabled=False))
    assert outcome["status"] == sandbox.STATUS_UNAVAILABLE


# --- 策略 -----------------------------------------------------------------


def test_sandbox_policy_defaults_follow_settings():
    settings = get_settings()
    policy = sandbox.SandboxPolicy.from_settings()
    assert policy.enabled is bool(settings.sandbox_enabled)
    assert policy.cpu_seconds == settings.sandbox_cpu_seconds == 5
    assert policy.wall_seconds == settings.sandbox_wall_seconds == 10
    assert policy.memory_mb == settings.sandbox_memory_mb == 512
    assert policy.output_bytes == settings.sandbox_output_bytes == 1_048_576
    assert policy.max_processes == settings.sandbox_max_processes == 32
    assert policy.workspace_mb == 128
    assert policy.policy_version == sandbox.POLICY_VERSION


def test_deploy_policy_file_matches_module_defaults():
    policy = sandbox.SandboxPolicy.from_file()
    assert policy.enabled is True
    assert policy.cpu_seconds == 5
    assert policy.wall_seconds == 10
    assert policy.memory_mb == 512
    assert policy.output_bytes == 1_048_576
    assert policy.max_processes == 32
    assert policy.workspace_mb == 128
    policy.validate()


# --- 受限 DSL（沙箱不可用时的兜底） ----------------------------------------


def test_run_dsl_computes_personal_reports_without_sandbox():
    # 沙箱不可用（禁用）时的降级路径
    assert sandbox.run_analysis("result = 1", PAYROLL_DATA, sandbox.SandboxPolicy(enabled=False))["status"] == "unavailable"

    assert sandbox.run_dsl("sum(amount_minor)", PAYROLL_DATA)["result"] == EXPECTED_TOTAL
    assert sandbox.run_dsl("count()", PAYROLL_DATA)["result"] == 5
    assert sandbox.run_dsl("avg(amount_minor)", PAYROLL_DATA)["result"] == float(EXPECTED_MEAN)
    assert sandbox.run_dsl("min(amount_minor)", PAYROLL_DATA)["result"] == 5000
    assert sandbox.run_dsl("max(amount_minor)", PAYROLL_DATA)["result"] == 60000
    # 手算：升序 [5000, 8000, 12000, 15000, 60000]，rank = 0.9 * 4 = 3.6
    #       15000 + (60000 - 15000) * 0.6 = 42000
    assert sandbox.run_dsl("pct(amount_minor, 90)", PAYROLL_DATA)["result"] == 42000.0
    grouped = sandbox.run_dsl("group_by(category, sum(amount_minor))", PAYROLL_DATA)
    assert grouped["result"] == [
        {"key": "交通", "value": 20000},
        {"key": "购物", "value": 60000},
        {"key": "餐饮", "value": 20000},
    ]
    counted = sandbox.run_dsl("group_by(category, count())", PAYROLL_DATA)
    assert counted["result"] == [{"key": "交通", "value": 2}, {"key": "购物", "value": 1}, {"key": "餐饮", "value": 2}]
    assert sandbox.run_dsl("sum(amounts)", {"amounts": [100, 250, 300]})["result"] == 650
    # 非数值按 NULL 语义跳过："3" 可解析，None 跳过
    assert sandbox.run_dsl("sum(amount)", {"rows": [{"amount": 1}, {"amount": 2}, {"amount": "3"}, {"amount": None}]})["result"] == 6
    assert sandbox.run_dsl("group_by(merchant, max(amount_minor))", PAYROLL_DATA)["result"] == [
        {"key": "云栖咖啡", "value": 12000},
        {"key": "地铁", "value": 5000},
        {"key": "小面馆", "value": 8000},
        {"key": "打车", "value": 15000},
        {"key": "电商", "value": 60000},
    ]


@pytest.mark.parametrize(
    ("expression", "reason"),
    [
        ("", "DSL_EMPTY"),
        ("drop table", "DSL_UNKNOWN_FUNCTION:drop"),
        ("__import__('os')", "DSL_UNKNOWN_FUNCTION:__import__"),
        ("sum(amount_minor", "DSL_SYNTAX"),
        ("sum(1)", "DSL_ARGS"),
        ("pct(amount_minor, 101)", "DSL_PERCENTILE_RANGE"),
        ("sum(a); import os", "DSL_SYNTAX"),
        ("x = 1", "DSL_SYNTAX"),
    ],
)
def test_run_dsl_rejects_unsupported_expressions(expression, reason):
    outcome = sandbox.run_dsl(expression, PAYROLL_DATA)
    assert outcome["status"] == "error"
    assert reason in outcome["reason_codes"]
    assert outcome["result"] is None


def test_run_dsl_is_not_a_code_execution_channel():
    # DSL 不是 eval：赋值、导入、属性访问、函数定义都不可用
    for expression in ("os.system('x')", "result = 1", "[x for x in data]", "lambda: 1", "print(1)"):
        outcome = sandbox.run_dsl(expression, PAYROLL_DATA)
        assert outcome["status"] == "error", expression
        assert outcome["result"] is None


# --- runner 源码与解释器一致性 --------------------------------------------


def test_runner_source_is_self_contained_and_executable(tmp_path):
    runner = tmp_path / "runner.py"
    source = sandbox.runner_source()
    runner.write_text(source, encoding="utf-8")
    import py_compile

    py_compile.compile(str(runner), doraise=True, cfile=str(tmp_path / "runner.pyc"))
    # runner 必须自包含：以 -I -S 运行时不能依赖 finpilot 包或父进程上下文
    assert "import finpilot" not in source
    assert "from finpilot" not in source
    assert "sys.addaudithook" in source
    assert "setrlimit" in source
    assert sys.version_info >= (3, 11)
