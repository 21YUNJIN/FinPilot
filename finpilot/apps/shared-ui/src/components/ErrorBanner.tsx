import type { ReactElement, ReactNode } from "react";
import type { ApiFailure } from "../api/client";
import { errorMessageFor, retryModeHint, resolveUserMessage } from "../api/errorMessages";
import { cx } from "../lib/cx";

/**
 * 错误提示条（SRS 16.2 / NFR-04）。
 *
 * 规则：
 * - 只展示后端 `safe_message` 或 errors.py 镜像文案 + 可执行下一步，不展示堆栈；
 * - `status: unknown`（超时/结果未知）不能写成"失败"，只能说"结果未知，需要核对"；
 * - 需要查询原订单时，主按钮文案是"用原操作编号查询"，绝不诱导重复提交。
 */
export interface ErrorBannerProps {
  /** 来自 api/client 的失败对象（推荐直接传）。 */
  failure?: ApiFailure | null;
  code?: string | null;
  message?: string | null;
  retryMode?: string | null;
  hint?: string | null;
  title?: string;
  /** 场景说明，例如"确认计划"。 */
  context?: string;
  onRetry?: () => void;
  onDismiss?: () => void;
  retryLabel?: string;
  dismissLabel?: string;
  showCode?: boolean;
  className?: string;
  children?: ReactNode;
  "data-testid"?: string;
}

function titleFor(failure: ApiFailure | null, override?: string): string {
  if (override) return override;
  if (!failure) return "操作未完成";
  if (failure.status === "unknown" || failure.code === "RESULT_UNKNOWN" || failure.code === "PROVIDER_TIMEOUT") {
    return "结果未知（未确认成功，也未确认失败）";
  }
  if (failure.status === "rejected") return "操作被拒绝";
  if (failure.transport) return "请求未到达业务层";
  if (failure.status === "failed") return "操作失败";
  return "操作未完成";
}

export function ErrorBanner({
  failure,
  code,
  message,
  retryMode,
  hint,
  title,
  context,
  onRetry,
  onDismiss,
  retryLabel,
  dismissLabel = "关闭",
  showCode = true,
  className,
  children,
  "data-testid": dataTestId = "error-banner",
}: ErrorBannerProps): ReactElement {
  const effective: ApiFailure | null =
    failure ??
    (code || message
      ? (() => {
          const resolved = resolveUserMessage({ code: code ?? "SYSTEM_ERROR", serverMessage: message, retryMode });
          return {
            code: resolved.code,
            message: message ?? errorMessageFor(resolved.code),
            hint: hint ?? resolved.hint,
            retryMode: String(resolved.retryMode),
            knownCode: resolved.knownCode,
            requiresQueryOfOriginalOperation: resolved.requiresQueryOfOriginalOperation,
            httpStatus: null,
            status: null,
            reasonCodes: [],
            details: null,
            requestId: null,
            operationId: null,
            envelope: null,
            transport: false,
          } satisfies ApiFailure;
        })()
      : null);

  if (!effective) return <></>;

  const effectiveHint = hint ?? effective.hint ?? retryModeHint(effective.retryMode);
  const needsQuery = effective.requiresQueryOfOriginalOperation;
  const actionLabel = retryLabel ?? (needsQuery ? "用原操作编号查询" : "重试");

  return (
    <div
      className={cx("fp-error-banner", needsQuery && "fp-error-banner--unknown", className)}
      role="alert"
      aria-live="assertive"
      data-code={effective.code}
      data-status={effective.status ?? "none"}
      data-testid={dataTestId}
    >
      <p className="fp-error-banner__title">{titleFor(effective, title)}</p>
      {context ? <p className="fp-error-banner__context">场景：{context}</p> : null}
      <p className="fp-error-banner__message">{effective.message}</p>
      {effectiveHint ? <p className="fp-error-banner__hint">{effectiveHint}</p> : null}
      {needsQuery ? (
        <p className="fp-error-banner__query">
          请勿重复提交：系统会用原操作编号（幂等键）核对真实结果，重复提交可能造成两笔业务。
        </p>
      ) : null}
      {showCode ? (
        <p className="fp-error-banner__code">
          错误码：<code>{effective.code}</code>
          {effective.reasonCodes.length > 0 ? <> · 原因码：{effective.reasonCodes.join("、")}</> : null}
          {effective.operationId ? <> · 操作编号：<code>{effective.operationId}</code></> : null}
        </p>
      ) : null}
      {children}
      {onRetry || onDismiss ? (
        <div className="fp-error-banner__actions">
          {onRetry ? (
            <button type="button" className="fp-button fp-button--primary" onClick={onRetry}>
              {actionLabel}
            </button>
          ) : null}
          {onDismiss ? (
            <button type="button" className="fp-button" onClick={onDismiss}>
              {dismissLabel}
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}