import { describe, expect, it } from "vitest";
import {
  DISPLAY_TIME_ZONE,
  formatCountdown,
  formatDate,
  formatDateTime,
  formatTimeWindow,
  secondsUntil,
} from "../src/format/datetime";

describe("时间：统一按 Asia/Shanghai 展示", () => {
  it("UTC 时间转换为上海时间展示", () => {
    expect(DISPLAY_TIME_ZONE).toBe("Asia/Shanghai");
    expect(formatDateTime("2026-10-27T08:00:00Z")).toBe("2026-10-27 16:00:00");
    expect(formatDateTime("2026-10-27T16:25:09+08:00")).toBe("2026-10-27 16:25:09");
    expect(formatDate("2026-10-27T08:00:00Z")).toBe("2026-10-27");
    // 跨日边界：UTC 20:00 是上海次日 04:00
    expect(formatDate("2026-10-27T20:00:00Z")).toBe("2026-10-28");
  });

  it("缺失或非法时间显示占位符，不显示 Invalid Date", () => {
    expect(formatDateTime(null)).toBe("—");
    expect(formatDateTime(undefined, "不适用")).toBe("不适用");
    expect(formatDateTime("not-a-date", "不适用")).toBe("不适用");
    expect(formatDateTime("")).toBe("—");
  });

  it("倒计时与剩余秒数用于验证有效期", () => {
    const now = Date.parse("2026-10-27T08:00:00Z");
    expect(secondsUntil("2026-10-27T08:02:00Z", now)).toBe(120);
    expect(secondsUntil("2026-10-27T07:59:00Z", now)).toBe(0);
    expect(secondsUntil("bad", now)).toBeNull();
    expect(formatCountdown(119)).toBe("01:59");
    expect(formatCountdown(0)).toBe("00:00");
    expect(formatCountdown(null)).toBe("--:--");
  });

  it("时间窗优先使用服务端 label，其次用起止点，缺失显示'不适用'", () => {
    expect(formatTimeWindow({ label: "2026-10-28 09:00—17:00（Asia/Shanghai）" })).toContain("2026-10-28");
    expect(formatTimeWindow({ start: "2026-10-27T16:00:00Z", end: "2026-10-27T17:00:00Z" })).toBe(
      "2026-10-28 00:00:00 至 2026-10-28 01:00:00（Asia/Shanghai）",
    );
    expect(formatTimeWindow(null)).toBe("不适用");
    expect(formatTimeWindow("")).toBe("不适用");
  });
});