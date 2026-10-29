import type { ReactElement } from "react";
import type { LocalConfirmedAction, VersionConflictDetail } from "../types";
import { formatDateTime } from "../format/datetime";
import { cx } from "../lib/cx";

/**
 * 版本冲突提示（NFR-03 / A01：两客户端同时修改任务时提示版本冲突并刷新，不丢弃一方已确认动作）。
 *
 * 规则：
 * - 明确写出"你基于哪个修订、服务端已更新"；
 * - 说明刷新**不会**丢弃已确认动作，并逐条列出被保留的本地动作；
 * - 只提供"刷新到最新修订"，不提供"强制覆盖"（避免用旧修订覆盖服务端结论）。
 */
export interface VersionConflictNoticeProps {
  conflict: VersionConflictDetail;
  onRefresh: () => void;
  onDismiss?: () => void;
  /** 本地已确认但尚未反映到服务端视图的动作，刷新后仍然保留。 */
  pendingActions?: LocalConfirmedAction[];
  refreshing?: boolean;
  className?: string;
  "data-testid"?: string;
}

export function VersionConflictNotice({
  conflict,
  onRefresh,
  onDismiss,
  pendingActions = [],
  refreshing = false,
  className,
  "data-testid": dataTestId = "version-conflict-notice",
}: VersionConflictNoticeProps): ReactElement {
  const serverText =
    conflict.serverRevision === null || conflict.serverRevision === undefined
      ? "服务端已有更新修订（具体版本以刷新结果为准）"
      : `服务端当前修订：r${conflict.serverRevision}`;

  return (
    <div
      className={cx("fp-version-conflict", className)}
      role="alert"
      aria-live="assertive"
      data-testid={dataTestId}
      data-expected-revision={conflict.expectedRevision}
    >
      <p className="fp-version-conflict__title">版本冲突：任务已被其他端更新</p>
      <p className="fp-version-conflict__detail">
        你基于修订 r{conflict.expectedRevision} 提交，{serverText}。检测时间：{formatDateTime(conflict.detectedAt)}。
      </p>
      <p className="fp-version-conflict__keep">
        刷新到最新修订不会丢弃你已确认的动作；已确认的 {pendingActions.length} 项会保留并在刷新后重新核对。
      </p>
      {pendingActions.length > 0 ? (
        <ul className="fp-version-conflict__actions" data-testid="version-conflict-pending-actions">
          {pendingActions.map((action) => (
            <li key={action.actionId}>
              {action.label}
              <span className="fp-version-conflict__meta">
                （基于 r{action.expectedRevision}
                {action.operationId ? ` · 操作编号 ${action.operationId}` : ""} · {formatDateTime(action.at)}）
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="fp-version-conflict__meta">本端没有待保留的本地动作。</p>
      )}
      <div className="fp-version-conflict__buttons">
        <button
          type="button"
          className="fp-button fp-button--primary"
          onClick={onRefresh}
          aria-busy={refreshing}
          disabled={refreshing}
        >
          {refreshing ? "正在刷新…" : "刷新到最新修订"}
        </button>
        {onDismiss ? (
          <button type="button" className="fp-button" onClick={onDismiss}>
            稍后处理
          </button>
        ) : null}
      </div>
    </div>
  );
}