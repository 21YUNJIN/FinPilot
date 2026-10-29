"""容器隔离配置回归：compose 的隔离声明不得被后续改动悄悄削弱。

静态校验只能证明"配置写了"，不能证明"运行时生效"；运行时验证需要在有容器运行时的
Linux 主机上执行（见 deploy/README.md），本仓库开发机没有 docker/WSL 发行版。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
VERIFIER = REPO_ROOT / "deploy" / "verify_isolation.py"
COMPOSE = REPO_ROOT / "deploy" / "compose.yaml"


def _load_verifier():
    spec = importlib.util.spec_from_file_location("finpilot_verify_isolation", VERIFIER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_compose_isolated_configuration_passes():
    verifier = _load_verifier()
    results = verifier.verify(COMPOSE)
    failures = [item for item in results if not item["ok"]]
    assert results, "校验器没有返回任何检查项"
    assert not failures, "隔离配置不满足声明：" + "; ".join(f"{item['check']}={item['detail']}" for item in failures)


def test_verifier_rejects_a_missing_hardening_block(tmp_path):
    verifier = _load_verifier()
    weakened = COMPOSE.read_text(encoding="utf-8").replace("  cap_drop:\n    - ALL\n", "", 1)
    candidate = tmp_path / "compose.yaml"
    candidate.write_text(weakened, encoding="utf-8")
    results = verifier.verify(candidate)
    assert any(item["check"].endswith("cap_drop_all") and not item["ok"] for item in results), (
        "移除 cap_drop 后校验器应当报错"
    )