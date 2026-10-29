import { useId, useState, type FormEvent, type ReactElement, type ReactNode } from "react";
import type { ApiFailure } from "../api/client";
import { ErrorBanner } from "./ErrorBanner";
import { ModeBadge } from "./ModeBadge";
import { cx } from "../lib/cx";

/**
 * 登录闸门（两个客户端共用）：未认证时显示登录表单，认证后渲染 children。
 *
 * 规则：
 * - 身份由服务端会话决定（POST /v1/auth/sessions），前端不缓存密码、不存 token 到 localStorage；
 * - 登录失败展示后端安全文案（401 会同时清理本地会话缓存）；
 * - 顶部始终显示运行模式（未标注时写明"不得当作实时运行"）。
 */
export interface SessionGateProps {
  authenticated: boolean;
  channel: string;
  onLogin: (credentials: { user_id: string; password: string }) => Promise<ApiFailure | null>;
  children: ReactNode;
  title?: string;
  subtitle?: string;
  mode?: string | null;
  hint?: ReactNode;
  footer?: ReactNode;
  className?: string;
  "data-testid"?: string;
}

export function SessionGate({
  authenticated,
  channel,
  onLogin,
  children,
  title = "融策智能体 FinPilot",
  subtitle = "个人银行智能体",
  mode,
  hint,
  footer,
  className,
  "data-testid": dataTestId = "session-gate",
}: SessionGateProps): ReactElement {
  const [userId, setUserId] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<ApiFailure | null>(null);
  const userFieldId = useId();
  const passwordFieldId = useId();

  if (authenticated) return <>{children}</>;

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setFailure(null);
    const result = await onLogin({ user_id: userId.trim(), password });
    setBusy(false);
    if (result) {
      setFailure(result);
      return;
    }
    // 登录成功后立刻清空密码字段，避免留在组件状态里。
    setPassword("");
  }

  return (
    <section className={cx("fp-gate", className)} data-testid={dataTestId} aria-labelledby={`${userFieldId}-title`}>
      <header className="fp-gate__header">
        <h1 className="fp-gate__title" id={`${userFieldId}-title`}>
          {title}
        </h1>
        <p className="fp-gate__subtitle">{subtitle}</p>
        <ModeBadge mode={mode ?? null} size="sm" source="登录前尚无法取得业务信封" />
      </header>

      <form className="fp-gate__form" onSubmit={handleSubmit} autoComplete="on">
        <p className="fp-gate__channel">
          渠道：<code>{channel}</code>（两个客户端连接同一后端与同一账号体系）
        </p>
        <label className="fp-gate__label" htmlFor={userFieldId}>
          用户 ID
        </label>
        <input
          id={userFieldId}
          className="fp-gate__input"
          name="username"
          type="text"
          autoComplete="username"
          value={userId}
          onChange={(event) => setUserId(event.target.value)}
          required
          data-testid="login-user-id"
        />

        <label className="fp-gate__label" htmlFor={passwordFieldId}>
          密码
        </label>
        <input
          id={passwordFieldId}
          className="fp-gate__input"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          required
          data-testid="login-password"
        />

        {failure ? <ErrorBanner failure={failure} context="登录" showCode /> : null}

        <button type="submit" className="fp-button fp-button--primary" disabled={busy} data-testid="login-submit">
          {busy ? "正在登录…" : "登录"}
        </button>
        <p className="fp-gate__note">
          身份只来自服务端会话；切换账号会清除本机会话与只读视图缓存，另一个账号看不到上一个账号的数据。
          高风险操作需要单独的强验证（TOTP），验证码不会进入对话或日志。
        </p>
        {hint}
      </form>
      {footer ? <footer className="fp-gate__footer">{footer}</footer> : null}
    </section>
  );
}