"""调度器常驻工作者入口（DEL-02 部署脚本）。

``finpilot.workers.scheduler`` 提供 ``run_due_jobs`` / ``run_job_forever``；本入口用
**可优雅退出**的循环调用 ``run_due_jobs``（``run_job_forever`` 是无限循环、不响应信号），
业务语义完全复用内核：租约、窗口重检、到期重判策略等级与授权、失败归因。

用法::

    & .\\venv\\Scripts\\python.exe scripts\\worker_scheduler.py              # 5 秒一轮
    & .\\venv\\Scripts\\python.exe scripts\\worker_scheduler.py --once       # 只跑一轮
    & .\\venv\\Scripts\\python.exe scripts\\worker_scheduler.py --interval 10

对应 V5：S1-03、AUTH-07、T05（窗口外 missed，绝不静默执行）。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from finpilot.db import session_scope
from finpilot.workers.scheduler import run_due_jobs

logger = logging.getLogger("finpilot.workers.scheduler.loop")

_STOP = False


def _handle_signal(signum: int, _frame) -> None:  # pragma: no cover - 信号处理
    global _STOP
    _STOP = True
    logger.info("收到信号 %s，准备退出", signum)


def run_loop(*, interval: float, limit: int, once: bool) -> dict:
    totals = {"rounds": 0, "leased": 0, "executed": 0, "waiting": 0, "missed": 0, "failed": 0}
    while not _STOP:
        with session_scope() as session:
            result = run_due_jobs(session, limit=limit)
        totals["rounds"] += 1
        for key in ("leased", "executed", "waiting", "missed", "failed"):
            totals[key] += int(result.get(key, 0))
        if result.get("leased"):
            logger.info("scheduler round=%s result=%s", totals["rounds"], result)
        if once:
            break
        time.sleep(interval)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinPilot 调度器常驻工作者")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    parser.add_argument("--limit", type=int, default=20, help="每轮最多领取作业数（默认 20）")
    parser.add_argument("--once", action="store_true", help="只跑一轮后退出（脚本/测试用）")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (AttributeError, ValueError):  # pragma: no cover
        pass

    totals = run_loop(interval=args.interval, limit=args.limit, once=args.once)
    logger.info("调度器工作者退出：%s", totals)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())