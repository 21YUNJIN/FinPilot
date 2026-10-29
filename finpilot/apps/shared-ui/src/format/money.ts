/**
 * 金额格式化：后端只传整数分（`amount_minor: int`）。
 *
 * 硬约束（INTERFACES.md §1、V5 4.1）：
 * - 前端禁止用浮点做金额运算，一律用 BigInt 做整数分 → 元的字符串拼接，
 *   避免 `0.1 + 0.2`、`1234.565 * 100` 这类精度误差。
 * - 展示固定两位小数、千分位分隔；币种固定 CNY，其他币种按原样显示代码。
 * - 金额缺失/为 null 时显示"不适用"（NA 规则），绝不显示 0 或空白。
 */

export const DEFAULT_CURRENCY = "CNY";
export const NA_TEXT = "不适用";

const CURRENCY_SYMBOLS: Readonly<Record<string, string>> = {
  CNY: "¥",
  USD: "$",
  EUR: "€",
  HKD: "HK$",
};

export type MinorInput = number | string | bigint | null | undefined;

export class MoneyFormatError extends Error {
  readonly code: string;

  constructor(code: string, message: string) {
    super(message);
    this.name = "MoneyFormatError";
    this.code = code;
  }
}

/** 严格的整数分解析：只接受整数分（number 必须为安全整数；string 必须匹配 ±\d+）。 */
export function parseMinor(value: MinorInput): bigint {
  if (value === null || value === undefined || value === "") {
    throw new MoneyFormatError("MONEY_MISSING", "金额缺失");
  }
  if (typeof value === "bigint") return value;
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new MoneyFormatError("MONEY_NOT_FINITE", "金额不是有限数值");
    }
    if (!Number.isInteger(value)) {
      throw new MoneyFormatError("AMOUNT_PRECISION", "金额必须是整数分（不允许小数分）");
    }
    if (!Number.isSafeInteger(value)) {
      throw new MoneyFormatError("MONEY_UNSAFE_INTEGER", "金额超出安全整数范围，请以字符串传递");
    }
    return BigInt(value);
  }
  const text = String(value).trim().replace(/,/g, "");
  if (!/^[+-]?\d+$/.test(text)) {
    throw new MoneyFormatError("AMOUNT_PRECISION", `金额必须是整数分，收到 ${JSON.stringify(value)}`);
  }
  return BigInt(text);
}

function groupThousands(digits: string): string {
  let out = "";
  for (let i = 0; i < digits.length; i += 1) {
    const remaining = digits.length - i;
    out += digits[i] as string;
    if (remaining > 1 && remaining % 3 === 1) out += ",";
  }
  return out;
}

/** 整数分 → "12,345.67"（无币种符号）。 */
export function formatMinorToYuan(value: MinorInput): string {
  const minor = parseMinor(value);
  const negative = minor < 0n;
  const abs = negative ? -minor : minor;
  const yuan = abs / 100n;
  const cents = abs % 100n;
  const body = `${groupThousands(yuan.toString())}.${cents.toString().padStart(2, "0")}`;
  return negative ? `-${body}` : body;
}

/** 整数分 → "¥12,345.67"；null/undefined → "不适用"。 */
export function formatMoney(value: MinorInput, options: { currency?: string; fallback?: string } = {}): string {
  const currency = options.currency ?? DEFAULT_CURRENCY;
  const fallback = options.fallback ?? NA_TEXT;
  if (value === null || value === undefined || value === "") return fallback;
  try {
    const amount = formatMinorToYuan(value);
    const symbol = CURRENCY_SYMBOLS[currency];
    return symbol ? `${symbol}${amount}` : `${currency} ${amount}`;
  } catch {
    return fallback;
  }
}

/** 用户输入的元 → 整数分（严格：最多两位小数）。用于表单，不用于展示。 */
export function yuanToMinor(input: string): bigint {
  const text = String(input ?? "").trim().replace(/[,\s¥]/g, "");
  if (!/^[+-]?(\d+)(\.\d{0,2})?$/.test(text)) {
    throw new MoneyFormatError("AMOUNT_PRECISION", "金额最多两位小数，请重新输入。");
  }
  const negative = text.startsWith("-");
  const unsigned = text.replace(/^[+-]/, "");
  const [yuanPart = "0", centPart = ""] = unsigned.split(".");
  const cents = (centPart + "00").slice(0, 2);
  const minor = BigInt(yuanPart) * 100n + BigInt(cents);
  const signed = negative ? -minor : minor;
  if (signed > BigInt(Number.MAX_SAFE_INTEGER) || signed < BigInt(Number.MIN_SAFE_INTEGER)) {
    throw new MoneyFormatError("MONEY_UNSAFE_INTEGER", "金额超出安全整数范围");
  }
  return signed;
}

/** 份额（1e-8 定点）→ 字符串，最多 8 位小数，去掉尾随 0。 */
export function formatSharesE8(value: MinorInput): string {
  const shares = parseMinor(value);
  const negative = shares < 0n;
  const abs = negative ? -shares : shares;
  const whole = abs / 100000000n;
  const fraction = (abs % 100000000n).toString().padStart(8, "0").replace(/0+$/, "");
  const body = fraction ? `${whole}.${fraction}` : whole.toString();
  return negative ? `-${body}` : body;
}

/** 百分数展示（入参是已经算好的数值，例如 0.1234 → "12.34%"）。 */
export function formatPercent(ratio: number | null | undefined, digits = 2): string {
  if (ratio === null || ratio === undefined || !Number.isFinite(ratio)) return NA_TEXT;
  return `${(ratio * 100).toFixed(digits)}%`;
}