import { useCallback, useEffect, useState, type ReactElement } from "react";
import {
  ErrorBanner,
  StatusChip,
  TaskPanel,
  TASK_STATUS_LABELS,
  formatDateTime,
  taskApi,
  toneForStatus,
  type ApiFailure,
  type TaskView,
} from "@finpilot/shared-ui";
import { useAppState } from "../state";
import { TaskDetail } from "./TaskDetail";

/**
 * 任务页：任务列表 + 选中任务详情。
 *
 * 规则：
 * - 列表只显示服务端返回的状态标签（文字），未登记状态显示原值；
 * - 选中任务复用 TaskDetail（同一份真实状态视图）；
 * - 加载/失败都有真实说明，不显示假进度。
 */
export function TasksPage(): ReactElement {
  const { activeTaskId, setActiveTaskId, reportMode } = useAppState();
  const [tasks, setTasks] = useState<TaskView[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const result = await taskApi.list({ limit: 20 });
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError(null);
    setTasks(result.data);
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="web-tasks">
      <section className="fp-card" aria-label="任务列表">
        <header className="fp-card__header">
          <h2 className="fp-card__title">任务列表</h2>
          <button type="button" className="fp-button fp-button--small" onClick={() => void load()} disabled={loading}>
            {loading ? "正在刷新…" : "刷新"}
          </button>
        </header>
        {error ? <ErrorBanner failure={error} context="任务列表" onRetry={() => void load()} /> : null}
        {tasks.length === 0 ? (
          <p className="web-hint" aria-busy={loading}>
            {loading ? "正在读取任务列表…" : "暂无任务（可到对话页创建）。"}
          </p>
        ) : (
          <ul className="web-task-list" data-testid="web-task-list">
            {tasks.map((task) => (
              <li key={task.task_id} className="web-task-list__item">
                <button
                  type="button"
                  className="fp-button fp-button--small"
                  aria-current={activeTaskId === task.task_id}
                  onClick={() => setActiveTaskId(task.task_id)}
                >
                  查看
                </button>
                <span className="web-task-list__objective">
                  {task.goal?.goals?.length ? task.goal.goals.join("、") : task.task_id}
                </span>
                <StatusChip
                  label={TASK_STATUS_LABELS[String(task.status)] ?? `未知状态（${String(task.status)}）`}
                  tone={toneForStatus(String(task.status))}
                  srPrefix="任务状态"
                  rawValue={String(task.status)}
                />
                <span className="web-task-list__time">更新：{formatDateTime(task.updated_at)}</span>
                <span className="web-task-list__revision">修订 r{task.revision}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      {activeTaskId ? (
        <TaskDetail taskId={activeTaskId} onMode={reportMode} />
      ) : (
        <TaskPanel task={null} />
      )}
    </div>
  );
}