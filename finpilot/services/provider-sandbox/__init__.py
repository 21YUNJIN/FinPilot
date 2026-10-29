"""独立业务提供方沙箱（V5 SYS-06 / API-04 / S6-03 / T21 / T54）。

目录名含连字符，不能作为常规 Python 包导入，因此本文件只承担三件事：

1. 说明职责边界：这是与 ``services/api``（主银行实现，PostgreSQL）**完全分离**的另一套进程与
   状态存储（stdlib sqlite3），用于验证跨系统异步执行、迟到/乱序回执、幂等与故障恢复，
   而不是"同库函数自调用"。
2. 提供把本目录加入 ``sys.path`` 并按模块名加载 ``store`` / ``faults`` / ``signing`` /
   ``bank`` / ``merchant`` 的辅助函数，供测试与演示脚本复用。
3. 提供同时启动两个服务的命令行入口（``python services/provider-sandbox/__init__.py``）。

单独启动（在仓库根目录）::

    python -m uvicorn bank:app --app-dir services/provider-sandbox --port 58101
    python -m uvicorn merchant:app --app-dir services/provider-sandbox --port 58102

导入用法（测试/脚本）::

    import importlib.util, sys
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("provider_sandbox", Path("services/provider-sandbox/__init__.py"))
    ...
    # 或者更简单：把目录放进 sys.path 后 import bank / import merchant

两服务共享同一签名协议与故障模型，但**各自一份 SQLite**：
``BANK_SANDBOX_STATE`` 与 ``MERCHANT_SANDBOX_STATE`` 必须指向不同文件。
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

__version__ = "1.0.0"
__all__ = [
    "PROVIDER_IDS",
    "PROVIDER_SANDBOX_DIR",
    "SERVICE_PORTS",
    "STATE_ENV_VARS",
    "ensure_sys_path",
    "import_sandbox_module",
    "load_app",
    "main",
    "run_both",
]

PROVIDER_SANDBOX_DIR = Path(__file__).resolve().parent
PROVIDER_IDS = {"bank": "bank_sandbox_v1", "merchant": "merchant_sandbox_v1"}
SERVICE_PORTS = {"bank": 58101, "merchant": 58102}
STATE_ENV_VARS = {"bank": "BANK_SANDBOX_STATE", "merchant": "MERCHANT_SANDBOX_STATE"}


def ensure_sys_path() -> Path:
    """把提供方沙箱目录放到 ``sys.path`` 首位（目录名含连字符，不能按包导入）。"""
    path = str(PROVIDER_SANDBOX_DIR)
    if path not in sys.path:
        sys.path.insert(0, path)
    return PROVIDER_SANDBOX_DIR


def import_sandbox_module(name: str) -> ModuleType:
    """按模块名导入沙箱内部模块（``store`` / ``faults`` / ``signing`` / ``bank`` / ``merchant``）。"""
    allowed = {"store", "faults", "signing", "bank", "merchant"}
    if name not in allowed:
        raise ValueError(f"unknown sandbox module: {name!r}; expected one of {sorted(allowed)}")
    ensure_sys_path()
    return importlib.import_module(name)


def load_app(which: str, **kwargs: Any) -> Any:
    """加载并（可选）构造 FastAPI 应用。

    ``load_app("bank")`` 返回模块级 ``app``；``load_app("bank", state_path=...)`` 用独立状态库新建
    一个应用（测试推荐这样隔离）。
    """
    if which not in PROVIDER_IDS:
        raise ValueError(f"unknown provider: {which!r}; expected 'bank' or 'merchant'")
    module = import_sandbox_module(which)
    if kwargs:
        return module.create_app(**kwargs)
    return module.app


def run_both(host: str = "127.0.0.1") -> None:  # pragma: no cover - 本地演示
    """在后台线程同时启动银行与商户沙箱（两个端口、两个状态库）。"""
    import threading

    import uvicorn

    ensure_sys_path()
    servers = []
    for which, port in SERVICE_PORTS.items():
        module = import_sandbox_module(which)
        server = uvicorn.Server(uvicorn.Config(module.app, host=host, port=port, log_level="info"))
        threading.Thread(target=server.run, name=f"{which}-sandbox", daemon=True).start()
        servers.append((which, port, server))
    print("FinPilot 提供方沙箱已启动：")
    for which, port, _ in servers:
        print(f"  - {which:8s} http://{host}:{port}  ({PROVIDER_IDS[which]})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:  # pragma: no cover
        for _, _, server in servers:
            server.should_exit = True


def main() -> None:  # pragma: no cover - 命令行入口
    run_both()


if __name__ == "__main__":  # pragma: no cover
    main()