/**
 * 时间展示：后端持久化 UTC（timestamptz），展示统一用 Asia/Shanghai
 * （对应后端 timeutil.format_iso 的口径）。
 * 不允许前端改用浏览器本地时区，否则两客户端会显示不同时间。
 */

export const DISPLAY_TIME_ZONE = "Asia/Shanghai";

const partsCache = new Map<string, Intl.DateTimeFormat>();

function formatterFor(options: Intl.DateTimeFormatOptions): Intl.DateTimeFormat {
  const key = JSON.stringify(options);
  const cached = partsCache.get(key);
  if (cached) return cached;
  const created = new Intl.DateTimeFormat("en-CA", { timeZone: DISPLAY_TIME_ZONE, ...options });
  partsCache.set(key, created);
  return created;
}

interface Parts {
  year: string;
  month: string;
  day: string;
  hour: string;
  minute: string;
  second: string;
}

function toParts(date: Date): Parts | null {
  if (Number.isNaN(date.getTime())) return null;
  const parts = formatterFor({
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).formatToParts(date);
  const pick = (type: string): string => parts.find((p) => p.type === type)?.value ?? "";
  return {
    year: pick("year"),
    month: pick("month"),
    day: pick("day"),
    hour: pick("hour"),
    minute: pick("minute"),
    second: pick("second"),
  };
}

function parse(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined || value === "") return null;
  if (value instanceof Date) return value;
  if (typeof value === "number") return new Date(value);
  const text = String(value).trim();
  // 后端以 ISO-8601 传输；对缺失时区的最小支持：按 UTC 解释。
  const normalized = /(?:Z|[+-]\d{2}:?\d{2})$/.test(text) ? text : `${text}Z`;
  const date = new Date(normalized);
  return Number.isNaN(date.getTime()) ? null : date;
}

/** "2026-10-27 16:25:09"（Asia/Shanghai）。 */
export function formatDateTime(value: string | number | Date | null | undefined, fallback = "—"): string {
  const date = parse(value);
  if (!date) return fallback;
  const p = toParts(date);
  if (!p) return fallback;
  return `${p.year}-${p.month}-${p.day} ${p.hour}:${p.minute}:${p.second}`;
}

/** "2026-10-27"。 */
export function formatDate(value: string | number | Date | null | undefined, fallback = "—"): string {
  const date = parse(value);
  if (!date) return fallback;
  const p = toParts(date);
  if (!p) return fallback;
  return `${p.year}-${p.month}-${p.day}`;
}

/** "16:25:09"。 */
export function formatTime(value: string | number | Date | null | undefined, fallback = "—"): string {
  const date = parse(value);
  if (!date) return fallback;
  const p = toParts(date);
  if (!p) return fallback;
  return `${p.hour}:${p.minute}:${p.second}`;
}

/** 距目标时刻还剩多少秒（已过期返回 0，无法解析返回 null）。 */
export function secondsUntil(
  value: string | number | Date | null | undefined,
  now: number = Date.now(),
): number | null {
  const date = parse(value);
  if (!date) return null;
  return Math.max(0, Math.floor((date.getTime() - now) / 1000));
}

/** 秒 → "01:59"（用于验证有效期倒计时）。 */
export function formatCountdown(totalSeconds: number | null | undefined): string {
  if (totalSeconds === null || totalSeconds === undefined || !Number.isFinite(totalSeconds)) return "--:--";
  const safe = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safe / 60);
  const seconds = safe % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

/** 时间窗展示：优先使用后端给的 label，缺失时用起止点拼装。 */
export function formatTimeWindow(
  window:
    | { start?: string | null; end?: string | null; label?: string }
    | string
    | null
    | undefined,
  fallback = "不适用",
): string {
  if (!window) return fallback;
  if (typeof window === "string") return window.trim() === "" ? fallback : window;
  if (window.label && window.label.trim() !== "") return window.label;
  const start = window.start ? formatDateTime(window.start, "") : "";
  const end = window.end ? formatDateTime(window.end, "") : "";
  if (start && end) return `${start} 至 ${end}（Asia/Shanghai）`;
  if (start) return `${start} 起（Asia/Shanghai）`;
  if (end) return `至 ${end}（Asia/Shanghai）`;
  return fallback;
}