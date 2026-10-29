import { useState, type FormEvent, type KeyboardEvent, type ReactElement } from "react";
import {
  AuthorizationCard,
  PlanSteps,
  StatusChip,
  TASK_STATUS_LABELS,
  formatDateTime,
  toneForStatus,
  type Plan,
  type TaskMessage,
  type TaskView,
} from "@finpilot/shared-ui";

/**
 * 右侧消息流：消息气泡 + 内嵌授权卡片/计划卡片。
 *
 * 规则：
 * - 消息来自服务端任务视图（展示补充字段）；没有消息时显示"服务端未返回消息"，不编造对话；
 * - 授权卡片内嵌在气泡里：YELLOW 可直接确认，RED 只能进入验证（由 shared-ui 组件保证）；
 * - 计划步骤在同一气泡下方展示真实状态，不用动画代替状态；
 * - 回执卡片由父组件在用户点击"查询回执"后以独立气泡展示。
 */
export interface MessageStreamProps {
  task: TaskView | null;
  loading?: boolean;
  busy?: boolean;
  onSend: (text: string) => void;
  onConfirmPlan: (plan: Plan) => void;
  onVerifyPlan: (plan: Plan) => void;
  onQueryOperation: (operationId: string) => void;
}

function roleLabel(role: string): string {
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

export function MessageStream({
  task,
  loading = false,
  busy = false,
  onSend,
  onConfirmPlan,
  onVerifyPlan,
  onQueryOperation,
}: MessageStreamProps): ReactElement {
  const [draft, setDraft] = useState("");
  const messages: TaskMessage[] = task?.messages ?? [];
  const plan = task?.plan ?? null;
  const activeGrant = plan
    ? (task?.grants ?? []).find((grant) => grant.plan_id === plan.plan_id && grant.status === "active")
    : undefined;
  const showAuthorization = !!plan && (task?.status === "awaiting_authorization" || plan.required_level === "RED" || plan.required_level === "YELLOW");

  function submit(event?: FormEvent<HTMLFormElement>): void {
    event?.preventDefault();
    const text = draft.trim();
    if (!text) return;
    onSend(text);
    setDraft("");
  }

  function keyDown(event: KeyboardEvent<HTMLTextAreaElement>): void {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      submit();
    }
  }

  return (
    <div className="im-stream" data-testid="im-message-stream">
      <div className="im-stream__messages" role="log" aria-live="polite" aria-relevant="additions text">
        {!task ? (
          <p className="im-stream__empty" aria-busy={loading}>
            {loading ? "正在读取会话…" : "请选择左侧会话，或发送一条新指令开始。"}
          </p>
        ) : messages.length === 0 ? (
          <p className="im-stream__empty">服务端尚未返回消息（任务已创建，等待智能体回复）。</p>
        ) : (
          messages.map((message) => (
            <article
              key={message.message_id}
              className={`fp-im__bubble fp-im__bubble--${message.role}`}
              data-role={message.role}
              data-testid={`im-message-${message.message_id}`}
            >
              <span className="fp-im__bubble-meta">
                {roleLabel(message.role)} · {formatDateTime(message.created_at)}
                {typeof message.task_revision === "number" ? ` · 修订 r${message.task_revision}` : ""}
              </span>
              <p className="im-stream__text">{message.text}</p>
              {message.operation_id ? (
                <button
                  type="button"
                  className="fp-button fp-button--small"
                  onClick={() => onQueryOperation(message.operation_id as string)}
                >
                  查看回执（{message.operation_id}）
                </button>
              ) : null}
            </article>
          ))
        )}

        {task && plan ? (
          <article className="fp-im__bubble fp-im__bubble--card" data-testid="im-plan-bubble">
            <span className="fp-im__bubble-meta">
              系统 · 计划 r{plan.revision} ·{" "}
              <StatusChip
                label={plan.feasible ? "可行方案已生成" : "不可行（服务端给出原因）"}
                tone={plan.feasible ? "pending" : "danger"}
                srPrefix="计划"
              />
            </span>
            <PlanSteps plan={plan} onViewOperation={onQueryOperation} />
            {showAuthorization ? (
              <div className="fp-im__bubble-card">
                <AuthorizationCard
                  level={String(plan.required_level)}
                  summary={{
                    scope_hash: plan.scope_hash,
                    ...(plan.scope as Record<string, unknown> | undefined),
                  }}
                  scopeHash={plan.scope_hash}
                  status={activeGrant ? "confirmed" : "pending"}
                  verified={!!activeGrant}
                  busy={busy}
                  revocable={!!activeGrant}
                  onConfirm={() => onConfirmPlan(plan)}
                  onVerify={() => onVerifyPlan(plan)}
                />
              </div>
            ) : null}
          </article>
        ) : null}

        {task?.result_summary ? (
          <article className="fp-im__bubble fp-im__bubble--card" data-testid="im-result-bubble">
            <span className="fp-im__bubble-meta">
              系统 · 结果摘要 ·{" "}
              <StatusChip
                label={TASK_STATUS_LABELS[String(task.status)] ?? `未知状态（${String(task.status)}）`}
                tone={toneForStatus(String(task.status))}
                srPrefix="任务状态"
              />
            </span>
            <p className="im-stream__text">
              未完成与未知项以任务页/结果页的权威展示为准；未知项不会显示为完成。
            </p>
          </article>
        ) : null}
      </div>

      <form className="im-stream__composer" onSubmit={submit}>
        <label className="im-stream__composer-label" htmlFor="im-composer">
          发送消息（会产生新修订；旧授权不会自动沿用）
        </label>
        <textarea
          id="im-composer"
          className="im-stream__composer-input"
          data-testid="im-composer"
          rows={2}
          maxLength={2000}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={keyDown}
          disabled={!task || busy}
          placeholder={task ? "例如：把预算改成 300 元" : "请先选择会话"}
        />
        <button
          type="submit"
          className="fp-button fp-button--primary"
          disabled={!task || busy || draft.trim() === ""}
          data-testid="im-send"
        >
          发送（Ctrl+Enter）
        </button>
      </form>
    </div>
  );
}