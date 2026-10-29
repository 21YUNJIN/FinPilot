"""独立提供方沙箱：故障注入（V5 第 7.2 节 / T21、T54）。

支持五类故障，全部持久化在提供方自己的状态库中（进程重启后仍然生效）：

===================  ====================================================================
``delay_ms``         应答前等待 N 毫秒（模拟慢提供方）。等待上限受 ``SANDBOX_MAX_DELAY_MS``
                     限制，超过部分不再真实等待，但报告中如实标注 ``delay_clipped``。
``drop_response``    业务照常受理并落库，但把应答变成超时语义（HTTP 504 +
                     ``PROVIDER_TIMEOUT``）——模拟“外部已受理但应答丢失”。客户端必须用
                     同一个稳定业务键查询，不能凭本地超时判定失败。
``unavailable``      所有业务端点返回 503 ``PROVIDER_UNAVAILABLE``，且不产生任何状态变化；
                     健康检查与故障管理端点仍然可用（否则故障无法清除）。
``reject_next``      接下来 N 次“新”提交返回可验证的业务拒绝（``status=failed`` /
                     ``PROVIDER_REJECTED``）并落库，重复同键提交返回同一拒绝结果。
``query_fail_times`` 接下来 N 次查询返回 503 ``PROVIDER_QUERY_FAILED``，不改变权威状态——
                     模拟“查询失败不等于业务失败”，禁止据此把结果写成失败。
===================  ====================================================================

计数型故障（``reject_next`` / ``query_fail_times``）每次命中真实扣减一次，扣减与业务写
在同一 ``BEGIN IMMEDIATE`` 事务里，保证并发下不会多送或少送。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

try:
    import store as store_mod  # type: ignore
except ImportError:  # pragma: no cover - 以包方式导入时的回退
    from . import store as store_mod  # type: ignore

FAULT_FIELDS = ("delay_ms", "drop_response", "unavailable", "reject_next", "query_fail_times")
MAX_COUNTER = 100
DEFAULT_MAX_DELAY_MS = 5000


class FaultConfigError(ValueError):
    """故障配置非法（未知字段或类型错误）。"""

    def __init__(self, message: str, *, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.details = dict(details or {})


@dataclass
class FaultConfig:
    delay_ms: int = 0
    drop_response: bool = False
    unavailable: bool = False
    reject_next: int = 0
    query_fail_times: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise FaultConfigError(f"{name} must be a boolean", details={"field": name, "value": value})


def _as_count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FaultConfigError(f"{name} must be a non-negative integer", details={"field": name, "value": value})
    if value < 0 or value > MAX_COUNTER:
        raise FaultConfigError(
            f"{name} must be between 0 and {MAX_COUNTER}", details={"field": name, "value": value}
        )
    return int(value)


def normalize_faults(payload: Mapping[str, Any]) -> dict[str, Any]:
    """校验并规范化故障片段；未知字段直接拒绝（不静默忽略）。"""
    if not isinstance(payload, Mapping):
        raise FaultConfigError("faults body must be a JSON object")
    unknown = [key for key in payload if key not in FAULT_FIELDS]
    if unknown:
        raise FaultConfigError(f"unknown fault fields: {sorted(unknown)}", details={"unknown": sorted(unknown)})
    result: dict[str, Any] = {}
    for key, value in payload.items():
        if key == "delay_ms":
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise FaultConfigError(
                    "delay_ms must be a non-negative integer", details={"field": key, "value": value}
                )
            result[key] = int(value)
        elif key in ("drop_response", "unavailable"):
            result[key] = _as_bool(value, key)
        else:
            result[key] = _as_count(value, key)
    return result


def max_delay_ms() -> int:
    raw = os.environ.get("SANDBOX_MAX_DELAY_MS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_DELAY_MS
    except ValueError:  # pragma: no cover - 环境变量写错时退回默认
        value = DEFAULT_MAX_DELAY_MS
    return max(0, value)


class FaultInjector:
    """围绕状态库的故障注入器；可注入 ``sleep`` 便于测试。"""

    def __init__(
        self,
        sandbox_store: "store_mod.SandboxStore",
        *,
        sleep: Callable[[float], None] = time.sleep,
        delay_cap_ms: int | None = None,
    ) -> None:
        self.store = sandbox_store
        self._sleep = sleep
        self._delay_cap_ms = delay_cap_ms

    # -- 读 ----------------------------------------------------------------

    def current(self) -> dict[str, Any]:
        config = self.store.read_faults()
        view = {key: config.get(key, store_mod.FAULT_DEFAULTS[key]) for key in FAULT_FIELDS}
        view["delay_cap_ms"] = self.delay_cap()
        view["delay_clipped"] = int(view["delay_ms"]) > view["delay_cap_ms"]
        view["effective_delay_ms"] = min(int(view["delay_ms"]), view["delay_cap_ms"])
        view["provider_id"] = self.store.provider_id
        view["observed_at"] = store_mod.now_iso()
        return view

    def delay_cap(self) -> int:
        return self._delay_cap_ms if self._delay_cap_ms is not None else max_delay_ms()

    # -- 写 ----------------------------------------------------------------

    def update(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """合并式更新：只覆盖请求中出现的字段。"""
        patch = normalize_faults(payload)
        if not patch:
            return self.current()
        config = self.store.read_faults()
        config.update(patch)
        self.store.write_faults(config)
        return self.current()

    def replace(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """全量覆盖：未出现的字段回到默认值。"""
        patch = normalize_faults(payload)
        config = dict(store_mod.FAULT_DEFAULTS)
        config.update(patch)
        self.store.write_faults(config)
        return self.current()

    def clear(self) -> dict[str, Any]:
        self.store.write_faults(dict(store_mod.FAULT_DEFAULTS))
        return self.current()

    # -- 行为 --------------------------------------------------------------

    def wait(self) -> int:
        """按 ``delay_ms`` 等待（受上限约束），返回真实等待毫秒数。"""
        config = self.store.read_faults()
        requested = int(config.get("delay_ms") or 0)
        effective = min(requested, self.delay_cap())
        if effective > 0:
            self._sleep(effective / 1000.0)
        return effective

    def is_unavailable(self) -> bool:
        return bool(self.store.read_faults().get("unavailable"))

    def drops_response(self) -> bool:
        return bool(self.store.read_faults().get("drop_response"))

    def take_reject(self, conn: Any = None) -> bool:
        """命中一次“拒绝下一次提交”。``conn`` 传入时复用调用方的写事务。"""
        return self.store.consume_counter("reject_next", conn)

    def take_query_failure(self, conn: Any = None) -> bool:
        """命中一次“查询失败”。``conn`` 传入时复用调用方的写事务。"""
        return self.store.consume_counter("query_fail_times", conn)


__all__ = [
    "FAULT_FIELDS",
    "FaultConfig",
    "FaultConfigError",
    "FaultInjector",
    "MAX_COUNTER",
    "max_delay_ms",
    "normalize_faults",
]