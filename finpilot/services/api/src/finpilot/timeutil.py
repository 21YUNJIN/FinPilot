"""时间口径：持久化 UTC，业务日与日累计使用 Asia/Shanghai（V5 第 4.1 节）。

测试使用可注入时钟；相对日期必须基于任务的 reference_time 解析，
恢复任务时不得重新解释“明天”。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

UTC = timezone.utc
SHANGHAI = ZoneInfo("Asia/Shanghai")

ISO_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def now_utc() -> datetime:
    return datetime.now(tz=UTC)


def to_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("naive datetime 不允许进入持久层，必须带时区")
    return value.astimezone(UTC)


def to_shanghai(value: datetime) -> datetime:
    return to_utc(value).astimezone(SHANGHAI)


def business_day(value: datetime | None = None) -> date:
    """业务日（Asia/Shanghai）。

    必须走可注入时钟：日累计、定时任务与测试都依赖同一时间源，
    否则冻结时钟下会出现额度桶落在真实日期、而校验落在参考日期的分叉。
    """
    return to_shanghai(value or utcnow()).date()


def business_day_start_utc(day: date) -> datetime:
    local = datetime.combine(day, time(0, 0, 0), tzinfo=SHANGHAI)
    return local.astimezone(UTC)


def parse_iso(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间字符串必须带时区偏移")
    return parsed.astimezone(UTC)


def format_iso(value: datetime) -> str:
    """统一带时区 ISO 8601（默认以上海时区呈现，存储仍为 UTC）。"""
    return to_shanghai(value).isoformat(timespec="seconds")


def format_iso_utc(value: datetime) -> str:
    return to_utc(value).isoformat(timespec="seconds")


@dataclass
class Clock:
    """可注入时钟。"""

    frozen_at: datetime | None = None

    def now(self) -> datetime:
        if self.frozen_at is not None:
            return self.frozen_at
        return now_utc()

    def today(self) -> date:
        return business_day(self.now())

    def advance(self, seconds: float) -> None:
        if self.frozen_at is None:
            self.frozen_at = self.now()
        self.frozen_at = self.frozen_at + timedelta(seconds=seconds)

    def freeze(self, at: datetime) -> None:
        self.frozen_at = to_utc(at)

    def unfreeze(self) -> None:
        self.frozen_at = None


_clock = Clock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    global _clock
    _clock = clock


def reset_clock() -> None:
    global _clock
    _clock = Clock()


def utcnow() -> datetime:
    """全局当前时间（受可注入时钟控制）。"""
    return _clock.now()


def current_business_day() -> date:
    return business_day(_clock.now())


def humanize_deadline(deadline: datetime, reference: datetime | None = None) -> str:
    reference = reference or _clock.now()
    delta = to_utc(deadline) - to_utc(reference)
    total_minutes = int(delta.total_seconds() // 60)
    if total_minutes <= 0:
        return "已到期"
    days, rem_minutes = divmod(total_minutes, 60 * 24)
    hours, minutes = divmod(rem_minutes, 60)
    parts = []
    if days:
        parts.append(f"{days} 天")
    if hours:
        parts.append(f"{hours} 小时")
    if minutes and not days:
        parts.append(f"{minutes} 分钟")
    return "".join(parts)