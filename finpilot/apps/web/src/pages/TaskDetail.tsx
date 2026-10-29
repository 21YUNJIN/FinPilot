import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import {
  ModeBadge,
  TaskPanel,
  ChallengeDialog,
  AuthorizationCard,
  ErrorBanner,
  VersionConflictNotice,
  challengeApi,
  challengeAttemptsRemaining,
  grantApi,
  newOperationId,
  planApi,
  taskApi,
  useTask,
  type ApiFailure,
  type AuthChallenge,
  type Grant,
  type Plan,
  type TaskView,
} from "@finpilot/shared-ui";
import { useAppState } from "../state";

/**
 * 任务详情（对话 + 计划 + 授权 + 验证 + 结果）——对话页、任务页、确认页共用。
 *
 * 规则：
 * - 所有写操作都带 expected_revision（来自当前任务视图）与 operation_id（客户端幂等键）；
 * - RED 计划的确认会返回 challenge：只有验证通过后才会出现"确认提交"；
 * - 验证码只通过 /v1/auth/challenges/{id}/verify 提交，不在界面回显、不写入对话；
 * - 版本冲突时展示冲突提示并保留已确认动作，只允许刷新到最新修订。
 */
export interface TaskDetailProps {
  taskId: string;
  focus?: "confirm" | "task";
  onMode?: (mode: string | null) => void;
}

export function TaskDetail({ taskId, focus = "task", onMode }: TaskDetailProps): ReactElement {
  const { setError } = useAppState();
  const [busy, setBusy] = useState(false);
  const [challenge, setChallenge] = useState<AuthChallenge | null>(null);
  const [challengeError, setChallengeError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<ApiFailure | null>(null);

  const task = useTask({ taskId, pollIntervalMs: 2000 });
  const view: TaskView | null = task.view;

  useEffect(() => {
    if (view?.mode) onMode?.(view.mode);
  }, [onMode, view?.mode]);

  const revision = view?.revision ?? 0;

  const refresh = useCallback(async () => {
    await task.refresh();
  }, [task]);

  const confirmPlan = useCallback(
    async (plan: Plan) => {
      if (!view) return;
      setBusy(true);
      setActionError(null);
      const operationId = newOperationId();
      task.recordLocalAction({
        kind: "confirm_plan",
        label: `已确认计划 r${plan.revision}`,
        operationId,
      });
      const result = await planApi.confirm(plan.plan_id, {
        scopeHash: plan.scope_hash,
        expectedRevision: revision,
        idempotencyKey: operationId,
      });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        setError(result.error);
        return;
      }
      if (result.data.challengeId) {
        // RED：服务端要求强验证，先进入验证流程（此时没有任何资金效果）。
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
      await refresh();
    },
    [refresh, revision, setError, task, view],
  );

  const submitCode = useCallback(
    async (code: string) => {
      if (!challenge) return;
      setChallengeError(null);
      const result = await challengeApi.verify(challenge.challenge_id, code);
      if (!result.ok) {
        // 只展示后端安全文案，不回显验证码。
        setChallengeError(result.error.message);
        if (result.error.code === "CHALLENGE_LOCKED" || result.error.code === "CHALLENGE_EXPIRED") {
          setChallenge((previous) =>
            previous
              ? {
                  ...previous,
                  status: result.error.code === "CHALLENGE_LOCKED" ? "locked" : "expired",
                  locked_until:
                    result.error.code === "CHALLENGE_LOCKED"
                      ? new Date(Date.now() + 900_000).toISOString()
                      : previous.locked_until,
                }
              : previous,
          );
        }
        return;
      }
      task.recordLocalAction({ kind: "verify_challenge", label: "已完成强验证（TOTP）" });
      setChallenge(null);
      await refresh();
    },
    [challenge, refresh, task],
  );

  const cancelTask = useCallback(async () => {
    if (!view) return;
    setBusy(true);
    const result = await taskApi.cancel(taskId, { expectedRevision: revision, operationId: newOperationId() });
    setBusy(false);
    if (!result.ok) {
      setActionError(result.error);
      setError(result.error);
      return;
    }
    setActionError(null);
    await refresh();
  }, [refresh, revision, setError, taskId, view]);

  const resumeTask = useCallback(async () => {
    if (!view) return;
    setBusy(true);
    const result = await taskApi.resume(taskId, { expectedRevision: revision, operationId: newOperationId() });
    setBusy(false);
    if (!result.ok) {
      setActionError(result.error);
      setError(result.error);
      return;
    }
    setActionError(null);
    await refresh();
  }, [refresh, revision, setError, taskId, view]);

  const revokeGrant = useCallback(
    async (grant: Grant) => {
      setBusy(true);
      const result = await grantApi.revoke(grant.grant_id, { expectedRevision: revision, operationId: newOperationId() });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        setError(result.error);
        return;
      }
      setActionError(null);
      await refresh();
    },
    [refresh, revision, setError],
  );

  const sendMessage = useCallback(
    async (text: string) => {
      if (!view) return;
      setBusy(true);
      const operationId = newOperationId();
      task.recordLocalAction({ kind: "send_message", label: `已发送指令：${text.slice(0, 40)}`, operationId });
      const result = await taskApi.sendMessage(taskId, {
        message: text,
        expectedRevision: revision,
        operationId,
        channel: "app",
      });
      setBusy(false);
      if (!result.ok) {
        setActionError(result.error);
        // VERSION_CONFLICT 由 useTask 的冲突提示统一展示；这里不重复弹错误条。
        if (result.error.code !== "VERSION_CONFLICT") setError(result.error);
        return;
      }
      setActionError(null);
      await refresh();
    },
    [refresh, revision, setError, task, taskId, view],
  );

  return (
    <div className="web-task-detail">
      {focus === "confirm" ? (
        <header className="web-section-header">
          <h1 className="web-section-title">确认与验证</h1>
          <p className="web-section-hint">
            这里展示与 scope_hash 同源的参数；红色等级必须完成强验证（TOTP）后才会提交。
          </p>
        </header>
      ) : null}

      {task.conflict ? (
        <VersionConflictNotice
          conflict={task.conflict}
          onRefresh={() => {
            void task.resolveConflict();
          }}
          onDismiss={task.dismissConflict}
          pendingActions={task.pendingActions}
          refreshing={task.refreshing}
        />
      ) : null}

      {actionError ? <ErrorBanner failure={actionError} context="任务操作" onDismiss={() => setActionError(null)} /> : null}

      <TaskPanel
        task={view}
        loading={task.loading}
        error={task.error}
        busy={busy}
        onSendMessage={(text) => {
          void sendMessage(text);
        }}
        onConfirmPlan={(plan) => {
          void confirmPlan(plan);
        }}
        onVerifyPlan={(plan) => {
          // 用户从授权卡片进入验证：先发起确认，服务端返回 challenge 后弹出验证框。
          void confirmPlan(plan);
        }}
        onStopTask={() => {
          void cancelTask();
        }}
        onResumeTask={() => {
          void resumeTask();
        }}
        onRevokeGrant={(grant) => {
          void revokeGrant(grant);
        }}
        onViewOperation={(operationId) => {
          window.location.hash = `/result/${operationId}`;
        }}
        onRefresh={() => {
          void refresh();
        }}
      />

      {focus === "confirm" ? (
        <p className="web-section-hint" data-testid="confirm-focus-hint">
          下方授权卡片与执行步骤来自服务端同一份计划；红色等级必须先完成强验证，验证码不会进入对话或日志。
        </p>
      ) : null}

      {challenge ? (
        <ChallengeDialog
          challengeId={challenge.challenge_id}
          expiresAt={challenge.expires_at}
          attemptsRemaining={challengeAttemptsRemaining(challenge)}
          maxAttempts={challenge.max_attempts ?? 5}
          lockedUntil={challenge.locked_until ?? null}
          serverError={challengeError}
          serverErrorCode={challenge.status === "locked" ? "CHALLENGE_LOCKED" : "CHALLENGE_FAILED"}
          busy={busy}
          purpose="红色等级操作提交前的强验证"
          onSubmit={submitCode}
          onCancel={() => {
            setChallenge(null);
            setChallengeError(null);
          }}
        />
      ) : null}
    </div>
  );
}