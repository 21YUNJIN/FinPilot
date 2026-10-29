import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { cacheKeys, resourceCache } from "../src/api/cache";
import { getActiveUserId, setSession } from "../src/api/session";
import { TaskPanel } from "../src/components/TaskPanel";
import { useTask, type TaskFetcher } from "../src/hooks/useTask";
import type { ApiResult } from "../src/api/client";
import type { LocalConfirmedAction, TaskView } from "../src/types";

function taskView(overrides: Partial<TaskView> = {}): TaskView {
  return {
    task_id: "task_1",
    owner_user_id: "user_demo_01",
    channel: "app",
    status: "running",
    revision: 5,
    reference_time: "2026-10-27T08:00:00Z",
    mode: "mock",
    created_at: "2026-10-27T07:59:00Z",
    updated_at: "2026-10-27T08:00:00Z",
    messages: [],
    ...overrides,
  };
}

function okResult(data: TaskView): ApiResult<TaskView> {
  return {
    ok: true,
    data,
    envelope: {
      status: "ok",
      data,
      error: null,
      provenance: { adapter: "finpilot-api", observed_at: "2026-10-27T08:00:00Z", schema_version: "1.0", mode: "mock" },
    },
    meta: {
      method: "GET",
      path: "/v1/tasks/task_1",
      url: "http://127.0.0.1:58000/v1/tasks/task_1",
      httpStatus: 200,
      status: "ok",
      requestId: "req_1",
      operationId: null,
    },
  };
}

function conflictResult(serverRevision: number): ApiResult<TaskView> {
  return {
    ok: false,
    error: {
      code: "VERSION_CONFLICT",
      message: "对象已被其他操作更新，请刷新后重试。",
      hint: "请修正输入或刷新后重试。",
      retryMode: "after_input_fix",
      knownCode: true,
      requiresQueryOfOriginalOperation: false,
      httpStatus: 409,
      status: "failed",
      reasonCodes: [],
      details: { server_revision: serverRevision },
      requestId: "req_2",
      operationId: null,
      envelope: null,
      transport: false,
    },
    meta: {
      method: "POST",
      path: "/v1/tasks/task_1/messages",
      url: "http://127.0.0.1:58000/v1/tasks/task_1/messages",
      httpStatus: 409,
      status: "failed",
      requestId: "req_2",
      operationId: null,
    },
  };
}

describe("useTask 轮询与版本冲突", () => {
  it("先加载任务视图，并在冲突时保留本地已确认动作、提示刷新", async () => {
    setSession({ token: "tok_a", userId: "user_demo_01", channel: "web" });
    let call = 0;
    const fetcher: TaskFetcher = vi.fn(async () => {
      call += 1;
      if (call === 1) return okResult(taskView({ revision: 5 }));
      return conflictResult(7);
    });

    const { result } = renderHook(() => useTask({ taskId: "task_1", fetcher, pollIntervalMs: 10_000 }));

    await waitFor(() => expect(result.current.view?.revision).toBe(5));
    expect(result.current.error).toBeNull();

    act(() => {
      result.current.recordLocalAction({ kind: "confirm_plan", label: "已确认计划 r5", operationId: "op_1" });
    });
    expect(result.current.pendingActions).toHaveLength(1);
    expect(result.current.pendingActions[0]?.expectedRevision).toBe(5);

    await act(async () => {
      await result.current.refresh();
    });

    await waitFor(() => expect(result.current.conflict).not.toBeNull());
    expect(result.current.conflict?.expectedRevision).toBe(5);
    expect(result.current.conflict?.serverRevision).toBe(7);
    // 冲突不丢弃已确认动作，也保留最后一次已知视图
    expect(result.current.pendingActions).toHaveLength(1);
    expect(result.current.view?.revision).toBe(5);
    expect(result.current.error).toBeNull();

    await act(async () => {
      await result.current.resolveConflict();
    });
    expect(result.current.conflict).toBeNull();
  });

  it("冲突提示在界面上可渲染，并列出被保留的动作", async () => {
    setSession({ token: "tok_a", userId: "user_demo_01", channel: "web" });
    let call = 0;
    const fetcher: TaskFetcher = async () => {
      call += 1;
      return call === 1 ? okResult(taskView({ revision: 3 })) : conflictResult(4);
    };

    function Harness() {
      const task = useTask({ taskId: "task_1", fetcher, pollIntervalMs: 10_000 });
      return (
        <TaskPanel
          task={task.view}
          conflict={task.conflict}
          pendingActions={task.pendingActions}
          refreshing={task.refreshing}
          onRefresh={() => {
            task.recordLocalAction({ kind: "send_message", label: "已发送：预算改成 300 元" });
            void task.refresh();
          }}
        />
      );
    }

    render(<Harness />);
    await waitFor(() => expect(screen.getByTestId("task-revision")).toHaveTextContent("修订 r3"));

    await act(async () => {
      await screen.getByRole("button", { name: "刷新" }).click();
    });

    await waitFor(() => expect(screen.getByTestId("version-conflict-notice")).toBeInTheDocument());
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("你基于修订 r3");
    expect(screen.getByTestId("version-conflict-pending-actions")).toHaveTextContent("已发送：预算改成 300 元");
  });

  it("切换用户时清空本地状态与缓存（另一个用户看不到上一个用户的数据）", async () => {
    setSession({ token: "tok_a", userId: "user_demo_01", channel: "web" });
    const fetcher: TaskFetcher = async () =>
      okResult(taskView({ revision: getActiveUserId() === "user_demo_02" ? 20 : 8 }));

    const { result } = renderHook(() => useTask({ taskId: "task_1", fetcher, pollIntervalMs: 10_000 }));
    await waitFor(() => expect(result.current.view?.revision).toBe(8));
    expect(resourceCache.size()).toBeGreaterThan(0);
    act(() => {
      result.current.recordLocalAction({ kind: "confirm_plan", label: "用户 A 的确认动作" });
    });
    expect(result.current.pendingActions).toHaveLength(1);

    await act(async () => {
      setSession({ token: "tok_b", userId: "user_demo_02", channel: "web" });
    });

    // 新用户取到的是自己的视图（r20），不是上一个用户的缓存（r8）
    await waitFor(() => expect(result.current.view?.revision).toBe(20));
    expect(result.current.pendingActions).toHaveLength(0);
    expect(resourceCache.get(cacheKeys.taskView("task_1"), "user_demo_01")).toBeUndefined();
    expect(resourceCache.owners()).toEqual(["user_demo_02"]);
  });

  it("任务到达终态后停止轮询（不再重复请求）", async () => {
    setSession({ token: "tok_a", userId: "user_demo_01", channel: "web" });
    const fetcher = vi.fn(async () => okResult(taskView({ status: "succeeded", revision: 12 })));

    const { result } = renderHook(() => useTask({ taskId: "task_1", fetcher, pollIntervalMs: 10 }));
    await waitFor(() => expect(result.current.view?.status).toBe("succeeded"));
    const callsAfterFirstLoad = fetcher.mock.calls.length;

    await new Promise((resolve) => setTimeout(resolve, 80));
    expect(fetcher.mock.calls.length).toBe(callsAfterFirstLoad);
  });
});