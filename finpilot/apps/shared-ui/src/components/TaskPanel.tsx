import { useState, type FormEvent, type KeyboardEvent, type ReactElement, type ReactNode } from "react";
import type { ApiFailure } from "../api/client";
import { normalizeResultSummary } from "../api/adapters";
import type { Grant, LocalConfirmedAction, Plan, ScopeSummary, TaskMessage, TaskView, VersionConflictDetail } from "../types";
import { AUTH_LEVEL_SHORT_LABELS, isTaskTerminal, TASK_STATUS_LABELS } from "../contract/labels";
import { formatDateTime } from "../format/datetime";
import { AuthorizationCard } from "./AuthorizationCard";
import { ErrorBanner } from "./ErrorBanner";
import { GoalSummaryCard } from "./GoalSummaryCard";
import { ModeBadge } from "./ModeBadge";
import { MoneyText } from "./MoneyText";
import { PlanSteps } from "./PlanSteps";
import { StatusChip, toneForStatus } from "./StatusChip";
import { VersionConflictNotice } from "./VersionConflictNotice";
import { cx } from "../lib/cx";

/**
 * 任务面板（A01 共用组件；两个客户端都渲染它）。
 *
 * 覆盖 SRS 18.2 的主要界面元素：对话、目标摘要、执行步骤、授权卡片、等待原因、结果未完成项。
 * 规则：
 * - 状态一律来自服务端（task.status / 步骤状态 / 权威结果），前端不推断成功；
 * - 不渲染假进度动画：进度只显示按真实步骤状态统计的数字与已核验效果清单；
 * - 等待人工、等待到账、等待授权都显示"等待原因"，绝不显示成完成；
 * - 版本冲突时保留已确认动作（pendingActions），只提示刷新。
 */
export interface TaskPanelProps {
  task?: TaskView | null;
  loading?: boolean;
  error?: ApiFailure | null;
  conflict?: VersionConflictDetail | null;
  pendingActions?: LocalConfirmedAction[];
  refreshing?: boolean;
  busy?: boolean;
  disabled?: boolean;
  title?: string;
  onSendMessage?: (text: string) => void;
  onConfirmPlan?: (plan: Plan) => void;
  onVerifyPlan?: (plan: Plan) => void;
  onStopTask?: () => void;
  onResumeTask?: () => void;
  onRevokeGrant?: (grant: Grant) => void;
  onViewOperation?: (operationId: string) => void;
  onDismissConflict?: () => void;
  onRefresh?: () => void;
  children?: ReactNode;
  className?: string;
  "data-testid"?: string;
}

function messageRoleLabel(role: string): string {
  switch (role) {
    case "user":
      return "你";
    case "assistant":
      return "融策智能体";
    case "system":
      return "系统";
    case "operator":
      return "人工客服";
    default:
      return `未知角色（${role}）`;
  }
}

function scopeFromPlan(plan: Plan): ScopeSummary {
  const raw = (plan.scope ?? {}) as Record<string, unknown>;
  const summary: ScopeSummary = {
    scope_hash: plan.scope_hash,
    tool: typeof raw.tool === "string" ? (raw.tool as string) : undefined,
    amount_minor: typeof raw.amount_minor === "number" ? (raw.amount_minor as number) : null,
    fee_minor: typeof raw.fee_minor === "number" ? (raw.fee_minor as number) : null,
    currency: typeof raw.currency === "string" ? (raw.currency as string) : "CNY",
    source_account_id: typeof raw.source_account_id === "string" ? (raw.source_account_id as string) : null,
    object_label: typeof raw.object_label === "string" ? (raw.object_label as string) : null,
    counterparty:
      raw.counterparty && typeof raw.counterparty === "object"
        ? (raw.counterparty as ScopeSummary["counterparty"])
        : typeof raw.counterparty === "string"
          ? raw.counterparty
          : null,
    time_window:
      raw.time_window && typeof raw.time_window === "object"
        ? (raw.time_window as ScopeSummary["time_window"])
        : null,
    irreversible: typeof raw.irreversible === "boolean" ? (raw.irreversible as boolean) : undefined,
    irreversible_impacts: Array.isArray(raw.irreversible_impacts)
      ? (raw.irreversible_impacts as string[])
      : undefined,
    cancel_conditions: Array.isArray(raw.cancel_conditions) ? (raw.cancel_conditions as string[]) : undefined,
    irreversible_cost_minor:
      typeof raw.irreversible_cost_minor === "number" ? (raw.irreversible_cost_minor as number) : null,
  };
  const known = new Set([
    "tool",
    "amount_minor",
    "fee_minor",
    "currency",
    "source_account_id",
    "object_label",
    "counterparty",
    "time_window",
    "irreversible",
    "irreversible_impacts",
    "cancel_conditions",
    "irreversible_cost_minor",
    "scope_hash",
  ]);
  const extra = Object.fromEntries(Object.entries(raw).filter(([key]) => !known.has(key)));
  if (Object.keys(extra).length > 0) summary.extra = extra;
  return summary;
}

/** 结果区：必须显示未完成项与未知项（STATE-04），未知不得写成完成。 */
export function TaskResultSection({ task }: { task: TaskView }): ReactElement | null {
  const summary = normalizeResultSummary(task.result_summary ?? null);
  if (!summary) return null;

  return (
    <section className="fp-card fp-result" aria-label="任务结果" data-testid="task-result">
      <header className="fp-card__header">
        <h2 className="fp-card__title">任务结果</h2>
        <StatusChip
          label={TASK_STATUS_LABELS[String(task.status)] ?? `未知状态（${String(task.status)}）`}
          tone={toneForStatus(String(task.status))}
          srPrefix="任务状态"
          rawValue={String(task.status)}
        />
        <span className="fp-result__revision">修订 r{task.revision}</span>
      </header>

      <dl className="fp-result__amounts">
        <div>
          <dt>已花金额（权威）</dt>
          <dd>
            <MoneyText amountMinor={summary.totalSpentMinor ?? task.spent_minor ?? null} fallback="不适用" />
          </dd>
        </div>
        <div>
          <dt>已产生费用</dt>
          <dd>
            <MoneyText amountMinor={summary.totalFeeMinor ?? task.fee_minor ?? null} fallback="不适用" />
          </dd>
        </div>
        <div>
          <dt>生成时间</dt>
          <dd>{formatDateTime(summary.generatedAt, "未提供")}</dd>
        </div>
      </dl>

      <section className="fp-result__group" aria-label="已完成效果">
        <h3>已完成效果（{summary.completed.length}）</h3>
        <p className="fp-result__hint">只列已核验事实（回执／账本／运营录入），模型措辞不算依据。</p>
        {summary.completed.length === 0 ? (
          <p className="fp-result__empty">无</p>
        ) : (
          <ul data-testid="task-result-completed">
            {summary.completed.map((item, index) => (
              <li key={`${item.label}-${index}`}>
                <span className="fp-result__label">{item.label}</span>
                {typeof item.amountMinor === "number" ? <MoneyText amountMinor={item.amountMinor} /> : null}
                {item.operationId ? (
                  <span className="fp-result__op">
                    操作编号：<code>{item.operationId}</code>
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      {[
        { key: "pending", title: `待完成（${summary.pending.length}）`, items: summary.pending, hint: "尚未完成，不是成功。" },
        {
          key: "unknown",
          title: `结果未知（${summary.unknown.length}）`,
          items: summary.unknown,
          hint: "需要用原操作编号核对，不得当作成功或失败。",
        },
        {
          key: "failed",
          title: `失败（${summary.failed.length}）`,
          items: summary.failed,
          hint: "失败项若涉及资金，以服务端回执与账本为准。",
        },
      ].map((group) => (
        <section className="fp-result__group" key={group.key} aria-label={group.title}>
          <h3>{group.title}</h3>
          <p className="fp-result__hint">{group.hint}</p>
          {group.items.length === 0 ? (
            <p className="fp-result__empty">无</p>
          ) : (
            <ul data-testid={`task-result-${group.key}`}>
              {group.items.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          )}
        </section>
      ))}

      {summary.nextActions.length > 0 ? (
        <section className="fp-result__group" aria-label="下一步">
          <h3>下一步可执行动作</h3>
          <ul data-testid="task-result-next-actions">
            {summary.nextActions.map((action) => (
              <li key={action.action_id}>
                {action.label}
                {action.kind ? `（${action.kind}）` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}
    </section>
  );
}

export function TaskPanel({
  task,
  loading = false,
  error,
  conflict,
  pendingActions = [],
  refreshing = false,
  busy = false,
  disabled = false,
  title = "任务",
  onSendMessage,
  onConfirmPlan,
  onVerifyPlan,
  onStopTask,
  onResumeTask,
  onRevokeGrant,
  onViewOperation,
  onDismissConflict,
  onRefresh,
  children,
  className,
  "data-testid": dataTestId = "task-panel",
}: TaskPanelProps): ReactElement {
  const [draft, setDraft] = useState("");

  const status = task ? String(task.status) : null;
  const terminal = isTaskTerminal(status);
  const plan = task?.plan ?? null;
  const activeGrant = plan
    ? (task?.grants ?? []).find(
        (grant) => grant.plan_id === plan.plan_id && (grant.status === "active" || grant.status === "consumed"),
      )
    : undefined;
  const verified = !!activeGrant;
  const messages: TaskMessage[] = task?.messages ?? [];
  const waiting = task?.waiting_reason ?? null;
  const showAuthorization =
    !!plan && (task?.status === "awaiting_authorization" || plan.required_level === "RED" || plan.required_level === "YELLOW");

  function submitDraft(event?: FormEvent<HTMLFormElement>): void {
    event?.preventDefault();
    const text = draft.trim();
    if (!text || !onSendMessage || disabled || terminal) return;
    onSendMessage(text);
    setDraft("");
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submitDraft();
    }
  }

  return (
    <section className={cx("fp-card", "fp-task", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header fp-task__header">
        <h2 className="fp-card__title">{title}</h2>
        {task ? (
          <>
            <StatusChip
              label={TASK_STATUS_LABELS[status as string] ?? `未知状态（${status}）`}
              tone={toneForStatus(status)}
              srPrefix="任务状态"
              rawValue={status ?? undefined}
              data-testid="task-status-chip"
            />
            <span className="fp-task__revision" data-testid="task-revision">
              修订 r{task.revision}
            </span>
            <ModeBadge
              mode={task.mode ?? null}
              observedAt={task.updated_at ? formatDateTime(task.updated_at) : null}
              size="sm"
              source="任务视图"
            />
            <span className="fp-task__channel">渠道：{task.channel ?? "未提供"}</span>
            {onRefresh ? (
              <button type="button" className="fp-button fp-button--small" onClick={onRefresh} disabled={refreshing}>
                {refreshing ? "刷新中…" : "刷新"}
              </button>
            ) : null}
          </>
        ) : null}
      </header>

      {conflict ? (
        <VersionConflictNotice
          conflict={conflict}
          onRefresh={onRefresh ?? (() => undefined)}
          onDismiss={onDismissConflict}
          pendingActions={pendingActions}
          refreshing={refreshing}
        />
      ) : null}

      {error ? <ErrorBanner failure={error} context="任务视图" onRetry={onRefresh} /> : null}

      {!task ? (
        <p className="fp-task__empty" aria-busy={loading}>
          {loading ? "正在读取任务状态…" : "尚未选择任务。"}
        </p>
      ) : (
        <>
          {task.goal?.goals && task.goal.goals.length > 0 ? (
            <p className="fp-task__objective">目标：{task.goal.goals.join("、")}</p>
          ) : null}
          {task.blocked_reason ? (
            <p className="fp-task__blocked" role="status">
              任务受阻：{task.blocked_reason}
            </p>
          ) : null}

          <section className="fp-task__messages" aria-label="任务对话">
            <h3 className="fp-task__section-title">对话</h3>
            <ol className="fp-chat" role="log" aria-live="polite" aria-relevant="additions text">
              {messages.length === 0 ? (
                <li className="fp-chat__empty">还没有对话内容（或服务端未在任务视图中返回消息）。</li>
              ) : (
                messages.map((message) => (
                  <li
                    key={message.message_id}
                    className={cx("fp-chat__item", `fp-chat__item--${message.role}`)}
                    data-role={message.role}
                  >
                    <span className="fp-chat__meta">
                      {messageRoleLabel(message.role)} · {formatDateTime(message.created_at)}
                      {typeof message.task_revision === "number" ? ` · 修订 r${message.task_revision}` : ""}
                    </span>
                    <p className="fp-chat__text">{message.text}</p>
                    {message.operation_id ? (
                      <span className="fp-chat__op">
                        操作编号：<code>{message.operation_id}</code>
                      </span>
                    ) : null}
                  </li>
                ))
              )}
            </ol>
          </section>

          {waiting ? (
            <div className="fp-task__waiting" role="status" aria-live="polite" data-testid="task-waiting-reason">
              <p className="fp-task__waiting-title">等待原因：{waiting.safe_message}</p>
              <p className="fp-task__waiting-meta">原因码：{waiting.code}</p>
              {waiting.since ? <p className="fp-task__waiting-meta">自 {formatDateTime(waiting.since)} 起</p> : null}
              {waiting.operation_id ? (
                <p className="fp-task__waiting-meta">
                  相关操作编号：<code>{waiting.operation_id}</code>
                </p>
              ) : null}
              {waiting.challenge_id ? (
                <p className="fp-task__waiting-meta">
                  待完成验证：<code>{waiting.challenge_id}</code>
                </p>
              ) : null}
              {waiting.action_hint ? <p className="fp-task__waiting-action">你现在可以：{waiting.action_hint}</p> : null}
              <p className="fp-task__waiting-note">等待不是成功：状态变化后需要重新核对权威结果。</p>
            </div>
          ) : null}

          <GoalSummaryCard goal={task.goal ?? null} clarifications={task.clarifications ?? []} />

          <PlanSteps plan={plan} onViewOperation={onViewOperation} />

          {showAuthorization && plan ? (
            <AuthorizationCard
              level={String(plan.required_level ?? "YELLOW")}
              summary={scopeFromPlan(plan)}
              scopeHash={plan.scope_hash}
              status={activeGrant ? "confirmed" : "pending"}
              verified={verified}
              busy={busy}
              disabled={terminal}
              revocable={!!activeGrant && !!onRevokeGrant}
              onConfirm={onConfirmPlan ? () => onConfirmPlan(plan) : undefined}
              onVerify={onVerifyPlan ? () => onVerifyPlan(plan) : undefined}
              onRevoke={activeGrant && onRevokeGrant ? () => onRevokeGrant(activeGrant) : undefined}
            />
          ) : null}

          {task.progress ? (
            <div className="fp-task__progress" data-testid="task-progress-text">
              <p>
                进度（按真实步骤状态统计）：已完成 {task.progress.completed_steps} / 共 {task.progress.total_steps} 步。
              </p>
              {task.progress.pending_effects && task.progress.pending_effects.length > 0 ? (
                <p>待完成效果：{task.progress.pending_effects.join("；")}</p>
              ) : null}
              {task.progress.completed_effects && task.progress.completed_effects.length > 0 ? (
                <p>已核验效果：{task.progress.completed_effects.join("；")}</p>
              ) : null}
            </div>
          ) : null}

          {children}

          <TaskResultSection task={task} />

          <div className="fp-task__actions">
            {onStopTask && !terminal ? (
              <button type="button" className="fp-button" onClick={onStopTask} disabled={busy} data-testid="task-stop-button">
                停止任务（未提交部分会被停止；已提交的按实际结果处理）
              </button>
            ) : null}
            {onResumeTask && (task.status === "blocked" || task.status === "ready" || task.status === "awaiting_human") ? (
              <button
                type="button"
                className="fp-button fp-button--primary"
                onClick={onResumeTask}
                disabled={busy}
                data-testid="task-resume-button"
              >
                继续执行
              </button>
            ) : null}
          </div>

          <form className="fp-task__composer" onSubmit={submitDraft}>
            <label className="fp-task__composer-label" htmlFor={`${dataTestId}-composer`}>
              追加指令（会产生新修订；旧授权不会自动沿用）
            </label>
            <textarea
              id={`${dataTestId}-composer`}
              className="fp-task__composer-input"
              data-testid="task-composer"
              value={draft}
              rows={2}
              maxLength={2000}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={handleKeyDown}
              disabled={disabled || terminal || !onSendMessage}
              placeholder={terminal ? "任务已结束：如需继续请新建任务" : "例如：把生日礼物预算改成 300 元"}
            />
            <div className="fp-task__composer-actions">
              <button
                type="submit"
                className="fp-button fp-button--primary"
                disabled={disabled || terminal || !onSendMessage || draft.trim() === ""}
                data-testid="task-send-button"
              >
                发送（Ctrl+Enter）
              </button>
              {plan ? (
                <span className="fp-task__plan-level">
                  计划等级：
                  {AUTH_LEVEL_SHORT_LABELS[String(plan.required_level).toUpperCase()] ?? String(plan.required_level)}
                </span>
              ) : null}
              {activeGrant ? (
                <span className="fp-task__grant">
                  已授权：<code>{activeGrant.grant_id}</code>（{activeGrant.status}）
                </span>
              ) : null}
            </div>
          </form>
        </>
      )}
    </section>
  );
}