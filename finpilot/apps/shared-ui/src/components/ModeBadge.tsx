import type { ReactElement } from "react";
import { APP_MODE_LABELS } from "../contract/labels";
import { cx } from "../lib/cx";

/**
 * 运行模式徽标（NFR-04：在线 / 离线 / 模拟必须明确标识，依赖失败不得伪造结果）。
 *
 * 规则：
 * - 只认 `settings.app_mode ∈ {live, offline_replay, mock}`；
 * - 缺失或非法值显示"未标注 / 未知模式"，并明确写"不得当作实时运行"；
 * - 永不默认成 live，也不因缺少字段而隐藏徽标。
 */
export interface ModeBadgeProps {
  /** 运行模式，来自信封 provenance.mode 或任务视图 mode。 */
  mode?: string | null;
  /** 适配器标识，例如 finpilot-api / bank-sandbox。 */
  adapter?: string | null;
  /** 观测时间（ISO-8601）；原样展示在 title 中，便于与服务端日志核对。 */
  observedAt?: string | null;
  size?: "sm" | "md";
  /** 数据来源说明，例如"服务端 provenance"。 */
  source?: string;
  className?: string;
  "data-testid"?: string;
}

const MODE_MARKS: Readonly<Record<string, string>> = {
  live: "●",
  offline_replay: "⟲",
  mock: "◇",
};

export function modeBadgeText(mode?: string | null): string {
  if (mode === null || mode === undefined || String(mode).trim() === "") {
    return "运行模式未标注（不得当作实时运行）";
  }
  const key = String(mode).trim();
  return APP_MODE_LABELS[key] ?? `未知运行模式（${key}）：不得当作实时运行`;
}

export function ModeBadge({
  mode,
  adapter,
  observedAt,
  size = "md",
  source,
  className,
  "data-testid": dataTestId = "mode-badge",
}: ModeBadgeProps): ReactElement {
  const key = mode === null || mode === undefined ? "unlabeled" : String(mode).trim() || "unlabeled";
  const text = modeBadgeText(mode);
  const details = [
    adapter ? `适配器：${adapter}` : null,
    observedAt ? `观测时间：${observedAt}` : null,
    source ? `来源：${source}` : null,
  ].filter((item): item is string => item !== null);

  return (
    <span
      className={cx("fp-mode-badge", `fp-mode-badge--${size}`, `fp-mode-badge--${key}`, className)}
      data-mode={key}
      data-testid={dataTestId}
      role="status"
      aria-live="polite"
      title={details.length > 0 ? details.join("；") : undefined}
    >
      <span aria-hidden="true" className="fp-mode-badge__mark">
        {MODE_MARKS[key] ?? "?"}
      </span>
      <span className="fp-mode-badge__text">{text}</span>
    </span>
  );
}