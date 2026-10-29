import type { ReactElement } from "react";
import { cx } from "../lib/cx";

/**
 * 状态标签（SRS 18.2：每一步标明真实状态；风险状态不得只用颜色表示）。
 *
 * 规则：
 * - 必须有可见文字；label 为空时显示"状态未标注"，绝不渲染空标签；
 * - 颜色只是辅助，图标 aria-hidden，语义由文字承担；
 * - `rawValue` 保留后端原值，便于排查与审计对齐。
 */
export type StatusTone = "neutral" | "info" | "success" | "warning" | "danger" | "pending";

export interface StatusChipProps {
  label: string;
  tone?: StatusTone;
  /** 读屏前缀，如"任务状态" → aria-label="任务状态：已完成"。 */
  srPrefix?: string;
  /** 后端原始状态字符串，显示在 title 与 data 属性上。 */
  rawValue?: string | null;
  title?: string;
  className?: string;
  "data-testid"?: string;
}

const TONE_MARKS: Readonly<Record<StatusTone, string>> = {
  neutral: "•",
  info: "i",
  success: "✓",
  warning: "!",
  danger: "×",
  pending: "…",
};

export function StatusChip({
  label,
  tone = "neutral",
  srPrefix,
  rawValue,
  title,
  className,
  "data-testid": dataTestId,
}: StatusChipProps): ReactElement {
  const safeLabel = typeof label === "string" && label.trim() !== "" ? label : "状态未标注";
  const accessibleName = srPrefix ? `${srPrefix}：${safeLabel}` : safeLabel;
  const titleText = title ?? (rawValue ? `后端原始状态：${rawValue}` : undefined);

  return (
    <span
      className={cx("fp-chip", `fp-chip--${tone}`, className)}
      data-tone={tone}
      data-raw-status={rawValue ?? undefined}
      data-testid={dataTestId}
      title={titleText}
      role="status"
      aria-label={accessibleName}
    >
      <span aria-hidden="true" className="fp-chip__mark">
        {TONE_MARKS[tone]}
      </span>
      <span className="fp-chip__label">{safeLabel}</span>
    </span>
  );
}

/** 状态 → 视觉色调的**辅助**映射；文字标签始终是主要信息通道。 */
export function toneForStatus(status: string | null | undefined): StatusTone {
  switch ((status ?? "").trim().toLowerCase()) {
    case "succeeded":
    case "settled":
    case "completed":
    case "active":
    case "approved":
      return "success";
    case "partially_succeeded":
    case "awaiting_confirmation":
    case "awaiting_authorization":
    case "pending":
    case "pending_cancellation":
    case "pending_revocation":
    case "running":
    case "dispatching":
    case "submitted":
    case "awaiting_result":
    case "scheduled":
    case "queued":
      return "pending";
    case "failed":
    case "rejected":
    case "lost":
    case "blocked":
      return "danger";
    case "unknown":
    case "result_unknown":
    case "missed":
      return "warning";
    case "cancelled":
    case "stopped":
    case "revoked":
    case "closed":
    case "ended":
    case "expired":
      return "neutral";
    default:
      return "info";
  }
}