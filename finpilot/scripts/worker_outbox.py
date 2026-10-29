"""Outbox 常驻工作者入口（DEL-02 部署脚本）。

为什么需要这个文件：``finpilot.workers.outbox`` 只提供函数（``process_pending`` /
``requeue_stuck``），没有 ``__main__``；而容器与本机都需要一个"常驻循环"的可执行入口。
本入口**不复制业务逻辑**，只是按固定节奏调用内核已有的函数，并在收到 SIGINT/SIGTERM
时优雅退出（容器里 ``docker stop`` 会先发 SIGTERM）。

用法::

    & .\\venv\\Scripts\\python.exe scripts\\worker_outbox.py                # 5 秒一轮
    & .\\venv\\Scripts\\python.exe scripts\\worker_outbox.py --once         # 只跑一轮（排空）
    & .\\venv\\Scripts\\python.exe scripts\\worker_outbox.py --interval 2 --limit 20

对应 V5：MONEY-04、T21/T22（外部效果只能在事务提交后发生；未知结果绝不算失败）。
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from finpilot.db import session_scope
from finpilot.workers.outbox import process_pending, requeue_stuck

logger = logging.getLogger("finpilot.workers.outbox.loop")

_STOP = False


def bootstrap() -> dict:
    """Worker 启动时必须注册提供方适配器。

    Outbox 投递与 API 是**两个进程**，适配器注册表不共享。历史上只有 API 启动时注册，
    导致 Worker 投递时 ``get_adapter`` 抛 ``PROVIDER_CAPABILITY_MISSING``：操作直接
    failed、任务停在 reconciling、日志里只有 ``delivered=0 retried=N``。
    """
    from finpilot.adapters.registry import bootstrap_worker_adapters

    status = bootstrap_worker_adapters()
    logger.info("提供方适配器已注册：%s", status.get("registered"))
    unavailable = {k: v for k, v in status.items() if k != "registered" and str(v).startswith("unavailable")}
    if unavailable:
        logger.warning("部分适配器不可用：%s", unavailable)
    return status


def _handle_signal(signum: int, _frame) -> None:  # pragma: no cover - 信号处理
    global _STOP
    _STOP = True
    logger.info("收到信号 %s，准备退出（本轮处理完后停止）", signum)


def run_loop(*, interval: float, limit: int, requeue_after: int, once: bool) -> dict:
    """按间隔反复领取并投递 Outbox；返回累计计数。"""
    totals = {"rounds": 0, "picked": 0, "delivered": 0, "retried": 0, "unknown": 0, "requeued": 0}
    while not _STOP:
        with session_scope() as session:
            totals["requeued"] += requeue_stuck(session, older_than_seconds=requeue_after)
            result = process_pending(session, limit=limit)
        totals["rounds"] += 1
        for key in ("picked", "delivered", "retried", "unknown"):
            totals[key] += int(result.get(key, 0))
        if result.get("picked"):
            logger.info(
                "outbox round=%s picked=%s delivered=%s retried=%s unknown=%s",
                totals["rounds"],
                result.get("picked"),
                result.get("delivered"),
                result.get("retried"),
                result.get("unknown"),
            )
        if once:
            break
        time.sleep(interval)
    return totals


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FinPilot Outbox 常驻工作者")
    parser.add_argument("--interval", type=float, default=5.0, help="轮询间隔秒（默认 5）")
    parser.add_argument("--limit", type=int, default=50, help="每轮最多领取条数（默认 50）")
    parser.add_argument("--requeue-after", type=int, default=300, help="超过该秒数未投递则重排")
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
    except (AttributeError, ValueError):  # pragma: no cover - 部分平台无 SIGTERM
        pass

    bootstrap()

    totals = run_loop(
        interval=args.interval,
        limit=args.limit,
        requeue_after=args.requeue_after,
        once=args.once,
    )
    logger.info("outbox 工作者退出：%s", totals)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    sys.exit(main())