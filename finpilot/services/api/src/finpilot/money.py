"""金额、份额与分摊的确定性计算（V5 第 4.1、8、9 节）。

约定：
- 银行金额一律整数人民币分 amount_minor；币种固定 CNY。
- 输入小数最多两位，超精度要求用户修正，禁止浮点隐式取整。
- 份额用 1e-8 定点整数（shares_e8）表示，避免浮点漂移。
- 分摊使用最大余数法，合计严格等于总额。
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

from .errors import FinPilotError

CURRENCY = "CNY"
MINOR_SCALE = 2
SHARE_SCALE = 8
SHARES_E8 = 10**SHARE_SCALE

_AMOUNT_RE = re.compile(r"^\s*[+-]?\d+(\.\d+)?\s*$")


def parse_amount_minor(value: int | str | Decimal, *, allow_zero: bool = False, allow_negative: bool = False) -> int:
    """把用户输入解析为整数分。

    支持 int（视为分）、str/Decimal（视为元）。超过两位小数直接报错。
    """
    if isinstance(value, bool):
        raise FinPilotError("PARAM_INVALID", "布尔值不能作为金额。")
    if isinstance(value, int):
        minor = value
    else:
        text = str(value).strip().replace(",", "").replace("，", "")
        text = text.replace("元", "").replace("¥", "").replace("￥", "")
        if not _AMOUNT_RE.match(text):
            raise FinPilotError("PARAM_INVALID", "金额格式无法识别，请输入数字，最多两位小数。")
        try:
            dec = Decimal(text)
        except InvalidOperation as exc:  # pragma: no cover - 正则已保证
            raise FinPilotError("PARAM_INVALID", "金额无法解析。") from exc
        exponent = -dec.as_tuple().exponent
        if exponent > MINOR_SCALE:
            raise FinPilotError(
                "AMOUNT_PRECISION",
                "金额最多两位小数（分），请修正后重新提交。",
                details={"reason_codes": ["more_than_two_decimals"]},
            )
        minor = int((dec * 100).to_integral_value())

    if minor < 0 and not allow_negative:
        raise FinPilotError("PARAM_INVALID", "金额不能为负；退款请使用独立的退款操作。")
    if minor == 0 and not allow_zero:
        raise FinPilotError("PARAM_INVALID", "金额必须大于 0 元。")
    return minor


def format_minor(minor: int) -> str:
    """整数分 → 两位小数字符串。"""
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def yuan(minor: int) -> Decimal:
    return Decimal(minor) / Decimal(100)


def parse_shares_e8(value: str | Decimal | int) -> int:
    """份额定点解析，最多 8 位小数。"""
    dec = Decimal(str(value))
    exponent = -dec.as_tuple().exponent
    if exponent > SHARE_SCALE:
        raise FinPilotError("PARAM_INVALID", "份额最多八位小数。")
    return int((dec * SHARES_E8).to_integral_value())


def format_shares_e8(shares_e8: int) -> str:
    sign = "-" if shares_e8 < 0 else ""
    shares_e8 = abs(int(shares_e8))
    whole, frac = divmod(shares_e8, SHARES_E8)
    frac_text = f"{frac:08d}".rstrip("0")
    return f"{sign}{whole}" + (f".{frac_text}" if frac_text else "")


def largest_remainder_split(total_minor: int, parts: int) -> list[int]:
    """整数分按最大余数法分成 parts 份，合计严格等于 total_minor。

    100 元三人 → 33.34 / 33.33 / 33.33（余数按稳定顺序优先分配）。
    """
    if parts <= 0:
        raise FinPilotError("PARAM_INVALID", "分摊人数必须大于 0。")
    if total_minor < 0:
        raise FinPilotError("PARAM_INVALID", "分摊总额不能为负。")
    base, remainder = divmod(total_minor, parts)
    return [base + (1 if i < remainder else 0) for i in range(parts)]


def largest_remainder_split_weights(total_minor: int, weights: list[int]) -> list[int]:
    """按权重使用最大余数法分摊，合计严格等于总额。"""
    if not weights:
        raise FinPilotError("PARAM_INVALID", "缺少分摊权重。")
    if any(w < 0 for w in weights):
        raise FinPilotError("PARAM_INVALID", "分摊权重不能为负。")
    total_weight = sum(weights)
    if total_weight == 0:
        return largest_remainder_split(total_minor, len(weights))
    raw = [Decimal(total_minor) * Decimal(w) / Decimal(total_weight) for w in weights]
    floors = [int(r.to_integral_value(rounding="ROUND_FLOOR")) for r in raw]
    assigned = sum(floors)
    leftover = total_minor - assigned
    order = sorted(
        range(len(weights)),
        key=lambda i: (-(raw[i] - floors[i]), -weights[i], i),
    )
    for i in order[:leftover]:
        floors[i] += 1
    assert sum(floors) == total_minor, "分摊合计必须等于总额"
    return floors


def fee_for(amount_minor: int, fee_bps: int = 0, fee_fixed_minor: int = 0) -> int:
    """手续费：bps 按分计算并向下取整，另加固定费用。"""
    if fee_bps < 0 or fee_fixed_minor < 0:
        raise FinPilotError("PARAM_INVALID", "费率不能为负。")
    variable = (amount_minor * fee_bps) // 10_000
    return variable + fee_fixed_minor


def sum_minor(values: list[int]) -> int:
    return sum(int(v) for v in values)