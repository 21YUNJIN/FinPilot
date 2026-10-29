"""受限分析沙箱：AST 白名单校验 + 独立子进程 + 运行时 audit hook + 资源限额（V5 17.2、SEC-02、T37／T52）。

隔离模型（本机实现，跨平台）
----------------------------
1. **父进程 AST 白名单**（:func:`validate_code`）：拒绝危险导入、危险调用、dunder 属性
   访问、常量真值 ``while`` 循环；给出可读原因码（如 ``IMPORT_FORBIDDEN:os``）。
2. **独立子进程**：``sys.executable -I -S <runner>``，工作目录为一次性临时目录，
   环境变量最小化（无 DATABASE_URL、无模型密钥、无 HOME），输入经 stdin JSON 传入，
   结果经 stdout 返回并按 ``output_bytes`` 截断。
3. **运行时 audit hook**（子进程内 ``sys.addaudithook``）：拦截网络（socket.*）、
   子进程创建（subprocess.Popen／_winapi.CreateProcess／os.system 等）、文件写入与
   标准库目录之外的文件打开、非白名单模块的运行时导入、os.* 危险操作。
4. **POSIX 资源硬限额**：``resource.setrlimit`` 施加 ``RLIMIT_CPU``／``RLIMIT_AS``／
   ``RLIMIT_NPROC``／``RLIMIT_FSIZE``。
5. **Windows 兜底（如实标注）**：没有 ``resource`` 模块，CPU／内存／进程数硬限额
   **未生效**；由父进程墙钟超时 kill、输出字节上限、AST 校验与 audit hook 兜底。
   结果里 ``resources.limits_enforced`` 与 ``resources.limits_not_enforced`` 会如实列出
   当前平台真正生效与未生效的项，贡献报告不得把"尽力而为"写成"硬隔离"。
6. **不可用即关闭**：策略被禁用、策略加载失败、子进程无法启动时一律返回
   ``status="unavailable"``，**绝不回退到宿主进程 exec／eval**；此时个性化报表计算改走
   :func:`run_dsl`（无 I/O、无变量赋值的受限 DSL）。

诚实的差距
----------
* audit hook 是 **Python 层**拦截，不是内核级 seccomp／容器隔离：同一用户下并发运行的
  其它进程不受约束；绕过 CPython 审计事件的原生扩展调用（例如自带 C 扩展直接 open(2)）
  不会被本 hook 拦截。
* 本机 **没有 Docker**，因此 ``deploy/sandbox-policy.yaml`` 里的 Linux 容器策略
  （非 root、只读根、cap-drop ALL、无网络、seccomp……）在本环境**未被验证执行**，
  只是参考配置；本模块实现的是"子进程 + AST + audit hook + 墙钟/输出限额"这一较弱但
  可运行的等价物。二者差距见该 YAML 的 ``windows_dev_equivalence`` 段。
* 沙箱只应接收已去标识的必要数据（调用方用
  ``security.trust_boundary.sanitize_for_model`` 处理）；本模块不替调用方脱敏。
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .. import ids
from ..config import REPO_ROOT, get_settings
from ..errors import FinPilotError
from ..timeutil import utcnow

logger = logging.getLogger(__name__)

POLICY_VERSION = "sandbox-2026.10"
"""沙箱策略版本；容器策略文件（deploy/sandbox-policy.yaml）与本模块共用同一版本号。"""

DEFAULT_POLICY_FILE = "deploy/sandbox-policy.yaml"

STATUS_OK = "ok"
STATUS_VIOLATION = "violation"
STATUS_TIMEOUT = "timeout"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"

# --- 导入白名单／黑名单 ---------------------------------------------------

ALLOWED_IMPORTS: frozenset[str] = frozenset(
    {
        "math",
        "statistics",
        "decimal",
        "json",
        "datetime",
        "itertools",
        "functools",
        "collections",
        "re",
        "random",
    }
)

OPTIONAL_IMPORTS: frozenset[str] = frozenset({"numpy", "pandas"})
"""若运行环境可用则允许（本项目 venv 未安装，且 ``-S`` 不加载 site-packages，
因此当前环境下会被判为不可用并给出 ``IMPORT_UNAVAILABLE``）。"""

FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "os",
        "sys",
        "socket",
        "subprocess",
        "ctypes",
        "importlib",
        "shutil",
        "pathlib",
        "tempfile",
        "requests",
        "http",
        "multiprocessing",
        "threading",
        "urllib",
        "ssl",
        "asyncio",
        "signal",
        "resource",
        "gc",
        "builtins",
        "marshal",
        "pickle",
        "shelve",
        "sqlite3",
        "dbm",
        "mmap",
        "winreg",
        "msvcrt",
        "pty",
        "tty",
        "fcntl",
        "pwd",
        "grp",
        "platform",
        "sysconfig",
        "site",
        "code",
        "codeop",
        "webbrowser",
        "tkinter",
        "concurrent",
        "select",
        "selectors",
        "fileinput",
        "glob",
        "fnmatch",
        "zipfile",
        "tarfile",
        "gzip",
        "bz2",
        "lzma",
        "configparser",
        "logging",
        "warnings",
        "ctypes.util",
    }
)

FORBIDDEN_CALLS: frozenset[str] = frozenset(
    {
        "open",
        "exec",
        "eval",
        "compile",
        "__import__",
        "globals",
        "locals",
        "vars",
        "input",
        "breakpoint",
        "memoryview",
        "getattr",
        "setattr",
        "delattr",
        "exit",
        "quit",
        "help",
        "reload",
        "execfile",
        "compileall",
        "classmethod",
        "staticmethod",
        "super",
    }
)

FORBIDDEN_ATTRS: frozenset[str] = frozenset(
    {
        # 进程／系统
        "system",
        "popen",
        "fork",
        "forkpty",
        "spawn",
        "spawnl",
        "spawnv",
        "spawnve",
        "execv",
        "execve",
        "execl",
        "execlp",
        "execvp",
        "kill",
        "killpg",
        "syscall",
        "startfile",
        # 文件系统
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "mkdir",
        "makedirs",
        "rename",
        "renames",
        "replace",
        "chmod",
        "chown",
        "walk",
        "scandir",
        "listdir",
        "glob",
        "iglob",
        "read_text",
        "write_text",
        "read_bytes",
        "write_bytes",
        "open",
        "to_csv",
        "to_excel",
        "to_pickle",
        "to_parquet",
        "to_hdf",
        "read_csv",
        "read_excel",
        "read_pickle",
        "read_parquet",
        "read_hdf",
        "savetxt",
        "loadtxt",
        "savez",
        "save",
        "load",
        "dump",
        # 网络
        "urlopen",
        "socket",
        "connect",
        "bind",
        "listen",
        "accept",
        "send",
        "sendall",
        "recv",
        "recvfrom",
        "sendto",
    }
)

_UNBOUNDED_LOOP_REASON = "UNBOUNDED_LOOP"
_MAX_CODE_CHARS = 64_000
_MAX_AST_NODES = 5_000
_MAX_REASON_LIST = 24

_BLOCKED_ATTEMPTS: list[dict[str, Any]] = []
_MAX_BLOCKED_ATTEMPTS = 200


# --- 策略 -----------------------------------------------------------------


@dataclass(frozen=True)
class SandboxPolicy:
    """沙箱策略（默认取 ``settings.sandbox_*``）。"""

    enabled: bool = True
    cpu_seconds: int = 5
    wall_seconds: int = 10
    memory_mb: int = 512
    output_bytes: int = 1_048_576
    max_processes: int = 32
    workspace_mb: int = 128
    policy_version: str = POLICY_VERSION

    @classmethod
    def from_settings(cls) -> "SandboxPolicy":
        settings = get_settings()
        return cls(
            enabled=bool(settings.sandbox_enabled),
            cpu_seconds=int(settings.sandbox_cpu_seconds),
            wall_seconds=int(settings.sandbox_wall_seconds),
            memory_mb=int(settings.sandbox_memory_mb),
            output_bytes=int(settings.sandbox_output_bytes),
            max_processes=int(settings.sandbox_max_processes),
        )

    @classmethod
    def from_file(cls, path: str | os.PathLike[str] | None = None) -> "SandboxPolicy":
        """从 ``deploy/sandbox-policy.yaml`` 读取限额；文件缺失或结构错误时抛异常。

        抛异常是刻意的：调用方（或 :func:`run_analysis`）必须把它转成
        ``status="unavailable"``，而不是"用默认值继续跑"。
        """
        target = Path(path) if path is not None else Path(REPO_ROOT) / DEFAULT_POLICY_FILE
        if not target.is_file():
            raise FileNotFoundError(f"沙箱策略文件不存在: {target}")
        import yaml  # 局部导入：仅在读取策略文件时需要

        data = yaml.safe_load(target.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("沙箱策略文件必须是 YAML 映射")
        limits = data.get("limits")
        if not isinstance(limits, dict):
            raise ValueError("沙箱策略缺少 limits 段")
        enabled = data.get("enabled", True)
        return cls(
            enabled=bool(enabled),
            cpu_seconds=int(limits.get("cpu_seconds", 5)),
            wall_seconds=int(limits.get("wall_seconds", 10)),
            memory_mb=int(limits.get("memory_mb", 512)),
            output_bytes=int(limits.get("output_bytes", 1_048_576)),
            max_processes=int(limits.get("max_processes", 32)),
            workspace_mb=int(limits.get("workspace_mb", 128)),
            policy_version=str(data.get("version") or POLICY_VERSION),
        )

    def replace(self, **changes: Any) -> "SandboxPolicy":
        return replace(self, **changes)

    def validate(self) -> None:
        """限额必须为正且量级合理；否则视为策略无效（调用方返回 unavailable）。"""
        problems: list[str] = []
        for name in ("cpu_seconds", "wall_seconds", "memory_mb", "output_bytes", "max_processes", "workspace_mb"):
            value = getattr(self, name)
            if not isinstance(value, int) or value <= 0:
                problems.append(f"POLICY_INVALID:{name}")
        if self.output_bytes > 64 * 1_048_576:
            problems.append("POLICY_INVALID:output_bytes_too_large")
        if self.memory_mb > 8192:
            problems.append("POLICY_INVALID:memory_mb_too_large")
        if problems:
            raise FinPilotError(
                "SANDBOX_UNAVAILABLE",
                message="沙箱策略限额无效，已关闭代码执行。",
                details={"reason_codes": problems},
            )

    def child_payload(self) -> dict[str, Any]:
        return {
            "cpu_seconds": self.cpu_seconds,
            "wall_seconds": self.wall_seconds,
            "memory_mb": self.memory_mb,
            "output_bytes": self.output_bytes,
            "max_processes": self.max_processes,
            "workspace_mb": self.workspace_mb,
            "policy_version": self.policy_version,
        }

    def as_dict(self) -> dict[str, Any]:
        return {"enabled": self.enabled, **self.child_payload()}


@functools.lru_cache(maxsize=8)
def optional_module_available(name: str) -> bool:
    """用与子进程相同的解释器参数探测可选模块（numpy／pandas）是否可用。"""
    if name not in OPTIONAL_IMPORTS:
        return False
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-S", "-c", f"import {name}"],
            capture_output=True,
            timeout=30,
            env=_child_env(None),
        )
        return proc.returncode == 0
    except Exception:  # pragma: no cover - 探测失败按不可用处理
        return False


def allowed_imports() -> frozenset[str]:
    extra = {name for name in OPTIONAL_IMPORTS if optional_module_available(name)}
    return frozenset(ALLOWED_IMPORTS | extra)


# --- AST 校验 -------------------------------------------------------------


def _root_module(name: str | None) -> str:
    return (name or "").split(".")[0]


def _is_dunder(name: str) -> bool:
    return len(name) > 4 and name.startswith("__") and name.endswith("__")


_DANGEROUS_DUNDERS = (
    "__class__",
    "__globals__",
    "__builtins__",
    "__subclasses__",
    "__mro__",
    "__bases__",
    "__dict__",
    "__getattribute__",
    "__code__",
    "__loader__",
    "__import__",
    "__reduce__",
    "__reduce_ex__",
    "__init_subclass__",
    "__set_name__",
)


def _dunder_in_string(value: str) -> bool:
    """字符串常量里出现危险 dunder 时拒绝（``"{0.__class__}".format(x)`` 这类绕过）。"""
    return any(token in value for token in _DANGEROUS_DUNDERS)


def _constant_true(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return bool(node.value)
    if isinstance(node, (ast.List, ast.Tuple, ast.Set, ast.Dict)):
        return bool(getattr(node, "elts", None) or getattr(node, "keys", None))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return not _constant_true(node.operand)
    return False


def validate_code(code: str) -> list[str]:
    """AST 白名单校验，返回违规原因列表（空列表表示通过）。

    拒绝：危险 ``import``（os／sys／socket／subprocess／ctypes／importlib／shutil／
    pathlib／tempfile／requests／http／multiprocessing／threading 等）、危险调用
    （``open``／``exec``／``eval``／``compile``／``__import__``／``globals``／``locals``／
    ``vars``／``input``／``breakpoint``／``memoryview`` 等）、dunder 属性与 dunder 名字、
    危险属性（``system``／``popen``／``remove``／``to_csv``／``urlopen`` 等）、常量真值的
    ``while`` 无界循环。语法错误、超长代码、过多节点也会被拒绝。

    常量真值检测只是"能静态识别的那部分"：``while x > 0`` 这类无法静态判定的死循环由
    运行时墙钟超时兜底（见 :func:`run_analysis` 的 ``status="timeout"``）。
    """
    reasons: list[str] = []
    if not isinstance(code, str):
        return ["CODE_NOT_STRING"]
    if len(code) > _MAX_CODE_CHARS:
        return [f"CODE_TOO_LARGE:{len(code)}"]
    try:
        tree = ast.parse(code, filename="<analysis>", mode="exec")
    except SyntaxError as exc:
        return [f"SYNTAX_ERROR:{exc.msg}@{exc.lineno}"]

    node_count = sum(1 for _ in ast.walk(tree))
    if node_count > _MAX_AST_NODES:
        reasons.append(f"CODE_TOO_COMPLEX:{node_count}")

    available = allowed_imports()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = _root_module(alias.name)
                if root in FORBIDDEN_IMPORTS:
                    reasons.append(f"IMPORT_FORBIDDEN:{root}")
                elif root not in available:
                    if root in OPTIONAL_IMPORTS:
                        reasons.append(f"IMPORT_UNAVAILABLE:{root}")
                    else:
                        reasons.append(f"IMPORT_NOT_ALLOWED:{root}")
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                reasons.append("IMPORT_RELATIVE_FORBIDDEN")
                continue
            root = _root_module(node.module)
            if root in FORBIDDEN_IMPORTS:
                reasons.append(f"IMPORT_FORBIDDEN:{root}")
            elif root not in available:
                if root in OPTIONAL_IMPORTS:
                    reasons.append(f"IMPORT_UNAVAILABLE:{root}")
                else:
                    reasons.append(f"IMPORT_NOT_ALLOWED:{root}")
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in FORBIDDEN_CALLS:
                reasons.append(f"CALL_FORBIDDEN:{func.id}")
            elif isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_CALLS:
                reasons.append(f"CALL_FORBIDDEN:{func.attr}")
        if isinstance(node, ast.Attribute):
            if _is_dunder(node.attr):
                reasons.append(f"DUNDER_ATTRIBUTE_FORBIDDEN:{node.attr}")
            elif node.attr in FORBIDDEN_ATTRS:
                reasons.append(f"ATTR_FORBIDDEN:{node.attr}")
        if isinstance(node, ast.Name) and _is_dunder(node.id):
            reasons.append(f"DUNDER_NAME_FORBIDDEN:{node.id}")
        if isinstance(node, ast.While) and _constant_true(node.test):
            reasons.append(_UNBOUNDED_LOOP_REASON)
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            reasons.append("SCOPE_DIRECTIVE_FORBIDDEN")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and _dunder_in_string(node.value):
            # 反例："{0.__class__}".format(x) 能绕过属性节点检查读到 dunder，这里直接拒绝
            reasons.append("DUNDER_IN_STRING_LITERAL")

    seen: set[str] = set()
    unique: list[str] = []
    for reason in reasons:
        if reason in seen:
            continue
        seen.add(reason)
        unique.append(reason)
    return unique[:_MAX_REASON_LIST]


# --- 被阻止尝试的记录 ------------------------------------------------------


def record_blocked_attempt(
    *,
    source: str,
    reason_codes: Sequence[str],
    code_hash: str = "",
    detail: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """记录一次被阻止的沙箱尝试（审计／告警用），返回记录本身。

    资源耗尽、越界、网络与文件探测都必须留下可检索的证据（V5 17.2）。
    调用方可用返回的记录写审计事件（``append_event(event_type="sandbox.blocked", ...)``）。
    """
    record = {
        "at": utcnow().isoformat(timespec="seconds"),
        "source": source,
        "reason_codes": list(reason_codes)[:_MAX_REASON_LIST],
        "code_hash": code_hash,
        "detail": dict(detail or {}),
        "policy_version": POLICY_VERSION,
    }
    _BLOCKED_ATTEMPTS.append(record)
    if len(_BLOCKED_ATTEMPTS) > _MAX_BLOCKED_ATTEMPTS:
        del _BLOCKED_ATTEMPTS[: len(_BLOCKED_ATTEMPTS) - _MAX_BLOCKED_ATTEMPTS]
    logger.warning("沙箱阻止尝试：source=%s reasons=%s", source, record["reason_codes"])
    return record


def blocked_attempts() -> list[dict[str, Any]]:
    """最近被阻止的沙箱尝试（进程内环形缓冲，供告警／测试／运营视图使用）。"""
    return list(_BLOCKED_ATTEMPTS)


def clear_blocked_attempts() -> None:
    _BLOCKED_ATTEMPTS.clear()


# --- 子进程环境与 runner ---------------------------------------------------

_RUNNER_SOURCE = r'''
"""FinPilot 受限分析沙箱 runner（独立子进程；仅标准库；由 sandbox.py 写入临时目录）。"""
import ast
import json
import sys

# 诊断输出统一 UTF-8（-I 会忽略 PYTHONIOENCODING，否则中文 traceback 会被 locale 编码破坏）
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


class _Blocked(Exception):
    def __init__(self, code):
        Exception.__init__(self, code)
        self.code = code


class _OutputLimit(Exception):
    pass


class _CappedWriter:
    """限长 stdout：超限抛 _OutputLimit（阻断输出爆炸），不占用无界内存。"""

    def __init__(self, limit):
        self.limit = limit
        self.size = 0
        self.parts = []
        self.truncated = False

    def write(self, text):
        if not isinstance(text, str):
            text = str(text)
        remaining = self.limit - self.size
        if remaining <= 0:
            self.truncated = True
            raise _OutputLimit("output_limit_exceeded")
        if len(text) > remaining:
            self.parts.append(text[:remaining])
            self.size = self.limit
            self.truncated = True
            raise _OutputLimit("output_limit_exceeded")
        self.parts.append(text)
        self.size += len(text)
        return len(text)

    def flush(self):
        return None

    def isatty(self):
        return False

    def writelines(self, lines):
        for line in lines:
            self.write(line)

    def getvalue(self):
        return "".join(self.parts)


_STATE = {"limit": 1048576, "stdio_roots": (), "blocked_extra": []}

_BLOCKED_EVENTS = {
    "socket.__new__": "NETWORK_FORBIDDEN:socket",
    "socket.connect": "NETWORK_FORBIDDEN:connect",
    "socket.bind": "NETWORK_FORBIDDEN:bind",
    "socket.getaddrinfo": "NETWORK_FORBIDDEN:getaddrinfo",
    "socket.gethostbyname": "NETWORK_FORBIDDEN:gethostbyname",
    "socket.gethostbyaddr": "NETWORK_FORBIDDEN:gethostbyaddr",
    "socket.sethostname": "NETWORK_FORBIDDEN:sethostname",
    "socket.sendto": "NETWORK_FORBIDDEN:sendto",
    "subprocess.Popen": "SUBPROCESS_FORBIDDEN:Popen",
    "_winapi.CreateProcess": "SUBPROCESS_FORBIDDEN:CreateProcess",
    "os.system": "SUBPROCESS_FORBIDDEN:os.system",
    "os.exec": "SUBPROCESS_FORBIDDEN:os.exec",
    "os.spawn": "SUBPROCESS_FORBIDDEN:os.spawn",
    "os.posix_spawn": "SUBPROCESS_FORBIDDEN:posix_spawn",
    "os.fork": "SUBPROCESS_FORBIDDEN:fork",
    "os.forkpty": "SUBPROCESS_FORBIDDEN:forkpty",
    "os.kill": "PROCESS_FORBIDDEN:kill",
    "os.killpg": "PROCESS_FORBIDDEN:killpg",
    "ctypes.dlopen": "NATIVE_FORBIDDEN:dlopen",
    "ctypes.dlsym": "NATIVE_FORBIDDEN:dlsym",
    "ctypes.call_function": "NATIVE_FORBIDDEN:call_function",
    "ctypes.addressof": "NATIVE_FORBIDDEN:addressof",
    "mmap.__new__": "MEMORY_FORBIDDEN:mmap",
    "sqlite3.connect": "FILE_FORBIDDEN:sqlite3",
    "winreg.OpenKey": "REGISTRY_FORBIDDEN:OpenKey",
    "shutil.copyfile": "FILE_WRITE_FORBIDDEN:copyfile",
    "shutil.move": "FILE_WRITE_FORBIDDEN:move",
    "shutil.rmtree": "FILE_WRITE_FORBIDDEN:rmtree",
    "tempfile.mkstemp": "FILE_WRITE_FORBIDDEN:mkstemp",
    "tempfile.mkdtemp": "FILE_WRITE_FORBIDDEN:mkdtemp",
    "webbrowser.open": "NETWORK_FORBIDDEN:webbrowser",
    "ftplib.connect": "NETWORK_FORBIDDEN:ftp",
    "smtplib.connect": "NETWORK_FORBIDDEN:smtp",
    "urllib.Request": "NETWORK_FORBIDDEN:urllib",
    "_socket.socket": "NETWORK_FORBIDDEN:_socket",
    "os.putenv": "ENV_FORBIDDEN:putenv",
    "os.unsetenv": "ENV_FORBIDDEN:unsetenv",
    "os.chdir": "FILE_FORBIDDEN:chdir",
    "os.listdir": "FILE_FORBIDDEN:listdir",
    "os.scandir": "FILE_FORBIDDEN:scandir",
    "os.remove": "FILE_WRITE_FORBIDDEN:remove",
    "os.rename": "FILE_WRITE_FORBIDDEN:rename",
    "os.mkdir": "FILE_WRITE_FORBIDDEN:mkdir",
    "os.rmdir": "FILE_WRITE_FORBIDDEN:rmdir",
    "os.chmod": "FILE_WRITE_FORBIDDEN:chmod",
    "os.chown": "FILE_WRITE_FORBIDDEN:chown",
    "os.link": "FILE_WRITE_FORBIDDEN:link",
    "os.symlink": "FILE_WRITE_FORBIDDEN:symlink",
    "os.truncate": "FILE_WRITE_FORBIDDEN:truncate",
    "os.utime": "FILE_WRITE_FORBIDDEN:utime",
    "os.startfile": "SUBPROCESS_FORBIDDEN:startfile",
}


def _safe_roots():
    import os

    roots = set()
    for entry in list(sys.path):
        if not entry:
            continue
        try:
            real = os.path.realpath(entry)
        except Exception:
            continue
        if real and os.path.isdir(real):
            roots.add(real)
    for prefix in (getattr(sys, "prefix", ""), getattr(sys, "base_prefix", ""), getattr(sys, "exec_prefix", "")):
        if prefix:
            try:
                roots.add(os.path.realpath(prefix))
            except Exception:
                continue
    return tuple(roots)


def _short(path, limit=96):
    text = str(path)
    if len(text) > limit:
        text = "..." + text[-limit:]
    return text


def _check_open(path, mode):
    import os

    if path is None:
        return
    if isinstance(path, int):
        if path in (0, 1, 2) and not (mode and any(flag in str(mode) for flag in ("w", "a", "x", "+"))):
            return
        raise _Blocked("OPEN_FD_FORBIDDEN:" + str(path))
    text = str(path)
    if text in ("<stdin>", "<stdout>", "<stderr>"):
        return
    if mode and any(flag in str(mode) for flag in ("w", "a", "x", "+")):
        raise _Blocked("OPEN_WRITE_FORBIDDEN:" + _short(text))
    try:
        real = os.path.realpath(text)
    except Exception:
        raise _Blocked("OPEN_FORBIDDEN:" + _short(text))
    for root in _STATE["stdio_roots"]:
        if real == root or real.startswith(root + os.sep):
            return
    raise _Blocked("OPEN_FORBIDDEN:" + _short(text))


_PREIMPORT_SAFE = (
    "math",
    "statistics",
    "decimal",
    "json",
    "datetime",
    "itertools",
    "functools",
    "collections",
    "collections.abc",
    "re",
    "random",
    "numpy",
    "pandas",
)


def _preimport(allowed_imports):
    """在安装 hook 之前预加载白名单模块及其传递依赖。

    这样用户代码里的 ``import math`` 命中 ``sys.modules`` 缓存、不再触发文件打开与
    ``import`` 审计事件；而"运行时导入未在白名单且尚未加载的模块"仍会被 hook 拦下。
    """
    loaded = []
    for name in _PREIMPORT_SAFE:
        root = name.split(".")[0]
        if root not in allowed_imports:
            continue
        try:
            __import__(name)
            loaded.append(name)
        except BaseException:
            continue
    return loaded


def _install_hook(allowed_imports):
    def _hook(event, args):
        if event == "open":
            path = args[0] if args else None
            mode = args[1] if len(args) > 1 else None
            _check_open(path, mode)
            return
        if event == "import":
            name = str(args[0]) if args else ""
            root = name.split(".")[0]
            if root and root not in allowed_imports and root not in sys.modules:
                raise _Blocked("IMPORT_FORBIDDEN:" + root)
            return
        code = _BLOCKED_EVENTS.get(event)
        if code is not None:
            raise _Blocked(code)
        if event.startswith("os."):
            raise _Blocked("OS_EVENT_FORBIDDEN:" + event)
        if event.startswith("shutil."):
            raise _Blocked("FILE_WRITE_FORBIDDEN:" + event)
        if event.startswith("ctypes."):
            raise _Blocked("NATIVE_FORBIDDEN:" + event)

    sys.addaudithook(_hook)


def _apply_rlimits(policy):
    applied = []
    try:
        import resource
    except Exception:
        return applied

    def _set(name, value):
        try:
            resource.setrlimit(getattr(resource, name), value)
            applied.append(name)
        except Exception:
            return

    cpu = int(policy.get("cpu_seconds") or 0)
    if cpu > 0:
        _set("RLIMIT_CPU", (cpu, cpu + 1))
    memory_mb = int(policy.get("memory_mb") or 0)
    if memory_mb > 0:
        _set("RLIMIT_AS", (memory_mb * 1048576, memory_mb * 1048576))
    processes = int(policy.get("max_processes") or 0)
    if processes > 0:
        _set("RLIMIT_NPROC", (processes, processes))
    _set("RLIMIT_FSIZE", (0, 0))
    _set("RLIMIT_CORE", (0, 0))
    return applied


def _wrap_result(tree):
    """若用户代码没有给 result 赋值，把最后一条表达式语句作为结果。"""
    body = list(tree.body)
    if body and isinstance(body[-1], ast.Expr):
        assigns = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "result":
                        assigns = True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == "result":
                assigns = True
        if not assigns:
            last = body[-1]
            new_node = ast.Assign(targets=[ast.Name(id="result", ctx=ast.Store())], value=last.value)
            ast.copy_location(new_node, last)
            ast.fix_missing_locations(new_node)
            tree.body = body[:-1] + [new_node]
    return tree


def _analyze(payload, allowed_imports, policy):
    import traceback

    code = payload.get("code") or ""
    data = payload.get("data")
    if data is None:
        data = {}
    limit = int(policy.get("output_bytes") or 1048576)
    _STATE["limit"] = limit
    _STATE["stdio_roots"] = _safe_roots()
    applied = _apply_rlimits(policy)
    preimported = _preimport(allowed_imports)
    _install_hook(allowed_imports)

    namespace = {"__name__": "__finpilot_sandbox__", "data": data, "result": None}
    report = {
        "status": "ok",
        "result": None,
        "blocked": [],
        "error": None,
        "stdout": "",
        "stdout_truncated": False,
        "limits_applied": applied,
        "preimported": preimported,
    }
    try:
        tree = ast.parse(code, filename="<analysis>", mode="exec")
        tree = _wrap_result(tree)
        code_object = compile(tree, "<analysis>", "exec")
    except SyntaxError as exc:
        report["status"] = "error"
        report["error"] = {"code": "SYNTAX_ERROR", "message": str(exc)[:300]}
        return report

    writer = _CappedWriter(limit)
    real_stdout = sys.stdout
    sys.stdout = writer
    try:
        exec(code_object, namespace)
    except _Blocked as exc:
        report["status"] = "violation"
        report["blocked"] = [exc.code]
        report["error"] = {"code": exc.code}
    except _OutputLimit:
        report["status"] = "violation"
        report["blocked"] = ["OUTPUT_LIMIT_EXCEEDED"]
        report["error"] = {"code": "OUTPUT_LIMIT_EXCEEDED", "message": "分析输出超过 output_bytes 上限"}
    except BaseException as exc:
        report["status"] = "error"
        try:
            # linecache 会尝试打开源码文件，可能被 hook 拦下；诊断信息失败不影响结论
            traceback_tail = traceback.format_exc()[-600:]
        except BaseException:
            traceback_tail = ""
        report["error"] = {
            "code": "ANALYSIS_ERROR",
            "type": type(exc).__name__,
            "message": str(exc)[:300],
            "traceback_tail": traceback_tail,
        }
    finally:
        sys.stdout = real_stdout

    report["stdout"] = writer.getvalue()
    report["stdout_truncated"] = writer.truncated
    if report["status"] == "ok":
        result = namespace.get("result")
        try:
            json.dumps(result)
            report["result"] = result
        except (TypeError, ValueError):
            try:
                report["result"] = json.loads(json.dumps(result, default=str))
            except (TypeError, ValueError) as exc:
                report["status"] = "error"
                report["error"] = {"code": "RESULT_NOT_SERIALIZABLE", "message": str(exc)[:200]}
    return report


def _selfcheck(policy, allowed_imports):
    """在沙箱内实际验证运行时拦截是否生效（供 sandbox_selfcheck 使用）。"""
    import importlib
    import os
    import socket
    import subprocess

    # Exercise import restrictions themselves rather than accidentally blocking
    # creation of a missing __pycache__ directory before the import is checked.
    sys.dont_write_bytecode = True

    limit = int(policy.get("output_bytes") or 1048576)
    _STATE["limit"] = limit
    _STATE["stdio_roots"] = _safe_roots()
    applied = _apply_rlimits(policy)
    _preimport(allowed_imports)
    _install_hook(allowed_imports)

    probes = []

    def _probe(name, fn):
        try:
            outcome = fn()
            probes.append({"name": name, "blocked": False, "outcome": outcome or "succeeded"})
        except _Blocked as exc:
            probes.append({"name": name, "blocked": True, "code": exc.code})
        except BaseException as exc:
            probes.append({"name": name, "blocked": False, "outcome": type(exc).__name__, "detail": str(exc)[:120]})

    host_candidates = []
    system_root = os.environ.get("SYSTEMROOT") or os.environ.get("SystemRoot")
    if system_root:
        host_candidates.append(os.path.join(system_root, "win.ini"))
    host_candidates.extend(["/etc/hostname", "/etc/passwd"])
    host_candidates.append(os.path.abspath(__file__))

    def _host_read():
        for candidate in host_candidates:
            if not os.path.exists(candidate):
                continue
            handle = open(candidate, "r")
            try:
                payload = handle.read(64)
            finally:
                handle.close()
            return "read_succeeded:" + candidate + ":" + str(len(payload))
        return "no_candidate_file"

    def _host_write():
        target = os.path.join(os.getcwd(), "selfcheck_should_not_exist.txt")
        handle = open(target, "w")
        try:
            handle.write("probe")
        finally:
            handle.close()
        try:
            os.remove(target)
        except Exception:
            pass
        return "write_succeeded:" + target

    def _network():
        sock = socket.socket()
        try:
            sock.settimeout(0.2)
            sock.connect(("127.0.0.1", 1))
            return "connect_succeeded"
        finally:
            try:
                sock.close()
            except Exception:
                pass

    def _spawn():
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        try:
            proc.wait(timeout=5)
            return "spawn_succeeded"
        finally:
            try:
                proc.kill()
            except Exception:
                pass

    def _dynamic_import():
        module = importlib.import_module("multiprocessing")
        return "import_succeeded:" + getattr(module, "__name__", "?")

    def _listdir():
        return "listdir_succeeded:" + str(len(os.listdir(os.getcwd())))

    _probe("host_file_read", _host_read)
    _probe("workspace_file_write", _host_write)
    _probe("network_connect", _network)
    _probe("subprocess_spawn", _spawn)
    _probe("dynamic_import_forbidden", _dynamic_import)
    _probe("os_listdir", _listdir)

    unexpected = [item["name"] for item in probes if not item["blocked"]]
    return {
        "status": "ok" if not unexpected else "violation",
        "audit_hook_active": not unexpected,
        "probes": probes,
        "unexpected_success": unexpected,
        "limits_applied": applied,
        "blocked": [] if not unexpected else ["SANDBOX_SELFCHECK_FAILED:" + ",".join(unexpected)],
        "result": None,
        "error": None,
        "stdout": "",
        "stdout_truncated": False,
    }


def _emit(payload, limit):
    real_stdout = sys.__stdout__
    payload = dict(payload)
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > limit:
        payload["stdout"] = ""
        payload["stdout_truncated"] = True
        payload["truncated"] = True
        text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > limit:
        result = payload.get("result")
        payload["result"] = {
            "_truncated": True,
            "type": type(result).__name__,
            "preview": str(result)[:512],
        }
        payload["result_truncated"] = True
        text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text.encode("utf-8")) > limit:
        payload["result"] = None
        text = json.dumps(payload, ensure_ascii=False, default=str)
    raw = text.encode("utf-8")[:limit]
    real_stdout.buffer.write(raw)
    real_stdout.buffer.flush()


def main():
    # 显式用二进制读写：-I 会忽略 PYTHONIOENCODING，不能依赖 locale 编码
    raw_input = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw_input.decode("utf-8")) if raw_input.strip() else {}
    except (ValueError, UnicodeDecodeError) as exc:
        _emit({"status": "error", "error": {"code": "SANDBOX_PROTOCOL_ERROR", "message": str(exc)[:200]}, "blocked": [], "result": None}, 4096)
        return 0
    policy = payload.get("policy") or {}
    allowed = set(payload.get("allowed_imports") or [])
    limit = int(policy.get("output_bytes") or 1048576)
    try:
        if payload.get("op") == "selfcheck":
            report = _selfcheck(policy, allowed)
        else:
            report = _analyze(payload, allowed, policy)
    except BaseException as exc:
        # 任何未预期异常都必须变成结构化结果，不能以"空 stdout + 退出码 1"收场
        report = {
            "status": "error",
            "result": None,
            "blocked": [],
            "error": {"code": "SANDBOX_RUNNER_ERROR", "type": type(exc).__name__, "message": str(exc)[:200]},
            "stdout": "",
            "stdout_truncated": False,
            "limits_applied": [],
        }
    _emit(report, limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def runner_source() -> str:
    """返回沙箱 runner 源码（写入一次性临时目录后以 ``-I -S`` 执行）。"""
    return _RUNNER_SOURCE


def _child_env(workspace: str | None) -> dict[str, str]:
    """最小化子进程环境：不传数据库、模型密钥、HOME 与任何 FinPilot 变量。"""
    env: dict[str, str] = {
        "PATH": os.environ.get("PATH", ""),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONHASHSEED": "0",
        "PYTHONNOUSERSITE": "1",
    }
    for name in ("SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    if workspace:
        env["TEMP"] = workspace
        env["TMP"] = workspace
        env["HOME"] = workspace
        env["USERPROFILE"] = workspace
    return env


def _platform_limits(
    policy: SandboxPolicy, child_applied: Sequence[str]
) -> tuple[list[str], list[str], list[str], dict[str, Any]]:
    """返回 (已强制生效, 尽力而为, 未生效) 的限额清单（如实标注平台差异）。"""
    enforced = ["ast_validation", "audit_hook", "wall_timeout", "output_limit", "env_minimized", "workspace_isolated"]
    best_effort: list[str] = []
    not_enforced: list[str] = []
    if os.name == "posix":
        enforced.extend(child_applied or ["cpu_rlimit", "memory_rlimit", "process_rlimit", "file_write_rlimit"])
    else:
        not_enforced = ["cpu_rlimit", "memory_rlimit", "process_rlimit", "file_write_rlimit", "kernel_filesystem_isolation"]
        best_effort = [
            "cpu_limit(仅墙钟超时兜底)",
            "memory_limit(未强制，仅靠输出限额与数据规模约束)",
            "process_limit(audit hook 拦截创建，非内核 rlimit)",
            "file_write_block(audit hook 拦截，非只读根文件系统)",
        ]
    declared = {
        "cpu_seconds": policy.cpu_seconds,
        "wall_seconds": policy.wall_seconds,
        "memory_mb": policy.memory_mb,
        "output_bytes": policy.output_bytes,
        "max_processes": policy.max_processes,
        "workspace_mb": policy.workspace_mb,
    }
    return enforced, best_effort, not_enforced, declared


def _unavailable(reason_codes: Sequence[str], *, policy_version: str = POLICY_VERSION, error: str | None = None) -> dict[str, Any]:
    return {
        "status": STATUS_UNAVAILABLE,
        "result": None,
        "blocked": list(reason_codes),
        "resources": {
            "wall_ms": 0,
            "output_bytes": 0,
            "truncated": False,
            "exit_code": None,
            "limits_enforced": [],
            "limits_best_effort": [],
            "limits_not_enforced": [],
            "limits_declared": {},
            "platform": os.name,
        },
        "policy_version": policy_version,
        "engine": "unavailable",
        "error": {"code": "SANDBOX_UNAVAILABLE", "message": error or "分析沙箱不可用，已关闭生成代码执行。"},
        "notes": ["沙箱不可用时绝不回退到宿主进程 exec/eval；个性化报表计算改用 run_dsl。"],
    }


def _resolve_policy(policy: SandboxPolicy | str | os.PathLike[str] | None) -> SandboxPolicy:
    if policy is None:
        resolved = SandboxPolicy.from_settings()
    elif isinstance(policy, SandboxPolicy):
        resolved = policy
    elif isinstance(policy, (str, os.PathLike)):
        resolved = SandboxPolicy.from_file(policy)
    else:
        raise TypeError(f"不支持的 policy 类型: {type(policy).__name__}")
    resolved.validate()
    return resolved


def run_analysis(
    code: str,
    data: Mapping[str, Any] | Sequence[Any] | None = None,
    policy: SandboxPolicy | str | os.PathLike[str] | None = None,
) -> dict[str, Any]:
    """在受限子进程中执行分析代码，返回结构化结果。

    返回::

        {"status": "ok|violation|timeout|unavailable|error",
         "result": 任意 JSON 值或 null,
         "blocked": ["IMPORT_FORBIDDEN:os", ...],
         "resources": {"wall_ms", "output_bytes", "truncated", "exit_code",
                       "limits_enforced", "limits_best_effort", "limits_not_enforced",
                       "limits_declared", "platform", "peak_memory_mb", ...},
         "policy_version": "sandbox-2026.10",
         "engine": "subprocess+audit_hook",
         "error": {...} | null,
         "stdout": 分析代码的打印输出（截断）,
         "code_hash": sha256(code),
         "notes": [平台差异与边界说明]}

    约定：用户代码把要输出的结果赋给变量 ``result``（未赋值时取最后一条表达式语句的值）；
    输入数据通过 ``data`` 变量提供。状态含义：``violation`` = 静态校验或运行时 hook 拦截；
    ``timeout`` = 超过墙钟上限被父进程 kill；``unavailable`` = 策略禁用／策略加载失败／
    子进程不可启动（**绝不在宿主进程执行代码**）；``error`` = 用户代码异常或协议错误。
    """
    try:
        resolved = _resolve_policy(policy)
    except FinPilotError as exc:
        return _unavailable(exc.details.get("reason_codes", ["SANDBOX_UNAVAILABLE"]), error=exc.message)
    except Exception as exc:
        return _unavailable([f"POLICY_LOAD_FAILED:{type(exc).__name__}"], error=f"沙箱策略加载失败：{type(exc).__name__}")

    if not resolved.enabled:
        record_blocked_attempt(source="run_analysis", reason_codes=["SANDBOX_DISABLED"], code_hash=ids.sha256_hex(code or ""))
        return _unavailable(["SANDBOX_DISABLED"], policy_version=resolved.policy_version, error="沙箱已禁用，代码执行关闭。")

    if not isinstance(code, str):
        return {
            "status": STATUS_VIOLATION,
            "result": None,
            "blocked": ["CODE_NOT_STRING"],
            "resources": {
                "wall_ms": 0,
                "output_bytes": 0,
                "truncated": False,
                "exit_code": None,
                "limits_enforced": ["ast_validation"],
                "limits_best_effort": [],
                "limits_not_enforced": [],
                "limits_declared": resolved.child_payload(),
                "platform": os.name,
            },
            "policy_version": resolved.policy_version,
            "engine": "ast-validator",
            "error": {"code": "SANDBOX_VIOLATION", "message": "分析代码必须是字符串。"},
            "code_hash": "",
            "notes": ["静态校验在父进程完成；代码未进入任何子进程，宿主未执行。"],
        }
    code_text = code
    violations = validate_code(code_text)
    if violations:
        record_blocked_attempt(
            source="run_analysis.ast",
            reason_codes=violations,
            code_hash=ids.sha256_hex(code_text),
            detail={"length": len(code_text)},
        )
        return {
            "status": STATUS_VIOLATION,
            "result": None,
            "blocked": violations,
            "resources": {
                "wall_ms": 0,
                "output_bytes": 0,
                "truncated": False,
                "exit_code": None,
                "limits_enforced": ["ast_validation"],
                "limits_best_effort": [],
                "limits_not_enforced": [],
                "limits_declared": resolved.child_payload(),
                "platform": os.name,
            },
            "policy_version": resolved.policy_version,
            "engine": "ast-validator",
            "error": {"code": "SANDBOX_VIOLATION", "message": "生成代码触发了沙箱禁止能力，已阻止执行。"},
            "code_hash": ids.sha256_hex(code_text),
            "notes": ["静态校验在父进程完成；代码未进入任何子进程，宿主未执行。"],
        }

    if isinstance(data, Mapping):
        data_payload: dict[str, Any] = dict(data)
    elif isinstance(data, (list, tuple)):
        data_payload = {"rows": [dict(row) if isinstance(row, Mapping) else row for row in data]}
    elif data is None:
        data_payload = {}
    else:
        data_payload = {"value": data}
    payload = {
        "op": "analyze",
        "code": code_text,
        "data": data_payload,
        "policy": resolved.child_payload(),
        "allowed_imports": sorted(allowed_imports()),
    }
    return _run_child(payload, resolved, code_hash=ids.sha256_hex(code_text))


def sandbox_selfcheck(policy: SandboxPolicy | str | os.PathLike[str] | None = None) -> dict[str, Any]:
    """在沙箱内实际探测运行时拦截是否生效（自检／证据采集）。

    探针全部由 runner（受信代码）发起：读宿主文件、写工作区文件、连接 127.0.0.1:1、
    创建子进程、动态导入非白名单模块、列目录。只有被 audit hook 以 ``_Blocked`` 拦下
    才算 ``blocked=True``；探针若以其它方式失败（例如端口拒绝、权限错误）会如实记为
    ``outcome``，不会被算作"拦截成功"。
    """
    try:
        resolved = _resolve_policy(policy)
    except FinPilotError as exc:
        return _unavailable(exc.details.get("reason_codes", ["SANDBOX_UNAVAILABLE"]), error=exc.message)
    except Exception as exc:
        return _unavailable([f"POLICY_LOAD_FAILED:{type(exc).__name__}"], error=f"沙箱策略加载失败：{type(exc).__name__}")
    if not resolved.enabled:
        return _unavailable(["SANDBOX_DISABLED"], policy_version=resolved.policy_version, error="沙箱已禁用，自检不可用。")
    payload = {
        "op": "selfcheck",
        "policy": resolved.child_payload(),
        "allowed_imports": sorted(allowed_imports()),
    }
    return _run_child(payload, resolved, code_hash="")


def _run_child(payload: Mapping[str, Any], policy: SandboxPolicy, *, code_hash: str) -> dict[str, Any]:
    """真正拉起子进程；任何启动失败都返回 unavailable（不回退宿主执行）。"""
    tmpdir: str | None = None
    notes: list[str] = []
    if os.name != "posix":
        notes.append("Windows 本机：无 resource 模块，CPU／内存／进程数硬限额未生效，由墙钟超时、输出限额、AST 校验与 audit hook 兜底。")
        notes.append("audit hook 是 Python 层拦截，不是内核级 seccomp／容器隔离；宿主机文件系统未被内核级隔离。")
    else:
        notes.append("POSIX：已用 setrlimit 施加 CPU／内存／进程数／文件写入硬限额。")
    notes.append("沙箱不可用时绝不回退到宿主进程 exec/eval；个性化报表改用 run_dsl。")
    try:
        tmpdir = tempfile.mkdtemp(prefix="finpilot-sandbox-")
        runner_path = os.path.join(tmpdir, "runner.py")
        workspace = os.path.join(tmpdir, "work")
        os.makedirs(workspace, exist_ok=True)
        with open(runner_path, "w", encoding="utf-8") as handle:
            handle.write(_RUNNER_SOURCE)
        stdin_bytes = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        env = _child_env(workspace)
        started = time.perf_counter()
        try:
            proc = subprocess.Popen(
                [sys.executable, "-I", "-S", runner_path],
                cwd=workspace,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except Exception as exc:
            logger.error("沙箱子进程启动失败：%s", type(exc).__name__)
            record_blocked_attempt(source="run_analysis.spawn", reason_codes=[f"SANDBOX_SPAWN_FAILED:{type(exc).__name__}"], code_hash=code_hash)
            return _unavailable(["SANDBOX_SPAWN_FAILED"], policy_version=policy.policy_version, error=f"沙箱子进程无法启动：{type(exc).__name__}")

        timed_out = False
        try:
            stdout_bytes, stderr_bytes = proc.communicate(input=stdin_bytes, timeout=policy.wall_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            stdout_bytes, stderr_bytes = proc.communicate()
        wall_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:  # pragma: no cover - 临时目录/IO 故障
        logger.exception("沙箱运行环境准备失败")
        return _unavailable([f"SANDBOX_ENV_FAILED:{type(exc).__name__}"], policy_version=policy.policy_version, error="沙箱环境准备失败。")
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)

    exit_code = proc.returncode
    limit = policy.output_bytes
    truncated = False
    raw_stdout = stdout_bytes or b""
    if len(raw_stdout) > limit:
        raw_stdout = raw_stdout[:limit]
        truncated = True
    stdout_text = raw_stdout.decode("utf-8", "ignore")
    stderr_tail = (stderr_bytes or b"")[-600:].decode("utf-8", "ignore")
    enforced, best_effort, not_enforced, declared = _platform_limits(policy, [])

    if timed_out:
        record_blocked_attempt(
            source="run_analysis.wall_timeout",
            reason_codes=["WALL_TIMEOUT"],
            code_hash=code_hash,
            detail={"wall_seconds": policy.wall_seconds, "wall_ms": wall_ms},
        )
        return {
            "status": STATUS_TIMEOUT,
            "result": None,
            "blocked": ["WALL_TIMEOUT"],
            "resources": {
                "wall_ms": wall_ms,
                "output_bytes": len(raw_stdout),
                "truncated": truncated,
                "exit_code": exit_code,
                "limits_enforced": enforced,
                "limits_best_effort": best_effort,
                "limits_not_enforced": not_enforced,
                "limits_declared": declared,
                "platform": os.name,
            },
            "policy_version": policy.policy_version,
            "engine": "subprocess+audit_hook",
            "error": {"code": "SANDBOX_TIMEOUT", "message": f"分析超过墙钟上限 {policy.wall_seconds}s，已终止子进程。"},
            "stdout": stdout_text,
            "code_hash": code_hash,
            "notes": notes,
        }

    try:
        report = json.loads(stdout_text) if stdout_text.strip() else {}
    except ValueError:
        report = {}
    if not isinstance(report, dict) or "status" not in report:
        record_blocked_attempt(
            source="run_analysis.protocol",
            reason_codes=["SANDBOX_PROTOCOL_ERROR"],
            code_hash=code_hash,
            detail={"exit_code": exit_code},
        )
        return {
            "status": STATUS_ERROR,
            "result": None,
            "blocked": ["SANDBOX_PROTOCOL_ERROR"],
            "resources": {
                "wall_ms": wall_ms,
                "output_bytes": len(raw_stdout),
                "truncated": truncated,
                "exit_code": exit_code,
                "limits_enforced": enforced,
                "limits_best_effort": best_effort,
                "limits_not_enforced": not_enforced,
                "limits_declared": declared,
                "platform": os.name,
            },
            "policy_version": policy.policy_version,
            "engine": "subprocess+audit_hook",
            "error": {"code": "SANDBOX_PROTOCOL_ERROR", "message": "沙箱子进程未返回可解析结果。", "stderr_tail": stderr_tail},
            "stdout": stdout_text,
            "code_hash": code_hash,
            "notes": notes,
        }

    child_applied = list(report.get("limits_applied") or [])
    enforced, best_effort, not_enforced, declared = _platform_limits(policy, child_applied)
    blocked = [str(item) for item in (report.get("blocked") or [])]
    if blocked and report.get("status") == "violation":
        record_blocked_attempt(
            source="run_analysis.runtime",
            reason_codes=blocked,
            code_hash=code_hash,
            detail={"exit_code": exit_code},
        )
    result_truncated = bool(report.get("result_truncated"))
    outcome: dict[str, Any] = {
        "status": str(report.get("status") or STATUS_ERROR),
        "result": report.get("result"),
        "blocked": blocked,
        "resources": {
            "wall_ms": wall_ms,
            "output_bytes": len(raw_stdout),
            "truncated": bool(truncated or report.get("truncated") or result_truncated),
            "stdout_truncated": bool(report.get("stdout_truncated")),
            "result_truncated": result_truncated,
            "exit_code": exit_code,
            "limits_enforced": enforced,
            "limits_best_effort": best_effort,
            "limits_not_enforced": not_enforced,
            "limits_declared": declared,
            "platform": os.name,
            "child_limits_applied": child_applied,
        },
        "policy_version": policy.policy_version,
        "engine": "subprocess+audit_hook",
        "error": report.get("error"),
        "stdout": report.get("stdout") or "",
        "code_hash": code_hash,
        "notes": notes,
    }
    for extra_key in ("probes", "audit_hook_active", "unexpected_success", "preimported"):
        if extra_key in report:
            outcome[extra_key] = report[extra_key]
    return outcome


# --- 受限 DSL（沙箱不可用时的兜底报表计算） --------------------------------

_DSL_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<number>-?\d+(?:\.\d+)?)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<ident>[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*(?:\.[A-Za-z0-9_\u4e00-\u9fff]+)*)
  | (?P<star>\*)
  | (?P<comma>,)
  | (?P<lparen>\()
  | (?P<rparen>\))
""",
    re.VERBOSE,
)

_DSL_FUNCTIONS = ("sum", "count", "avg", "min", "max", "pct", "group_by")
_MAX_DSL_LENGTH = 2000
_MAX_DSL_DEPTH = 8


class _DslError(FinPilotError):
    def __init__(self, message: str, reason: str) -> None:
        FinPilotError.__init__(
            self,
            "PARAM_INVALID",
            message=message,
            details={"reason_codes": [reason]},
        )


def _dsl_tokenize(expression: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    while position < len(expression):
        match = _DSL_TOKEN_RE.match(expression, position)
        if not match:
            raise _DslError(f"表达式包含不支持的字符：{expression[position]!r}", "DSL_SYNTAX")
        position = match.end()
        kind = match.lastgroup or ""
        if kind == "ws":
            continue
        tokens.append((kind, match.group(0)))
    return tokens


def _dsl_parse(expression: str) -> dict[str, Any]:
    tokens = _dsl_tokenize(expression)
    index = 0

    def _peek() -> tuple[str, str] | None:
        return tokens[index] if index < len(tokens) else None

    def _expect(kind: str) -> str:
        nonlocal index
        token = _peek()
        if token is None or token[0] != kind:
            raise _DslError(f"表达式语法错误：期望 {kind}", "DSL_SYNTAX")
        index += 1
        return token[1]

    def _parse_expr(depth: int) -> dict[str, Any]:
        nonlocal index
        if depth > _MAX_DSL_DEPTH:
            raise _DslError("表达式嵌套过深", "DSL_DEPTH")
        token = _peek()
        if token is None:
            raise _DslError("表达式不完整", "DSL_SYNTAX")
        kind, value = token
        if kind == "number":
            index += 1
            return {"op": "literal", "value": float(value) if "." in value else int(value)}
        if kind == "ident":
            name = value
            index += 1
            if name not in _DSL_FUNCTIONS:
                raise _DslError(f"未知函数或字段：{name}", f"DSL_UNKNOWN_FUNCTION:{name}")
            _expect("lparen")
            args: list[Any] = []
            if name == "count" and _peek() and _peek()[0] == "rparen":
                index += 1
                return {"op": "count", "field": None}
            while True:
                inner = _peek()
                if inner is None:
                    raise _DslError("表达式缺少右括号", "DSL_SYNTAX")
                inner_kind, inner_text = inner
                if inner_kind == "star":
                    index += 1
                    args.append({"op": "literal", "value": "*"})
                elif inner_kind == "ident":
                    is_call = (
                        inner_text in _DSL_FUNCTIONS
                        and index + 1 < len(tokens)
                        and tokens[index + 1][0] == "lparen"
                    )
                    if is_call:
                        args.append(_parse_expr(depth + 1))
                    else:
                        index += 1
                        args.append({"op": "field", "name": inner_text})
                elif inner_kind == "string":
                    index += 1
                    args.append({"op": "field", "name": inner_text[1:-1]})
                elif inner_kind == "number":
                    index += 1
                    args.append({"op": "literal", "value": float(inner_text) if "." in inner_text else int(inner_text)})
                elif inner_kind == "lparen":
                    args.append(_parse_expr(depth + 1))
                else:
                    raise _DslError(f"函数参数不合法：{inner_text}", "DSL_SYNTAX")
                following = _peek()
                if following is None:
                    raise _DslError("表达式缺少右括号", "DSL_SYNTAX")
                if following[0] == "comma":
                    index += 1
                    continue
                _expect("rparen")
                break
            return {"op": name, "args": args}
        raise _DslError(f"表达式语法错误：{value}", "DSL_SYNTAX")

    tree = _parse_expr(0)
    if index != len(tokens):
        raise _DslError("表达式结尾存在多余记号", "DSL_SYNTAX")
    return tree


def _dsl_rows(data: Any) -> tuple[list[Any], dict[str, Any]]:
    if isinstance(data, list):
        return list(data), {}
    if not isinstance(data, Mapping):
        raise _DslError("data 必须是对象或行列表", "DSL_DATA_INVALID")
    for key in ("rows", "items", "records", "data", "entries"):
        value = data.get(key)
        if isinstance(value, list):
            return list(value), dict(data)
    return [dict(data)], dict(data)


def _dsl_dig(node: Any, path: str) -> Any:
    cursor = node
    for part in path.split("."):
        if isinstance(cursor, Mapping) and part in cursor:
            cursor = cursor[part]
        else:
            return None
    return cursor


def _dsl_values(field: str, rows: list[Any], top: Mapping[str, Any]) -> list[Any]:
    values: list[Any] = []
    found = False
    for row in rows:
        value = _dsl_dig(row, field)
        if value is not None:
            found = True
        values.append(value)
    if not found and field in top and isinstance(top[field], list):
        values = list(top[field])
    flat: list[Any] = []
    for value in values:
        if isinstance(value, list):
            flat.extend(value)
        else:
            flat.append(value)
    return flat


def _dsl_numeric(values: Iterable[Any]) -> tuple[list[float], int]:
    numbers: list[float] = []
    skipped = 0
    for value in values:
        if isinstance(value, bool) or value is None:
            skipped += 1
            continue
        if isinstance(value, (int, float)):
            numbers.append(float(value))
            continue
        if isinstance(value, str):
            text = value.strip()
            try:
                numbers.append(float(text))
                continue
            except ValueError:
                skipped += 1
                continue
        skipped += 1
    return numbers, skipped


def _dsl_percentile(numbers: list[float], percentile: float) -> float:
    if not numbers:
        raise _DslError("没有可用于百分位计算的数值", "DSL_NO_DATA")
    if percentile < 0 or percentile > 100:
        raise _DslError("百分位必须在 0 到 100 之间", "DSL_PERCENTILE_RANGE")
    ordered = sorted(numbers)
    if len(ordered) == 1:
        return ordered[0]
    rank = (percentile / 100.0) * (len(ordered) - 1)
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    fraction = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * fraction


def _dsl_eval(node: Mapping[str, Any], rows: list[Any], top: Mapping[str, Any]) -> Any:
    op = node.get("op")
    if op == "literal":
        return node.get("value")
    if op == "field":
        return node.get("name")
    if op == "count":
        field = node.get("field")
        if field is None:
            return len(rows)
        values = _dsl_values(str(field), rows, top)
        return sum(1 for value in values if value is not None)
    if op in ("sum", "avg", "min", "max", "pct"):
        args = node.get("args") or []
        if not args:
            raise _DslError(f"{op} 需要一个字段参数", "DSL_ARGS")
        field_node = args[0]
        if field_node.get("op") != "field":
            raise _DslError(f"{op} 的第一个参数必须是字段名", "DSL_ARGS")
        values = _dsl_values(str(field_node.get("name")), rows, top)
        numbers, skipped = _dsl_numeric(values)
        if op == "pct":
            if len(args) > 1 and args[1].get("op") == "literal":
                percentile = float(args[1]["value"])
            else:
                raise _DslError("pct 需要第二个参数（0-100 的百分位）", "DSL_ARGS")
            return round(_dsl_percentile(numbers, percentile), 6)
        if not numbers:
            return 0 if op == "sum" else None
        if op == "sum":
            total = sum(numbers)
            return int(total) if total.is_integer() else round(total, 6)
        if op == "avg":
            return round(sum(numbers) / len(numbers), 6)
        if op == "min":
            value = min(numbers)
            return int(value) if float(value).is_integer() else value
        value = max(numbers)
        return int(value) if float(value).is_integer() else value
    if op == "group_by":
        args = node.get("args") or []
        if len(args) != 2:
            raise _DslError("group_by 需要 (分组字段, 聚合表达式)", "DSL_ARGS")
        key_node, agg_node = args
        if key_node.get("op") != "field":
            raise _DslError("group_by 的第一个参数必须是字段名", "DSL_ARGS")
        key_field = str(key_node.get("name"))
        buckets: dict[str, list[Any]] = {}
        for row in rows:
            key = _dsl_dig(row, key_field)
            buckets.setdefault("" if key is None else str(key), []).append(row)
        return [
            {"key": key, "value": _dsl_eval(agg_node, buckets[key], {})}
            for key in sorted(buckets)
        ]
    raise _DslError(f"不支持的表达式：{op}", "DSL_UNSUPPORTED")


def run_dsl(expression: str, data: Mapping[str, Any] | Sequence[Any] | None = None) -> dict[str, Any]:
    """受限 DSL 报表计算：``sum``／``count``／``avg``／``min``／``max``／``group_by``／``pct``。

    语法（无 I/O、无变量赋值、无导入、无循环，手写词法／语法分析，不使用 ``eval``）：:

        sum(amount_minor)
        count()
        avg(支出)
        pct(amount_minor, 90)
        group_by(category, sum(amount_minor))
        group_by(merchant.name, count())

    字段用点路径取值；``data`` 支持 ``{"rows": [...]}``（或 ``items``／``records``／
    ``data``／``entries``）与"标量列表字段"（如 ``{"amounts": [1, 2, 3]}`` → ``sum(amounts)``）。
    非数值项按 SQL 的 NULL 语义跳过；``avg``／``pct`` 结果保留 6 位小数。

    返回 ``{"status": "ok", "result": ..., "rows": n, "skipped": n, ...}``；表达式非法时
    返回 ``{"status": "error", "reason_codes": [...]}`` 而不是抛异常（便于降级链路复用）。
    本函数用于沙箱不可用时仍能完成个性化报表计算，属于"能力降级"而非"安全替代"：
    它没有网络与文件访问面，但也只支持上述固定算子。
    """
    expr = expression if isinstance(expression, str) else ""
    if not expr.strip():
        return {
            "status": "error",
            "engine": "finpilot-dsl",
            "expression": expr,
            "result": None,
            "reason_codes": ["DSL_EMPTY"],
            "error": {"code": "PARAM_INVALID", "message": "表达式为空。"},
            "policy_version": POLICY_VERSION,
        }
    if len(expr) > _MAX_DSL_LENGTH:
        return {
            "status": "error",
            "engine": "finpilot-dsl",
            "expression": expr[:200],
            "result": None,
            "reason_codes": ["DSL_TOO_LONG"],
            "error": {"code": "PARAM_INVALID", "message": "表达式过长。"},
            "policy_version": POLICY_VERSION,
        }
    try:
        tree = _dsl_parse(expr)
        rows, top = _dsl_rows(data if data is not None else {})
        result = _dsl_eval(tree, rows, top)
        skipped = 0
        if tree.get("op") in ("sum", "avg", "min", "max", "pct") and tree.get("args"):
            field_node = tree["args"][0]
            if field_node.get("op") == "field":
                _numbers, skipped = _dsl_numeric(_dsl_values(str(field_node.get("name")), rows, top))
        return {
            "status": "ok",
            "engine": "finpilot-dsl",
            "expression": expr,
            "result": result,
            "rows": len(rows),
            "skipped": skipped,
            "reason_codes": [],
            "error": None,
            "policy_version": POLICY_VERSION,
            "note": "受限 DSL：无 I/O、无变量赋值、无导入；用于沙箱不可用时的报表降级计算。",
        }
    except FinPilotError as exc:
        return {
            "status": "error",
            "engine": "finpilot-dsl",
            "expression": expr,
            "result": None,
            "reason_codes": list(exc.details.get("reason_codes", ["DSL_ERROR"])),
            "error": {"code": exc.code, "message": exc.message},
            "policy_version": POLICY_VERSION,
        }
    except Exception as exc:  # pragma: no cover - 兜底：任何解析/求值异常都不外泄堆栈
        return {
            "status": "error",
            "engine": "finpilot-dsl",
            "expression": expr,
            "result": None,
            "reason_codes": [f"DSL_INTERNAL:{type(exc).__name__}"],
            "error": {"code": "PARAM_INVALID", "message": "表达式无法求值。"},
            "policy_version": POLICY_VERSION,
        }


__all__ = [
    "ALLOWED_IMPORTS",
    "FORBIDDEN_CALLS",
    "FORBIDDEN_IMPORTS",
    "OPTIONAL_IMPORTS",
    "POLICY_VERSION",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STATUS_UNAVAILABLE",
    "STATUS_VIOLATION",
    "SandboxPolicy",
    "allowed_imports",
    "blocked_attempts",
    "clear_blocked_attempts",
    "optional_module_available",
    "record_blocked_attempt",
    "run_analysis",
    "run_dsl",
    "runner_source",
    "sandbox_selfcheck",
    "validate_code",
]
