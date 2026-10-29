import { useEffect, useId, useRef, useState, type FormEvent, type ReactElement } from "react";
import { formatCountdown, formatDateTime, secondsUntil } from "../format/datetime";
import { ErrorBanner } from "./ErrorBanner";
import { cx } from "../lib/cx";

/**
 * 强验证对话框（A03：RED 操作的真实验证入口）。
 *
 * 硬规则：
 * - 验证码只通过**独立请求**提交（由 onVerify 的调用方发 `/v1/auth/challenges/{id}/verify`），
 *   绝不写入对话消息、模型上下文、前端日志或页面文本；
 * - 输入内容在提交后被清空，组件不渲染、不缓存、不回显验证码；
 * - 处理过期（120 秒）与尝试锁定（最多 5 次，锁定 15 分钟）两种真实状态；
 * - 过期/锁定时提交按钮禁用，并给出"重新发起验证"或"稍后再试"的可执行动作。
 */
export interface ChallengeDialogProps {
  challengeId: string;
  /** 挑战过期时间（ISO-8601）。 */
  expiresAt?: string | null;
  attemptsRemaining?: number | null;
  maxAttempts?: number;
  /** 锁定截止时间（ISO-8601）；未到则禁止提交。 */
  lockedUntil?: string | null;
  /** 服务端返回的失败提示（过期/锁定/验证码错误）。 */
  serverError?: string | null;
  /** 与服务端提示对应的错误码，默认按锁定提示处理。 */
  serverErrorCode?: string | null;
  busy?: boolean;
  purpose?: string;
  codeLength?: number;
  onSubmit: (code: string) => void | Promise<void>;
  onCancel?: () => void;
  onReissue?: () => void;
  className?: string;
  "data-testid"?: string;
}

export function ChallengeDialog({
  challengeId,
  expiresAt,
  attemptsRemaining,
  maxAttempts = 5,
  lockedUntil,
  serverError,
  serverErrorCode,
  busy = false,
  purpose,
  codeLength = 6,
  onSubmit,
  onCancel,
  onReissue,
  className,
  "data-testid": dataTestId = "challenge-dialog",
}: ChallengeDialogProps): ReactElement {
  const [code, setCode] = useState("");
  const [localError, setLocalError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [now, setNow] = useState<number>(() => Date.now());
  const inputId = useId();
  const helpId = useId();
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    if (!expiresAt && !lockedUntil) return undefined;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [expiresAt, lockedUntil]);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const remaining = expiresAt ? secondsUntil(expiresAt, now) : null;
  const expired = remaining !== null && remaining <= 0;
  const lockRemaining = lockedUntil ? secondsUntil(lockedUntil, now) : null;
  const locked = (lockRemaining !== null && lockRemaining > 0) || attemptsRemaining === 0;
  const blocked = expired || locked || busy || submitting;

  async function handleSubmit(event: FormEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    if (blocked) return;
    const trimmed = code.trim();
    if (!new RegExp(`^\\d{${codeLength}}$`).test(trimmed)) {
      setLocalError(`请输入 ${codeLength} 位数字动态验证码。`);
      return;
    }
    setLocalError(null);
    setSubmitting(true);
    try {
      await onSubmit(trimmed);
      // 提交后立即清空：验证码不保留在组件状态或 DOM 中。
      setCode("");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div
      className={cx("fp-card", "fp-challenge", className)}
      role="dialog"
      aria-modal="true"
      aria-labelledby={`${inputId}-title`}
      data-testid={dataTestId}
      data-challenge-id={challengeId}
      data-expired={expired ? "true" : "false"}
      data-locked={locked ? "true" : "false"}
    >
      <h2 className="fp-card__title" id={`${inputId}-title`}>
        强验证（动态验证码 TOTP）
      </h2>
      {purpose ? <p className="fp-challenge__purpose">验证用途：{purpose}</p> : null}
      <p className="fp-challenge__privacy" data-testid="challenge-privacy-note">
        验证码仅用于本次验证的独立请求，不会进入对话消息、模型上下文或前端日志，也不会显示在页面上。
        请不要截图或录屏。
      </p>

      <dl className="fp-challenge__meta">
        <div>
          <dt>验证编号</dt>
          <dd>
            <code data-testid="challenge-id">{challengeId}</code>
          </dd>
        </div>
        <div>
          <dt>有效期</dt>
          <dd data-testid="challenge-expiry">
            {expiresAt ? (
              expired ? (
                <>已过期（{formatDateTime(expiresAt)}）</>
              ) : (
                <>
                  剩余 {formatCountdown(remaining)} · 截止 {formatDateTime(expiresAt)}
                </>
              )
            ) : (
              "不适用（服务端未提供截止时间）"
            )}
          </dd>
        </div>
        <div>
          <dt>尝试次数</dt>
          <dd data-testid="challenge-attempts">
            {typeof attemptsRemaining === "number"
              ? `剩余 ${attemptsRemaining} / ${maxAttempts} 次`
              : `最多 ${maxAttempts} 次（剩余次数未提供）`}
          </dd>
        </div>
        {locked ? (
          <div>
            <dt>锁定</dt>
            <dd data-testid="challenge-locked">
              {lockRemaining && lockRemaining > 0
                ? `尝试次数过多，已锁定至 ${formatDateTime(lockedUntil)}（剩余 ${formatCountdown(lockRemaining)}）`
                : "尝试次数已用尽，请重新发起验证"}
            </dd>
          </div>
        ) : null}
      </dl>

      {expired ? (
        <p className="fp-challenge__expired" role="status" data-testid="challenge-expired-message">
          验证已过期（超过有效期），本次验证码已失效；请重新发起验证后再提交。
        </p>
      ) : null}

      <form className="fp-challenge__form" onSubmit={handleSubmit}>
        <label className="fp-challenge__label" htmlFor={inputId}>
          {codeLength} 位动态验证码
        </label>
        <input
          id={inputId}
          ref={inputRef}
          className="fp-challenge__input"
          data-testid="challenge-code-input"
          name="totp-code"
          type="text"
          inputMode="numeric"
          autoComplete="one-time-code"
          pattern="[0-9]*"
          maxLength={codeLength}
          value={code}
          aria-describedby={helpId}
          aria-invalid={localError ? true : undefined}
          disabled={blocked}
          onChange={(event) => setCode(event.target.value.replace(/\D/g, "").slice(0, codeLength))}
        />
        <p className="fp-challenge__help" id={helpId}>
          验证码来自你的验证器应用；本页面不会把它写入任何日志或会话记录。
        </p>
        {localError ? (
          <p className="fp-challenge__error" role="alert">
            {localError}
          </p>
        ) : null}
        {serverError ? (
          <ErrorBanner code={serverErrorCode ?? "CHALLENGE_LOCKED"} message={serverError} showCode={false} />
        ) : null}
        <div className="fp-challenge__actions">
          <button
            type="submit"
            className="fp-button fp-button--primary"
            disabled={blocked}
            aria-disabled={blocked}
            data-testid="challenge-submit"
          >
            {submitting ? "正在验证…" : "提交验证"}
          </button>
          {onReissue ? (
            <button type="button" className="fp-button" onClick={onReissue} disabled={submitting}>
              重新发起验证
            </button>
          ) : null}
          {onCancel ? (
            <button
              type="button"
              className="fp-button"
              onClick={() => {
                setCode("");
                onCancel();
              }}
              disabled={submitting}
            >
              取消（不提交任何操作）
            </button>
          ) : null}
        </div>
      </form>
      <p className="fp-challenge__footer">
        连续失败会触发锁定（示例策略：最多 {maxAttempts} 次，锁定 15 分钟）。锁定期间不会提交任何业务操作。
      </p>
    </div>
  );
}