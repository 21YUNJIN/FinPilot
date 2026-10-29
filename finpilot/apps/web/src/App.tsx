import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import {
  ErrorBanner,
  ModeBadge,
  SessionGate,
  clearSessionCache,
  getSession,
  onSessionChange,
  useSession,
  type ApiFailure,
  type SessionRecord,
} from "@finpilot/shared-ui";
import { AppStateProvider, useAppState, useHashRoute } from "./state";
import { ChatPage } from "./pages/ChatPage";
import { TasksPage } from "./pages/TasksPage";
import { ResultPage } from "./pages/ResultPage";
import { BillsPage, CardsPage, PrivacyPage, SubscriptionsPage, WealthPage } from "./pages/DomainPages";
import { TaskDetail } from "./pages/TaskDetail";

/**
 * APP 式 Web 客户端外壳（移动优先）。
 *
 * 结构：顶部运行模式徽标 + 底部标签导航（对话/任务/卡片/账单/理财/订阅/隐私）；
 * 确认与验证页、结果页为可选路由（由对话页/任务页进入）。
 *
 * 规则：
 * - 登录由 SessionGate 完成（身份只来自服务端会话）；
 * - 顶部模式徽标只显示服务端如实返回的 mode，未取到时显示"未标注"；
 * - 不使用任何假进度动画，加载态都是"正在读取服务端…"文字。
 */
const TABS: Array<{ id: string; label: string; path: string }> = [
  { id: "chat", label: "对话", path: "/chat" },
  { id: "tasks", label: "任务", path: "/tasks" },
  { id: "cards", label: "卡片", path: "/cards" },
  { id: "bills", label: "账单", path: "/bills" },
  { id: "wealth", label: "理财", path: "/wealth" },
  { id: "subscriptions", label: "订阅", path: "/subscriptions" },
  { id: "privacy", label: "隐私", path: "/privacy" },
];

function BottomNav({ current, onNavigate }: { current: string; onNavigate: (path: string) => void }): ReactElement {
  return (
    <nav className="fp-app__nav" aria-label="主导航">
      {TABS.map((tab) => (
        <button
          key={tab.id}
          type="button"
          className="fp-app__nav-item"
          aria-current={current.startsWith(tab.path) ? "page" : undefined}
          onClick={() => onNavigate(tab.path)}
        >
          {tab.label}
        </button>
      ))}
    </nav>
  );
}

function Shell(): ReactElement {
  const session = useSession("app");
  const { mode, reportMode, activeTaskId } = useAppState();
  const [route, navigate] = useHashRoute();
  const [loginError, setLoginError] = useState<ApiFailure | null>(null);

  // 会话在标签页内可恢复（刷新后仍是同一用户；切换用户会清空缓存）。
  const [current, setCurrent] = useState<SessionRecord | null>(() => getSession());
  useEffect(() => onSessionChange((next) => setCurrent(next)), []);

  const handleLogin = useCallback(
    async (credentials: { user_id: string; password: string }) => {
      const failure = await session.login({ ...credentials, channel: "app" });
      setLoginError(failure);
      return failure;
    },
    [session],
  );

  const page = useMemo(() => {
    if (route.startsWith("/result/")) {
      const operationId = route.slice("/result/".length);
      return <ResultPage operationId={operationId} />;
    }
    if (route.startsWith("/confirm/")) {
      const taskId = route.slice("/confirm/".length);
      return <TaskDetail taskId={taskId} focus="confirm" onMode={reportMode} />;
    }
    if (route.startsWith("/task/")) {
      const taskId = route.slice("/task/".length);
      return <TaskDetail taskId={taskId} onMode={reportMode} />;
    }
    switch (route) {
      case "/tasks":
        return <TasksPage />;
      case "/cards":
        return <CardsPage />;
      case "/bills":
        return <BillsPage />;
      case "/wealth":
        return <WealthPage />;
      case "/subscriptions":
        return <SubscriptionsPage />;
      case "/privacy":
        return <PrivacyPage />;
      case "/chat":
      default:
        return <ChatPage />;
    }
  }, [reportMode, route]);

  if (!current) {
    return (
      <SessionGate
        authenticated={false}
        channel="app"
        mode={mode}
        title="融策智能体 FinPilot"
        subtitle="个人银行智能体 · APP 客户端"
        onLogin={handleLogin}
        hint={
          loginError ? (
            <ErrorBanner failure={loginError} context="登录" showCode />
          ) : (
            <p className="fp-gate__note">
              两个客户端（APP 与 IM）连接同一后端与同一账号体系；风险等级、金额与服务端 scope_hash 同源。
            </p>
          )
        }
      >
        <></>
      </SessionGate>
    );
  }

  return (
    <div className="fp-app">
      <header className="fp-app__header">
        <h1 className="fp-app__title">融策智能体</h1>
        <ModeBadge mode={mode} size="sm" source="服务端信封 provenance / 任务视图" data-testid="app-mode-badge" />
        <span className="fp-app__user" data-testid="web-current-user">
          {current.displayName ? `${current.displayName}（${current.userId}）` : current.userId}
        </span>
        <button
          type="button"
          className="fp-button fp-button--small"
          onClick={() => {
            clearSessionCache("user_logout");
            session
              .logout()
              .then(() => navigate("/chat"))
              .catch(() => navigate("/chat"));
          }}
        >
          退出登录
        </button>
      </header>

      <main className="fp-app__main" data-route={route}>
        {activeTaskId && route !== "/chat" ? null : null}
        {page}
      </main>

      <BottomNav current={route} onNavigate={navigate} />
    </div>
  );
}

export function App(): ReactElement {
  return (
    <AppStateProvider>
      <Shell />
    </AppStateProvider>
  );
}

export { TABS as WEB_TABS };