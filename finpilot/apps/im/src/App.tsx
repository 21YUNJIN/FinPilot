import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactElement } from "react";
import {
  AuthorizationCard,
  ChallengeDialog,
  ErrorBanner,
  ModeBadge,
  OperationReceipt,
  SessionGate,
  StatusChip,
  challengeApi,
  challengeAttemptsRemaining,
  clearSessionCache,
  formatDateTime,
  getSession,
  newOperationId,
  onSessionChange,
  operationApi,
  planApi,
  taskApi,
  toneForStatus,
  TASK_STATUS_LABELS,
  useSession,
  useTask,
  type ApiFailure,
  type AuthChallenge,
  type OperationView,
  type Plan,
  type SessionRecord,
  type TaskView,
} from "@finpilot/shared-ui";
import { ConversationList } from "./components/ConversationList";
import { MessageStream } from "./components/MessageStream";

/**
 * IM 风格客户端：左侧会话列表 + 右侧消息流。
 *
 * 与 APP 客户端连接同一后端与同一账号体系（channel="im"）；
 * 消息气泡内嵌授权卡片与回执卡片，确认/验证在气泡里完成。
 * 规则：所有状态来自服务端；授权卡片与回执卡片复用 shared-ui 组件，
 * RED 未验证时气泡内也只出现"进入验证流程"，不出现提交按钮。
 */
export function App(): ReactElement {
  const session = useSession("im");
  const [current, setCurrent] = useState<SessionRecord | null>(() => getSession());
  const [mode, setMode] = useState<string | null>(null);
  const [conversations, setConversations] = useState<TaskView[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [listError, setListError] = useState<ApiFailure | null>(null);
  const [actionError, setActionError] = useState<ApiFailure | null>(null);
  const [challenge, setChallenge] = useState<AuthChallenge | null>(null);
  const [challengeError, setChallengeError] = useState<string | null>(null);
  const [operation, setOperation] = useState<OperationView | null>(null);
  const [busy, setBusy] = useState(false);
  const [mobileListOpen, setMobileListOpen] = useState(false);

  useEffect(() => onSessionChange((next) => setCurrent(next)), []);

  const activeTask = useTask({ taskId: activeId, pollIntervalMs: 2000 });
  const view = activeTask.view;

  useEffect(() => {
    if (view?.mode) setMode(view.mode);
  }, [view?.mode]);

  const loadConversations = useCallback(async () => {
    const result = await taskApi.list({ limit: 20 });
    if (!result.ok) {
      setListError(result.error);
      return;
    }
    setListError(null);
    setConversations(result.data);
    if (!activeId && result.data.length > 0) setActiveId((result.data[0] as TaskView).task_id);
  }, [activeId]);

  useEffect(() => {
    if (!current) return;
    void loadConversations();
  }, [current, loadConversations]);

  const handleLogin = useCallback(
    async (credentials: { user_id: string; password: string }) => session.login({ ...credentials, channel: "im" }),
    [session],
  );

  const createConversation = useCallback(
    async (text: string) => {
      setBusy(true);
      const result = await taskApi.create({ message: text, channel: "im", clientMessageId: newOperationId() });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        return;
      }
      setActionError(null);
      setActiveId(result.data.task_id);
      setMode(result.data.mode ?? null);
      await loadConversations();
    },
    [loadConversations],
  );

  const sendMessage = useCallback(
    async (text: string) => {
      if (!view) return;
      setBusy(true);
      const result = await taskApi.sendMessage(view.task_id, {
        message: text,
        expectedRevision: view.revision,
        operationId: newOperationId(),
        channel: "im",
      });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        return;
      }
      setActionError(null);
      await activeTask.refresh();
    },
    [activeTask, view],
  );

  const confirmPlan = useCallback(
    async (plan: Plan) => {
      if (!view) return;
      setBusy(true);
      const result = await planApi.confirm(plan.plan_id, {
        scopeHash: plan.scope_hash,
        expectedRevision: view.revision,
        idempotencyKey: newOperationId(),
      });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        return;
      }
      setActionError(null);
      if (result.data.challengeId) {
        setChallenge({
          challenge_id: result.data.challengeId,
          user_id: "",
          operation_scope_hash: plan.scope_hash,
          factor_type: "totp",
          status: "pending",
          expires_at: new Date(Date.now() + 120_000).toISOString(),
          attempts: 0,
          max_attempts: 5,
          plan_id: plan.plan_id,
          plan_revision: plan.revision,
        });
        setChallengeError(null);
        return;
      }
      await activeTask.refresh();
    },
    [activeTask, view],
  );

  const submitCode = useCallback(
    async (code: string) => {
      if (!challenge) return;
      const result = await challengeApi.verify(challenge.challenge_id, code);
      if (!result.ok) {
        setChallengeError(result.error.message);
        return;
      }
      setChallengeError(null);
      setChallenge(null);
      await activeTask.refresh();
    },
    [activeTask, challenge],
  );

  const openReceipt = useCallback(
    async (operationId: string) => {
      setBusy(true);
      const result = await operationApi.get(operationId);
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        return;
      }
      setActionError(null);
      setOperation(result.data);
    },
    [],
  );

  const sidebar = useMemo(
    () => (
      <ConversationList
        conversations={conversations}
        activeId={activeId}
        onSelect={(taskId) => {
          setActiveId(taskId);
          setOperation(null);
          setMobileListOpen(false);
        }}
        onCreate={(text) => {
          void createConversation(text);
        }}
        busy={busy}
      />
    ),
    [activeId, busy, conversations, createConversation],
  );

  if (!current) {
    return (
      <SessionGate
        authenticated={false}
        channel="im"
        mode={mode}
        title="融策智能体 FinPilot"
        subtitle="IM 客户端 · 与 APP 共用同一后端与账号体系"
        onLogin={handleLogin}
      >
        <></>
      </SessionGate>
    );
  }

  return (
    <div className="fp-im" data-testid="im-shell">
      <button
        type="button"
        className="fp-button fp-button--small fp-im__toggle"
        aria-expanded={mobileListOpen}
        onClick={() => setMobileListOpen((open) => !open)}
      >
        会话（{conversations.length}）{mobileListOpen ? " · 收起" : " · 展开"}
      </button>

      <aside className={`fp-im__sidebar${mobileListOpen ? " fp-im__sidebar--open" : ""}`} aria-label="会话列表">
        {sidebar}
      </aside>

      <main className="fp-im__stream" aria-label="消息流">
        <header className="fp-im__stream-header">
          <h1 className="fp-im__title">
            {view?.goal?.goals?.length ? view.goal.goals.join("、") : "融策智能体"}
          </h1>
          {view ? (
            <>
              <StatusChip
                label={TASK_STATUS_LABELS[String(view.status)] ?? `未知状态（${String(view.status)}）`}
                tone={toneForStatus(String(view.status))}
                srPrefix="任务状态"
                rawValue={String(view.status)}
                data-testid="im-task-status"
              />
              <span className="fp-im__revision">修订 r{view.revision}</span>
              <span className="fp-im__channel">渠道：{view.channel}</span>
            </>
          ) : null}
          <ModeBadge mode={view?.mode ?? mode} size="sm" source="任务视图" data-testid="im-mode-badge" />
        </header>

        {listError ? <ErrorBanner failure={listError} context="会话列表" onRetry={() => void loadConversations()} /> : null}
        {actionError ? <ErrorBanner failure={actionError} context="消息操作" onDismiss={() => setActionError(null)} /> : null}

        <MessageStream
          task={view}
          loading={activeTask.loading}
          busy={busy}
          onSend={(text) => {
            void sendMessage(text);
          }}
          onConfirmPlan={(plan) => {
            void confirmPlan(plan);
          }}
          onVerifyPlan={(plan) => {
            void confirmPlan(plan);
          }}
          onQueryOperation={(operationId) => {
            void openReceipt(operationId);
          }}
        />

        {operation ? (
          <div className="fp-im__bubble fp-im__bubble--card" data-testid="im-receipt-bubble">
            <span className="fp-im__bubble-meta">系统 · {formatDateTime(new Date().toISOString())}</span>
            <OperationReceipt
              operation={operation}
              querying={busy}
              onQueryOriginalOrder={(operationId) => {
                void openReceipt(operationId);
              }}
            />
          </div>
        ) : null}

        {view ? (
          <div className="fp-im__footer">
            <span className="fp-im__waiting">
              {view.waiting_reason
                ? `等待原因：${view.waiting_reason.safe_message}（${view.waiting_reason.code}）`
                : "当前没有等待原因。"}
            </span>
          </div>
        ) : null}

        {challenge ? (
          <ChallengeDialog
            challengeId={challenge.challenge_id}
            expiresAt={challenge.expires_at}
            attemptsRemaining={challengeAttemptsRemaining(challenge)}
            lockedUntil={challenge.locked_until ?? null}
            serverError={challengeError}
            serverErrorCode="CHALLENGE_FAILED"
            onSubmit={submitCode}
            onCancel={() => {
              setChallenge(null);
              setChallengeError(null);
            }}
            purpose="红色等级操作提交前的强验证"
          />
        ) : null}

        <div className="fp-im__session">
          <span>已登录：{current.displayName ? `${current.displayName}（${current.userId}）` : current.userId}</span>
          <button
            type="button"
            className="fp-button fp-button--small"
            onClick={() => {
              clearSessionCache("im_logout");
              setConversations([]);
              setActiveId(null);
              setOperation(null);
            }}
          >
            退出登录
          </button>
        </div>
      </main>
    </div>
  );
}

/** 仅用于让 TS 知道 FormEvent 被使用（消息输入在 MessageStream 内）。 */
export type ImComposerEvent = FormEvent<HTMLFormElement>;