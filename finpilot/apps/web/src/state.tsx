import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactElement, type ReactNode } from "react";
import { configureClient, getActiveUserId, type ApiFailure } from "@finpilot/shared-ui";

/**
 * APP 式 Web 客户端的状态层。
 *
 * 只做三件事：会话（来自服务端）、当前运行模式（来自服务端信封/任务视图）、当前选中任务。
 * 业务判定（等级、金额、可行性）全部由后端与 shared-ui 组件负责，这里不做任何推断。
 */
export interface AppState {
  userId: string | null;
  channel: "app";
  /** 最近一次服务端返回的运行模式；未知时为 null（界面会显示"未标注"）。 */
  mode: string | null;
  reportMode: (mode: string | null | undefined) => void;
  activeTaskId: string | null;
  setActiveTaskId: (taskId: string | null) => void;
  error: ApiFailure | null;
  setError: (error: ApiFailure | null) => void;
}

const AppStateContext = createContext<AppState | null>(null);

export function AppStateProvider({ children }: { children: ReactNode }): ReactElement {
  const [userId, setUserId] = useState<string | null>(() => getActiveUserId());
  const [mode, setMode] = useState<string | null>(null);
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);
  const [error, setError] = useState<ApiFailure | null>(null);

  useEffect(() => {
    configureClient({ channel: "app" });
  }, []);

  const reportMode = useCallback((next: string | null | undefined) => {
    if (typeof next === "string" && next.trim() !== "") {
      setMode((previous) => (previous === next ? previous : next));
    }
  }, []);

  useEffect(() => {
    const sync = (): void => setUserId(getActiveUserId());
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, []);

  const value = useMemo<AppState>(
    () => ({
      userId,
      channel: "app",
      mode,
      reportMode,
      activeTaskId,
      setActiveTaskId,
      error,
      setError,
    }),
    [activeTaskId, error, mode, reportMode, userId],
  );

  return <AppStateContext.Provider value={value}>{children}</AppStateContext.Provider>;
}

export function useAppState(): AppState {
  const value = useContext(AppStateContext);
  if (!value) throw new Error("useAppState 必须在 AppStateProvider 内使用");
  return value;
}

/** 极简 hash 路由：'#/tasks' → '/tasks'，'#/result/op_1' → '/result/op_1'。 */
export function useHashRoute(): [string, (path: string) => void] {
  const [path, setPath] = useState<string>(() => readHash());
  useEffect(() => {
    const onChange = (): void => setPath(readHash());
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);
  const navigate = useCallback((next: string) => {
    if (window.location.hash !== `#${next}`) window.location.hash = next;
    setPath(next);
  }, []);
  return [path, navigate];
}

function readHash(): string {
  const raw = window.location.hash.replace(/^#/, "");
  return raw === "" ? "/chat" : raw;
}