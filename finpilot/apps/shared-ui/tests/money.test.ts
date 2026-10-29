import { describe, expect, it } from "vitest";
import {
  formatMinorToYuan,
  formatMoney,
  formatPercent,
  formatSharesE8,
  MoneyFormatError,
  parseMinor,
  yuanToMinor,
} from "../src/format/money";

describe("金额：整数分 → 元（禁止浮点误差）", () => {
  it("整数分格式化为两位小数并带千分位", () => {
    expect(formatMinorToYuan(1234567)).toBe("12,345.67");
    expect(formatMinorToYuan(0)).toBe("0.00");
    expect(formatMinorToYuan(5)).toBe("0.05");
    expect(formatMinorToYuan(50)).toBe("0.50");
    expect(formatMinorToYuan(100)).toBe("1.00");
    expect(formatMinorToYuan(100000000)).toBe("1,000,000.00");
    expect(formatMinorToYuan(-500)).toBe("-5.00");
    expect(formatMinorToYuan(-123456)).toBe("-1,234.56");
  });

  it("超过 JS 安全整数的金额用字符串/BigInt 传递，不产生浮点误差", () => {
    // 9007199254740993 作为 number 会丢精度，这里必须以字符串形式传入。
    expect(formatMinorToYuan("9007199254740993")).toBe("90,071,992,547,409.93");
    expect(formatMinorToYuan(9007199254740993n)).toBe("90,071,992,547,409.93");
    expect(formatMinorToYuan("123456789012345678")).toBe("1,234,567,890,123,456.78");
    expect(() => formatMinorToYuan(9007199254740993)).toThrow(MoneyFormatError);
  });

  it("非法金额不猜测数字：小数分、非数字、NaN 全部报错", () => {
    expect(() => parseMinor(12.5)).toThrowError(/整数分/);
    expect(() => parseMinor("12.34")).toThrowError(/整数分/);
    expect(() => parseMinor(Number.NaN)).toThrowError(/有限数值/);
    expect(() => parseMinor("abc")).toThrowError(/整数分/);
  });

  it("缺失金额显示'不适用'，非法金额不显示为 0", () => {
    expect(formatMoney(null)).toBe("不适用");
    expect(formatMoney(undefined)).toBe("不适用");
    expect(formatMoney(123456)).toBe("¥1,234.56");
    expect(formatMoney(123456, { currency: "USD" })).toBe("$1,234.56");
    expect(formatMoney(123456, { currency: "SGD" })).toBe("SGD 1,234.56");
    expect(formatMoney("abc")).toBe("不适用");
  });

  it("用户输入的元 → 整数分，最多两位小数", () => {
    expect(yuanToMinor("300")).toBe(30000n);
    expect(yuanToMinor("300.5")).toBe(30050n);
    expect(yuanToMinor("300.55")).toBe(30055n);
    expect(yuanToMinor("¥1,000.00")).toBe(100000n);
    expect(yuanToMinor("0.1")).toBe(10n);
    expect(yuanToMinor("0.07")).toBe(7n);
    expect(() => yuanToMinor("300.555")).toThrowError(/两位小数/);
    expect(() => yuanToMinor("三百")).toThrowError(/两位小数/);
  });

  it("份额（1e-8 定点）与百分比格式化", () => {
    expect(formatSharesE8(100000000)).toBe("1");
    expect(formatSharesE8(150000000)).toBe("1.5");
    expect(formatSharesE8(1)).toBe("0.00000001");
    expect(formatPercent(0.1234)).toBe("12.34%");
    expect(formatPercent(null)).toBe("不适用");
  });
});