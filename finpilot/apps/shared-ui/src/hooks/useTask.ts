import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiFailure, ApiResult } from "../api/client";
import { cacheKeys, resourceCache } from "../api/cache";
import { taskApi } from "../api/endpoints";
import { newClientActionId } from "../api/ids";
import { getActiveUserId, onSessionChange } from "../api/session";
import { isTaskTerminal } from "../contract/labels";
import type { LocalConfirmedAction, TaskView, VersionConflictDetail } from "../types";

/**
 * 任务视图轮询 Hook（A01：两端同步、旧修订刷新、切换用户清缓存）。
 *
 * 行为：
 * - 按 taskId + 当前用户轮询 `GET /v1/tasks/{task_id}`，终态后停止轮询（可配置继续）；
 * - 401 由 api/client 清理会话，这里在用户变化时**清空本地状态**并丢弃在途响应；
 * - `VERSION_CONFLICT`：保留最后一次已知视图与本地已确认动作，弹出冲突提示并立即刷新一次，
 *   只提示刷新，绝不用旧修订覆盖服务端结论，也不丢弃用户已确认的动作；
 * - 视图做用户级缓存（只读），切换用户后缓存不可命中。
 */
export type TaskFetcher = (taskId: string, signal: AbortSignal) => Promise<ApiResult<TaskView>>;

export interface UseTaskOptions {
  taskId: string | null;
  /** 注入自定义取数函数（测试或离线模式使用）；默认调用真实接口。 */
  fetcher?: TaskFetcher;
  pollIntervalMs?: number;
  enabled?: boolean;
  pollAfterTerminal?: boolean;
  onVersionConflict?: (conflict: VersionConflictDetail) => void;
}

export interface RecordActionInput {
  kind: LocalConfirmedAction["kind"];
  label: string;
  expectedRevision?: number;
  operationId?: string | null;
  acknowledged?: boolean;
}

export interface UseTaskResult {
  view: TaskView | null;
  loading: boolean;
  refreshing: boolean;
  error: ApiFailure | null;
  conflict: VersionConflictDetail | null;
  pendingActions: LocalConfirmedAction[];
  lastUpdatedAt: string | null;
  refresh: () => Promise<void>;
  dismissConflict: () => void;
  resolveConflict: () => Promise<void>;
  recordLocalAction: (input: RecordActionInput) => LocalConfirmedAction;
  clearLocalActions: () => void;
}

export function useTask(options: UseTaskOptions): UseTaskResult {
  const { taskId, fetcher, pollIntervalMs = 1500, enabled = true, pollAfterTerminal = false, onVersionConflict } = options;

  const [userId, setUserId] = useState<string | null>(() => getActiveUserId());
  const [view, setView] = useState<TaskView | null>(null);
  const [loading, setLoading] = useState<boolean>(!!taskId && enabled);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);
  const [conflict, setConflict] = useState<VersionConflictDetail | null>(null);
  const [pendingActions, setPendingActions] = useState<LocalConfirmedAction[]>([]);
  const [lastUpdatedAt, setLastUpdatedAt] = useState<string | null>(null);

  const viewRevisionRef = useRef<number | null>(null);
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  /** 同一次冲突只自动刷新一次，避免服务端持续返回冲突时形成刷新风暴。 */
  const conflictRefreshRef = useRef(false);
  const conflictCallbackRef = useRef(onVersionConflict);
  conflictCallbackRef.current = onVersionConflict;

  const fetchTask = useMemo<TaskFetcher>(() => {
    if (fetcher) return fetcher;
    return (id, signal) => taskApi.get(id, { signal });
  }, [fetcher]);

  // 用户变化（切换账号 / 401 清会话）→ 丢弃本地状态与在途请求。
  useEffect(() => {
    return onSessionChange((session) => {
      const nextUser = session?.userId ?? null;
      setUserId((previous) => (previous === nextUser ? previous : nextUser));
    });
  }, []);

  const resetForUserChange = useCallback(() => {
    generationRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    setView(null);
    setError(null);
    setConflict(null);
    setPendingActions([]);
    setLastUpdatedAt(null);
    viewRevisionRef.current = null;
    conflictRefreshRef.current = false;
  }, []);

  useEffect(() => {
    resetForUserChange();
  }, [userId, resetForUserChange]);

  const load = useCallback(
    async (mode: "initial" | "refresh" | "poll"): Promise<void> => {
      if (!taskId || !enabled) {
        setLoading(false);
        return;
      }
      const generation = generationRef.current;
      abortRef.current?.abort();
      const controller = new AbortController();
      abortRef.current = controller;

      if (mode === "initial") setLoading(true);
      if (mode === "refresh") setRefreshing(true);

      let result: ApiResult<TaskView>;
      try {
        result = await fetchTask(taskId, controller.signal);
      } catch {
        setLoading(false);
        setRefreshing(false);
        return;
      }
      if (generation !== generationRef.current) {
        // 用户已切换或组件已重置：丢弃这次响应。
        return;
      }

      setLoading(false);
      setRefreshing(false);

      if (!result.ok) {
        if (result.error.code === "VERSION_CONFLICT") {
          const detail: VersionConflictDetail = {
            code: "VERSION_CONFLICT",
            message: result.error.message,
            expectedRevision: viewRevisionRef.current ?? -1,
            serverRevision: readServerRevision(result.error),
            detectedAt: new Date().toISOString(),
          };
          setConflict(detail);
          setError(null);
          conflictCallbackRef.current?.(detail);
          // 冲突后立即按真实服务端状态刷新一次（不覆盖、不丢本地动作）。
          if (!conflictRefreshRef.current) {
            conflictRefreshRef.current = true;
            void load("refresh");
          }
          return;
        }
        if (result.error.code === "REQUEST_ABORTED") return;
        setError(result.error);
        return;
      }

      conflictRefreshRef.current = false;
      setError(null);
      viewRevisionRef.current = result.data.revision;
      setView(result.data);
      setLastUpdatedAt(new Date().toISOString());
      const cacheUser = getActiveUserId();
      if (cacheUser) resourceCache.set(cacheKeys.taskView(taskId), cacheUser, result.data);
    },
    [enabled, fetchTask, taskId],
  );

  // 初次加载 + 缓存水合。
  useEffect(() => {
    if (!taskId || !enabled) {
      setLoading(false);
      return;
    }
    const cacheUser = getActiveUserId();
    const cached = cacheUser ? resourceCache.get<TaskView>(cacheKeys.taskView(taskId), cacheUser) : undefined;
    if (cached && viewRevisionRef.current === null) {
      setView(cached);
      viewRevisionRef.current = cached.revision;
    }
    void load("initial");
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, [taskId, enabled, load, userId]);

  // 轮询：终态停止（避免无意义请求），其它状态按真实状态推进。
  useEffect(() => {
    if (!taskId || !enabled) return undefined;
    const status = view ? String(view.status) : null;
    const terminal = isTaskTerminal(status);
    if (terminal && !pollAfterTerminal) return undefined;
    const timer = window.setTimeout(() => {
      void load("poll");
    }, Math.max(250, pollIntervalMs));
    return () => window.clearTimeout(timer);
  }, [taskId, enabled, pollIntervalMs, pollAfterTerminal, view, load, lastUpdatedAt]);

  const refresh = useCallback(async (): Promise<void> => {
    await load("refresh");
  }, [load]);

  const dismissConflict = useCallback(() => {
    conflictRefreshRef.current = false;
    setConflict(null);
  }, []);

  const resolveConflict = useCallback(async (): Promise<void> => {
    conflictRefreshRef.current = false;
    await load("refresh");
    setConflict(null);
  }, [load]);

  const recordLocalAction = useCallback(
    (input: RecordActionInput): LocalConfirmedAction => {
      const action: LocalConfirmedAction = {
        actionId: newClientActionId(),
        kind: input.kind,
        label: input.label,
        at: new Date().toISOString(),
        expectedRevision: input.expectedRevision ?? viewRevisionRef.current ?? -1,
        operationId: input.operationId ?? null,
        acknowledged: input.acknowledged ?? false,
      };
      setPendingActions((previous) => [...previous, action]);
      return action;
    },
    [],
  );

  const clearLocalActions = useCallback(() => setPendingActions([]), []);

  return {
    view,
    loading,
    refreshing,
    error,
    conflict,
    pendingActions,
    lastUpdatedAt,
    refresh,
    dismissConflict,
    resolveConflict,
    recordLocalAction,
    clearLocalActions,
  };
}

function readServerRevision(failure: ApiFailure): number | null {
  const data = failure.envelope?.data as { revision?: unknown } | null | undefined;
  if (data && typeof data.revision === "number") return data.revision;
  const details = failure.details as { server_revision?: unknown } | null | undefined;
  if (details && typeof details.server_revision === "number") return details.server_revision;
  return null;
}