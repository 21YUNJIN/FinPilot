import type { ReactElement } from "react";
import { DEFAULT_CURRENCY, formatMinorToYuan, parseMinor, type MinorInput } from "../format/money";
import { cx } from "../lib/cx";

/**
 * 金额展示（整数分 → 元，两位小数，禁用浮点）。
 *
 * 规则（INTERFACES.md §1、V5 4.1）：
 * - 入参必须是整数分；string/bigint 直接走 BigInt，number 必须是安全整数；
 * - null / undefined / 空串显示"不适用"（NA 规则），不显示 0；
 * - 非法输入显示"金额不合法"并带上错误码，绝不猜一个数字。
 */
export interface MoneyTextProps {
  amountMinor: MinorInput;
  currency?: string;
  /** 缺失时的文案，默认"不适用"。 */
  fallback?: string;
  className?: string;
  /** 是否显示币种符号（默认显示）。 */
  showCurrency?: boolean;
  "data-testid"?: string;
}

export function MoneyText({
  amountMinor,
  currency = DEFAULT_CURRENCY,
  fallback = "不适用",
  className,
  showCurrency = true,
  "data-testid": dataTestId,
}: MoneyTextProps): ReactElement {
  const missing = amountMinor === null || amountMinor === undefined || amountMinor === "";
  if (missing) {
    return (
      <span className={cx("fp-money", "fp-money--na", className)} data-amount-state="na" data-testid={dataTestId}>
        {fallback}
      </span>
    );
  }

  try {
    const minor = parseMinor(amountMinor);
    const yuan = formatMinorToYuan(minor);
    const symbol = currency === "CNY" ? "¥" : `${currency} `;
    const text = showCurrency ? `${symbol}${yuan}` : yuan;
    const readable = currency === "CNY" ? `${yuan} 元人民币` : `${yuan} ${currency}`;
    return (
      <span
        className={cx("fp-money", className)}
        data-amount-state="ok"
        data-minor={minor.toString()}
        data-currency={currency}
        data-testid={dataTestId}
        aria-label={readable}
      >
        {text}
      </span>
    );
  } catch (error) {
    const code = (error as { code?: string }).code ?? "MONEY_INVALID";
    return (
      <span
        className={cx("fp-money", "fp-money--invalid", className)}
        data-amount-state="invalid"
        data-amount-error={code}
        data-testid={dataTestId}
        role="status"
      >
        金额不合法
      </span>
    );
  }
}