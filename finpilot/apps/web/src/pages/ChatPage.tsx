import { useCallback, useState, type FormEvent, type ReactElement } from "react";
import { ErrorBanner, newOperationId, taskApi, type ApiFailure } from "@finpilot/shared-ui";
import { useAppState } from "../state";
import { TaskDetail } from "./TaskDetail";

/**
 * 对话页（APP 式）：创建任务 + 直接在当前页管理任务。
 *
 * 规则：
 * - 创建任务用 message + channel（契约 task.json 的客户端可写字段），幂等键用 client_message_id；
 * - 任务状态、等待原因、计划步骤、授权与结果都由服务端返回，页面不做任何推断；
 * - 没有假进度：等待时只显示"正在读取服务端状态…"这类真实说明。
 */
export function ChatPage(): ReactElement {
  const { activeTaskId, setActiveTaskId, reportMode } = useAppState();
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);

  const createTask = useCallback(
    async (event: FormEvent<HTMLFormElement>) => {
      event.preventDefault();
      const text = message.trim();
      if (!text) return;
      setBusy(true);
      setError(null);
      const result = await taskApi.create({
        message: text,
        channel: "app",
        clientMessageId: newOperationId(),
      });
      setBusy(false);
      if (!result.ok) {
        setError(result.error);
        return;
      }
      setMessage("");
      reportMode(result.data.mode ?? null);
      setActiveTaskId(result.data.task_id);
    },
    [message, reportMode, setActiveTaskId],
  );

  return (
    <div className="web-chat">
      <form className="web-composer" onSubmit={createTask}>
        <label className="web-composer__label" htmlFor="web-chat-input">
          告诉融策你想做什么（会产生新任务；高风险操作需要你确认或强验证）
        </label>
        <textarea
          id="web-chat-input"
          className="web-composer__input"
          data-testid="web-chat-input"
          rows={3}
          maxLength={2000}
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="例如：给妈妈买生日礼物，后天送到，预算 500 元以内"
        />
        <div className="web-composer__actions">
          <button
            type="submit"
            className="fp-button fp-button--primary"
            disabled={busy || message.trim() === ""}
            data-testid="web-chat-send"
          >
            {busy ? "正在创建任务…" : "发送"}
          </button>
          <span className="web-composer__hint">任务创建后可在下方查看真实状态、计划与授权。</span>
        </div>
      </form>

      {error ? <ErrorBanner failure={error} context="创建任务" onDismiss={() => setError(null)} /> : null}

      {activeTaskId ? (
        <TaskDetail taskId={activeTaskId} onMode={reportMode} />
      ) : (
        <section className="fp-card" aria-label="任务">
          <h2 className="fp-card__title">当前没有进行中的任务</h2>
          <p className="web-hint">
            发送一条指令后，这里会显示：目标摘要（硬约束/偏好/事实分开）、执行步骤的真实状态、
            授权卡片与结果回执。
          </p>
        </section>
      )}
    </div>
  );
}