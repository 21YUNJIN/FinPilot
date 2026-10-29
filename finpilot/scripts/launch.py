"""分离式进程启动器（DEL-02 运维脚本的支撑工具）。

为什么不用 PowerShell 的 ``Start-Process``：带 ``-RedirectStandardOutput`` 时，
Windows 上 ``CreateProcess`` 会带 ``bInheritHandles=TRUE``，于是**子孙进程会继承父进程
的 stdout 管道句柄**。长驻服务（api / worker / 沙箱）一旦继承了调用方的管道，管道就
直到它们全部退出才关闭——典型症状是"脚本已经跑完、但把输出接进管道/CI 的那一步一直挂住"。
本机实测到过这个现象（``dev_up.ps1 | Out-File`` 在两分钟后仍未返回）。

本启动器用 ``subprocess.Popen(close_fds=True)`` 在 Windows 上把 ``bInheritHandles``
置为 FALSE（POSIX 上等价于 ``start_new_session``），子进程只拿到自己那两个日志文件句柄，
不会持有调用方的管道。启动后立即打印 PID 并退出，便于脚本记录与停止。

用法::

    python scripts/launch.py --out api.out.log --err api.err.log --cwd <repo> -- \
        python -m uvicorn finpilot.app:app --host 127.0.0.1 --port 58000

输出：一行 ``PID=<pid>``（脚本侧解析；解析失败说明启动失败）。
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def _detach_flags() -> int:
    """Windows: DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP；其它平台: 0。"""
    if os.name == "nt":
        return int(getattr(subprocess, "DETACHED_PROCESS", 0x00000008)) | int(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinPilot 分离式进程启动器")
    parser.add_argument("--out", required=True, help="stdout 日志文件")
    parser.add_argument("--err", required=True, help="stderr 日志文件")
    parser.add_argument("--cwd", default=None, help="工作目录")
    parser.add_argument("--pid-file", default=None, help="把子进程 PID 写入该文件（脚本侧轮询用）")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="-- 之后的命令")
    args = parser.parse_args(argv)

    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("ERROR: empty command", file=sys.stderr)
        return 2

    out_path = Path(args.out)
    err_path = Path(args.err)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    err_path.parent.mkdir(parents=True, exist_ok=True)

    kwargs = {}
    if os.name != "nt":
        kwargs["start_new_session"] = True

    with out_path.open("wb") as out_handle, err_path.open("wb") as err_handle:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=out_handle,
            stderr=err_handle,
            cwd=args.cwd,
            close_fds=True,
            creationflags=_detach_flags(),
            **kwargs,
        )
    print(f"PID={process.pid}")
    if args.pid_file:
        pid_path = Path(args.pid_file)
        pid_path.parent.mkdir(parents=True, exist_ok=True)
        pid_path.write_text(f"{process.pid}", encoding="ascii")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())