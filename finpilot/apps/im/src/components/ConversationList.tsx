import { useState, type FormEvent, type ReactElement } from "react";
import { StatusChip, TASK_STATUS_LABELS, formatDateTime, toneForStatus, type TaskView } from "@finpilot/shared-ui";

/**
 * 左侧会话列表（每个任务 = 一个会话）。
 *
 * 规则：
 * - 列表项显示服务端返回的真实状态（文字标签 + 原值）；
 * - 新建会话直接用 message + channel 创建任务（幂等键 client_message_id）；
 * - 键盘可达：列表项是 button，可用 Enter/Space 切换。
 */
export interface ConversationListProps {
  conversations: TaskView[];
  activeId: string | null;
  busy?: boolean;
  onSelect: (taskId: string) => void;
  onCreate: (message: string) => void;
}

export function ConversationList({
  conversations,
  activeId,
  busy = false,
  onSelect,
  onCreate,
}: ConversationListProps): ReactElement {
  const [draft, setDraft] = useState("");

  function submit(event: FormEvent<HTMLFormElement>): void {
    event.preventDefault();
    const text = draft.trim();
    if (!text) return;
    onCreate(text);
    setDraft("");
  }

  return (
    <div className="im-conversations">
      <form className="im-conversations__new" onSubmit={submit}>
        <label className="im-conversations__label" htmlFor="im-new-conversation">
          新会话（发送一条指令开始）
        </label>
        <textarea
          id="im-new-conversation"
          className="im-conversations__input"
          rows={2}
          maxLength={2000}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          placeholder="例如：帮我把上个月的订阅理一遍"
          data-testid="im-new-conversation"
        />
        <button type="submit" className="fp-button fp-button--small fp-button--primary" disabled={busy || draft.trim() === ""}>
          发送
        </button>
      </form>

      {conversations.length === 0 ? (
        <p className="im-conversations__empty">还没有会话。</p>
      ) : (
        <ul className="fp-im__conversations" data-testid="im-conversation-list">
          {conversations.map((task) => (
            <li key={task.task_id}>
              <button
                type="button"
                className="fp-im__conversation"
                aria-current={activeId === task.task_id ? "true" : undefined}
                onClick={() => onSelect(task.task_id)}
              >
                <span className="im-conversations__title">
                  {task.goal?.goals?.length ? task.goal.goals.join("、") : task.task_id}
                </span>
                <span className="im-conversations__meta">
                  <StatusChip
                    label={TASK_STATUS_LABELS[String(task.status)] ?? `未知状态（${String(task.status)}）`}
                    tone={toneForStatus(String(task.status))}
                    srPrefix="任务状态"
                    rawValue={String(task.status)}
                  />
                  <span className="im-conversations__time">{formatDateTime(task.updated_at)}</span>
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}