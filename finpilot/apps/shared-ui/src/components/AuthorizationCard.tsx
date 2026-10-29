import type { ReactElement } from "react";
import type { ScopeSummary } from "../types";
import { AUTH_LEVEL_ACTIONS, AUTH_LEVEL_LABELS, AUTH_LEVEL_SHORT_LABELS } from "../contract/labels";
import { formatDateTime, formatTimeWindow } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, type StatusTone } from "./StatusChip";
import { ErrorBanner } from "./ErrorBanner";
import { cx } from "../lib/cx";

/**
 * 授权卡片（A03，最关键的用户决策入口）。
 *
 * 必须展示（与后端 scope_hash 同源）：等级、金额、费用、对象、时间窗、不可逆影响、scope_hash。
 *
 * 硬规则：
 * - GREEN：无需用户确认，服务端完成身份与归属检查后执行；
 * - YELLOW：必须用户明确确认（订阅查询按官方黄色例外）；
 * - RED：**必须进入真实验证流程**；`verified !== true` 时只能显示"进入验证流程"，
 *   不得渲染提交按钮，也不得出现任何完成/成功字样；
 * - 若调用方在 RED 未验证时传入成功类 `resultStatus`，卡片显示状态不一致警告并丢弃该结果；
 * - 参数变化会产生新的 scope_hash，因此卡片明确写出"参数以服务端 scope 为准"。
 */
export interface AuthorizationCardProps {
  level: string;
  summary: ScopeSummary;
  /** 覆盖 summary.scope_hash（两者不一致时以显式传入为准）。 */
  scopeHash?: string | null;
  title?: string;
  status?: "pending" | "verifying" | "confirmed" | "expired" | "revoked" | "rejected" | string;
  /** 服务端是否已完成强验证（RED 的必要条件）。 */
  verified?: boolean;
  busy?: boolean;
  disabled?: boolean;
  /** 服务端返回的失败提示（例如验证过期、授权超出范围）。 */
  errorMessage?: string | null;
  /** 结果状态；RED 未验证时传入完成态会被显式拒绝展示。 */
  resultStatus?: string | null;
  revocable?: boolean;
  onConfirm?: () => void;
  onVerify?: () => void;
  onRevoke?: () => void;
  className?: string;
  "data-testid"?: string;
}

const STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "待确认",
  verifying: "验证中",
  confirmed: "已确认（等待服务端提交）",
  expired: "已过期",
  revoked: "已撤销",
  rejected: "已拒绝",
};

const STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  pending: "pending",
  verifying: "pending",
  confirmed: "info",
  expired: "neutral",
  revoked: "neutral",
  rejected: "danger",
};

/** 会被视为"完成态"的结果值（RED 未验证时禁止展示）。 */
const COMPLETION_LIKE_RESULTS = new Set([
  "succeeded",
  "settled",
  "completed",
  "confirmed",
  "accepted",
  "partially_succeeded",
  "partial",
  "pending",
]);

function counterpartyText(counterparty: ScopeSummary["counterparty"]): string {
  if (counterparty === null || counterparty === undefined || counterparty === "") return "不适用";
  if (typeof counterparty === "string") return counterparty;
  const parts = [counterparty.name, counterparty.bank, counterparty.account_id, counterparty.payee_id].filter(
    (part): part is string => typeof part === "string" && part.trim() !== "",
  );
  return parts.length > 0 ? parts.join(" · ") : "不适用";
}

export function AuthorizationCard({
  level,
  summary,
  scopeHash,
  title = "授权确认",
  status = "pending",
  verified,
  busy = false,
  disabled = false,
  errorMessage,
  resultStatus,
  revocable = false,
  onConfirm,
  onVerify,
  onRevoke,
  className,
  "data-testid": dataTestId = "authorization-card",
}: AuthorizationCardProps): ReactElement {
  const levelKey = String(level ?? "").trim().toUpperCase();
  const levelLabel = AUTH_LEVEL_LABELS[levelKey] ?? `未知等级（${String(level)}）：不得自动执行`;
  const levelAction = AUTH_LEVEL_ACTIONS[levelKey] ?? "等级未登记：请以服务端策略判定为准，前端不代替决策。";
  const isRed = levelKey === "RED";
  const isYellow = levelKey === "YELLOW";
  const isGreen = levelKey === "GREEN";

  const verificationDone = verified === true;
  const needsVerification = isRed && !verificationDone;
  const effectiveScopeHash = scopeHash ?? summary.scope_hash ?? null;

  const completionConflict =
    needsVerification && !!resultStatus && COMPLETION_LIKE_RESULTS.has(String(resultStatus).trim().toLowerCase());

  const currency = summary.currency ?? "CNY";
  const submitDisabled = disabled || busy;
  const statusKey = String(status ?? "pending");

  return (
    <section
      className={cx("fp-card", "fp-auth", `fp-auth--${levelKey.toLowerCase() || "unknown"}`, className)}
      aria-label={`${title}（${levelKey || "未知等级"}）`}
      data-testid={dataTestId}
      data-level={levelKey}
      data-verified={verificationDone ? "true" : "false"}
      data-needs-verification={needsVerification ? "true" : "false"}
    >
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
        <StatusChip
          label={`${AUTH_LEVEL_SHORT_LABELS[levelKey] ?? `等级 ${levelKey || "未知"}`}`}
          tone={levelKey === "RED" ? "danger" : levelKey === "YELLOW" ? "warning" : "success"}
          srPrefix="授权等级"
          rawValue={String(level)}
          data-testid="authorization-level-chip"
        />
        <StatusChip
          label={STATUS_LABELS[statusKey] ?? `未知状态（${statusKey}）`}
          tone={STATUS_TONES[statusKey] ?? "info"}
          srPrefix="授权状态"
          rawValue={statusKey}
          data-testid="authorization-status-chip"
        />
      </header>

      <p className="fp-auth__level-text">{levelLabel}</p>
      <p className="fp-auth__level-action">{levelAction}</p>

      {completionConflict ? (
        <div className="fp-auth__conflict" role="alert" data-testid="authorization-red-guard">
          <p>
            状态不一致：红色等级操作在强验证通过前不得展示完成态。已忽略调用方传入的结果状态
            <code>{String(resultStatus)}</code>，请以服务端回执与操作查询为准。
          </p>
        </div>
      ) : null}

      <dl className="fp-auth__scope">
        <div className="fp-auth__row">
          <dt>金额</dt>
          <dd>
            <MoneyText
              amountMinor={summary.amount_minor ?? null}
              currency={currency}
              fallback="不适用"
              data-testid="authorization-amount"
            />
          </dd>
        </div>
        <div className="fp-auth__row">
          <dt>手续费 / 费用</dt>
          <dd>
            <MoneyText amountMinor={summary.fee_minor ?? null} currency={currency} fallback="不适用" />
          </dd>
        </div>
        <div className="fp-auth__row">
          <dt>不可退成本</dt>
          <dd>
            <MoneyText
              amountMinor={summary.irreversible_cost_minor ?? null}
              currency={currency}
              fallback="不适用"
            />
          </dd>
        </div>
        <div className="fp-auth__row">
          <dt>对象 / 收款方</dt>
          <dd data-testid="authorization-counterparty">{counterpartyText(summary.counterparty)}</dd>
        </div>
        <div className="fp-auth__row">
          <dt>业务对象</dt>
          <dd>{summary.object_label && summary.object_label.trim() !== "" ? summary.object_label : "不适用"}</dd>
        </div>
        <div className="fp-auth__row">
          <dt>付款账户</dt>
          <dd>{summary.source_account_id ?? "不适用"}</dd>
        </div>
        <div className="fp-auth__row">
          <dt>时间窗</dt>
          <dd>{formatTimeWindow(summary.time_window ?? null)}</dd>
        </div>
        <div className="fp-auth__row">
          <dt>不可逆影响</dt>
          <dd data-testid="authorization-irreversible">
            {summary.irreversible === true ? "不可撤销：是" : summary.irreversible === false ? "按条款可撤销" : "不适用（未标注）"}
            {summary.irreversible_impacts && summary.irreversible_impacts.length > 0 ? (
              <ul className="fp-auth__impacts">
                {summary.irreversible_impacts.map((impact) => (
                  <li key={impact}>{impact}</li>
                ))}
              </ul>
            ) : null}
          </dd>
        </div>
        <div className="fp-auth__row">
          <dt>取消条件</dt>
          <dd>
            {summary.cancel_conditions && summary.cancel_conditions.length > 0
              ? summary.cancel_conditions.join("；")
              : "不适用（未标注）"}
          </dd>
        </div>
        {summary.notes && summary.notes.length > 0 ? (
          <div className="fp-auth__row">
            <dt>补充说明</dt>
            <dd>{summary.notes.join("；")}</dd>
          </div>
        ) : null}
        {summary.extra && Object.keys(summary.extra).length > 0 ? (
          <div className="fp-auth__row">
            <dt>其他参数（原样展示）</dt>
            <dd>
              {Object.entries(summary.extra)
                .map(([key, value]) => `${key}=${typeof value === "string" ? value : JSON.stringify(value)}`)
                .join("；")}
            </dd>
          </div>
        ) : null}
      </dl>

      <p className="fp-auth__scope-hash">
        授权范围摘要 scope_hash：
        {effectiveScopeHash ? (
          <code data-testid="authorization-scope-hash">{effectiveScopeHash}</code>
        ) : (
          <span className="fp-auth__missing-hash">未提供（无法核对范围，请先刷新）</span>
        )}
      </p>
      <p className="fp-auth__scope-note">
        本卡片展示的参数与服务端 scope 同源；金额、对象或时间窗变化都会产生新的 scope_hash，
        需要重新确认（旧的授权不会被复用）。操作编号（幂等键）在重试时保持不变。
      </p>
      {summary.tool ? (
        <p className="fp-auth__tool">
          工具：<code>{summary.tool}</code>
        </p>
      ) : null}

      {needsVerification ? (
        <div className="fp-auth__verify-required" role="status" aria-live="polite" data-testid="authorization-verify-required">
          <p>
            红色等级：必须完成强验证（TOTP）后，服务端才会受理该操作。当前状态为"未验证、未提交"。
          </p>
          <p className="fp-auth__verify-note">
            验证码通过独立请求提交，不进入对话消息、模型上下文或前端日志，也不会显示在本页面。
          </p>
        </div>
      ) : null}

      {isRed && verificationDone ? (
        <p className="fp-auth__verified" role="status" data-testid="authorization-verified">
          强验证已通过：可以确认提交（仍以服务端回执为准，受理不等于到账）。
        </p>
      ) : null}

      {isYellow ? (
        <p className="fp-auth__yellow-note">
          黄色等级：需要你明确确认后才会提交；确认只对当前 scope_hash 与当前修订有效。
        </p>
      ) : null}

      {isGreen ? (
        <p className="fp-auth__green-note">
          绿色等级：无需你确认，服务端完成身份与归属检查后执行；如需停止请在任务页发起停止请求。
        </p>
      ) : null}

      {errorMessage ? <ErrorBanner code="CHALLENGE_EXPIRED" message={errorMessage} showCode={false} /> : null}

      <div className="fp-auth__actions">
        {needsVerification ? (
          <button
            type="button"
            className="fp-button fp-button--primary"
            onClick={onVerify}
            disabled={submitDisabled || !onVerify}
            aria-disabled={submitDisabled || !onVerify}
            title={!onVerify ? "当前环境未提供验证入口，请刷新后重试" : undefined}
            data-testid="authorization-verify-button"
          >
            进入验证流程（TOTP）
          </button>
        ) : null}

        {!needsVerification && (isYellow || (isRed && verificationDone)) ? (
          <button
            type="button"
            className="fp-button fp-button--primary"
            onClick={onConfirm}
            disabled={submitDisabled || !onConfirm}
            aria-disabled={submitDisabled || !onConfirm}
            title={!onConfirm ? "当前环境未提供确认入口，请刷新后重试" : undefined}
            data-testid="authorization-confirm-button"
          >
            {isRed ? "确认提交（红色等级，已验证）" : "确认并提交（黄色等级）"}
          </button>
        ) : null}

        {revocable && onRevoke ? (
          <button type="button" className="fp-button" onClick={onRevoke} disabled={busy}>
            撤销已授予的授权
          </button>
        ) : null}
      </div>

      <p className="fp-auth__footer">
        卡片生成时间与观测口径以服务端 provenance 为准
        {summary.time_window && typeof summary.time_window === "object" && summary.time_window.start
          ? `；时间窗起点 ${formatDateTime(summary.time_window.start)}`
          : ""}
        。任何"已受理"都不等于"已到账"。
      </p>
    </section>
  );
}