import type { ReactElement } from "react";
import type { Clarification, GoalSpec } from "../types";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, type StatusTone } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 目标摘要卡（A02 / PLAN-01：硬约束、偏好、事实必须分离展示）。
 *
 * 数据形状严格对齐 contracts/schemas/goal.json：
 * - 硬约束是一个对象（预算、应急金底线、保护账户组、交付截止、最大确认次数）；
 * - 偏好是成对比较（higher 优先于 lower），没有用户明确优先级时不得自行设权重；
 * - 事实区展示来源证据（evidence）与显式标注的推断项（inferred_fields）；
 * - `unresolved` 非空时必须提示"未澄清前零写入"；
 * - 金额一律走 MoneyText（整数分）。
 */
export interface GoalSummaryCardProps {
  goal?: GoalSpec | null;
  clarifications?: Clarification[];
  title?: string;
  /** 用户改意入口：会产生新 revision，旧授权随之失效。 */
  onEdit?: () => void;
  /** 澄清选项回调（有限选择 + 保留自由输入由外层输入框负责）。 */
  onAnswer?: (clarification: Clarification, value: string) => void;
  referenceTime?: string | null;
  className?: string;
  "data-testid"?: string;
}

const AUTHORIZATION_STATUS_LABELS: Readonly<Record<string, string>> = {
  not_requested: "授权未申请",
  pending: "授权待确认",
  granted: "授权已授予",
  revoked: "授权已撤销",
  expired: "授权已过期",
};

const AUTHORIZATION_STATUS_TONES: Readonly<Record<string, StatusTone>> = {
  not_requested: "neutral",
  pending: "pending",
  granted: "success",
  revoked: "neutral",
  expired: "warning",
};

export function GoalSummaryCard({
  goal,
  clarifications = [],
  title = "目标摘要",
  onEdit,
  onAnswer,
  referenceTime,
  className,
  "data-testid": dataTestId = "goal-summary",
}: GoalSummaryCardProps): ReactElement {
  if (!goal) {
    return (
      <section className={cx("fp-card", "fp-goal", className)} aria-label={title} data-testid={dataTestId}>
        <header className="fp-card__header">
          <h2 className="fp-card__title">{title}</h2>
        </header>
        <p className="fp-goal__empty">尚未生成目标摘要（服务端还未返回 GoalSpec）。</p>
      </section>
    );
  }

  const constraints = goal.hard_constraints;
  const preferences = goal.preferences ?? [];
  const unresolved = goal.unresolved ?? [];
  const inferred = goal.inferred_fields ?? [];
  const evidence = goal.evidence ?? [];
  const blockingClarifications = clarifications.filter((item) => item.blocking && item.status === "open");
  const authorizationStatus = String(goal.authorization_status ?? "");

  return (
    <section className={cx("fp-card", "fp-goal", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
        <StatusChip
          label={AUTHORIZATION_STATUS_LABELS[authorizationStatus] ?? `未登记授权状态（${authorizationStatus}）`}
          tone={AUTHORIZATION_STATUS_TONES[authorizationStatus] ?? "info"}
          srPrefix="授权状态"
          rawValue={authorizationStatus}
          data-testid="goal-authorization-status"
        />
        <span className="fp-goal__revision">修订 r{goal.revision}</span>
        {onEdit ? (
          <button type="button" className="fp-button fp-button--small" onClick={onEdit}>
            修改目标（产生新修订）
          </button>
        ) : null}
      </header>

      <p className="fp-goal__objective">
        <span className="fp-goal__objective-label">目标</span>
        {goal.goals && goal.goals.length > 0 ? goal.goals.join("、") : "目标标识未提供"}
      </p>
      <p className="fp-goal__reference">
        参考时间（Asia/Shanghai）：{formatDateTime(referenceTime ?? goal.reference_time)}
      </p>

      <section className="fp-goal__section" aria-labelledby="goal-hard-constraints">
        <h3 className="fp-goal__section-title" id="goal-hard-constraints">
          硬约束
        </h3>
        <p className="fp-goal__section-hint">不可由智能体自行放宽；变更需重新确认（新修订 + 新 scope_hash）。</p>
        <dl className="fp-goal__list" data-testid="goal-hard-constraints-list">
          <div className="fp-goal__row">
            <dt className="fp-goal__label">总预算</dt>
            <dd>
              <MoneyText amountMinor={constraints.total_budget_minor} currency={constraints.currency ?? "CNY"} />
              {constraints.budget_includes_fees === false ? "（不含费用）" : "（含费用）"}
            </dd>
          </div>
          <div className="fp-goal__row">
            <dt className="fp-goal__label">应急金底线</dt>
            <dd>
              <MoneyText amountMinor={constraints.protected_floor_minor} currency={constraints.currency ?? "CNY"} />
            </dd>
          </div>
          <div className="fp-goal__row">
            <dt className="fp-goal__label">保护账户组</dt>
            <dd>
              {constraints.protected_floor_scope && constraints.protected_floor_scope.length > 0
                ? constraints.protected_floor_scope.join("、")
                : "不适用（未指定保护组）"}
            </dd>
          </div>
          <div className="fp-goal__row">
            <dt className="fp-goal__label">交付截止</dt>
            <dd>{formatDateTime(constraints.delivery_deadline, "不适用（未指定截止时间）")}</dd>
          </div>
          <div className="fp-goal__row">
            <dt className="fp-goal__label">最大确认次数</dt>
            <dd>{typeof constraints.max_confirmations === "number" ? constraints.max_confirmations : "不适用"}</dd>
          </div>
          <div className="fp-goal__row">
            <dt className="fp-goal__label">币种</dt>
            <dd>{constraints.currency ?? "CNY"}</dd>
          </div>
        </dl>
        {unresolved.length > 0 ? (
          <p className="fp-goal__blocking" role="status" data-testid="goal-unresolved-blocking">
            未确定字段：{unresolved.join("、")} —— 未澄清前不会执行任何写操作。
          </p>
        ) : null}
      </section>

      <section className="fp-goal__section" aria-labelledby="goal-preferences">
        <h3 className="fp-goal__section-title" id="goal-preferences">
          偏好
        </h3>
        <p className="fp-goal__section-hint">可以权衡，不保证全部满足；没有明确优先级的不会自行加权。</p>
        {preferences.length === 0 ? (
          <p className="fp-goal__empty">无（本次没有表达偏好）</p>
        ) : (
          <ul className="fp-goal__list" data-testid="goal-preferences-list">
            {preferences.map((item, index) => (
              <li className="fp-goal__row" key={`${item.higher}-${item.lower}-${index}`}>
                <span className="fp-goal__label">优先</span>
                <span>{item.higher}</span>
                <span className="fp-goal__label">优于</span>
                <span>{item.lower}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="fp-goal__section" aria-labelledby="goal-facts">
        <h3 className="fp-goal__section-title" id="goal-facts">
          事实与来源
        </h3>
        <p className="fp-goal__section-hint">与用户表述分开：这里是可以核验的数据来源，以及被显式标记的推断项。</p>
        <ul className="fp-goal__list" data-testid="goal-facts-list">
          {evidence.length === 0 && inferred.length === 0 ? (
            <li className="fp-goal__empty">无（服务端未提供来源证据）</li>
          ) : null}
          {evidence.map((item) => (
            <li className="fp-goal__row" key={`evidence-${item}`}>
              <span className="fp-goal__source">来源证据</span>
              <span>{item}</span>
            </li>
          ))}
          {inferred.map((item) => (
            <li className="fp-goal__row" key={`inferred-${item}`}>
              <span className="fp-goal__inferred">推断（非用户明示）</span>
              <span>{item}</span>
            </li>
          ))}
          {goal.funding_sources && goal.funding_sources.length > 0 ? (
            <li className="fp-goal__row">
              <span className="fp-goal__source">资金来源</span>
              <span>{goal.funding_sources.join("、")}</span>
            </li>
          ) : null}
          {goal.unconfirmed_receivables_are_spendable === false ? (
            <li className="fp-goal__row">
              <span className="fp-goal__source">硬约束</span>
              <span>待收／待退资金不计入可用资金（不会提前花掉未到账的钱）。</span>
            </li>
          ) : null}
        </ul>
      </section>

      <section className="fp-goal__section fp-goal__section--clarify" aria-labelledby="goal-unresolved">
        <h3 className="fp-goal__section-title" id="goal-unresolved">
          待澄清（{clarifications.length}）
        </h3>
        {clarifications.length === 0 ? (
          <p className="fp-goal__empty">无：关键字段已确定。</p>
        ) : (
          <ul className="fp-goal__list" data-testid="goal-clarifications">
            {clarifications.map((item) => (
              <li key={item.clarification_id} className="fp-goal__clarify-item">
                <p className="fp-goal__clarify-question">
                  {item.blocking ? "（阻塞执行）" : "（可稍后确认）"} {item.prompt}
                </p>
                <p className="fp-goal__note">
                  字段：{item.field} · 状态：{item.status}
                  {item.reason_code ? ` · 原因码：${item.reason_code}` : ""}
                </p>
                {item.options && item.options.length > 0 ? (
                  <div className="fp-goal__options" role="group" aria-label={item.prompt}>
                    {item.options.map((option) => (
                      <button
                        key={option.option_id}
                        type="button"
                        className="fp-button fp-button--small"
                        onClick={() => onAnswer?.(item, option.option_id)}
                        disabled={!onAnswer || item.status !== "open"}
                      >
                        {option.label}
                      </button>
                    ))}
                  </div>
                ) : (
                  <p className="fp-goal__note">请直接补充说明。</p>
                )}
              </li>
            ))}
          </ul>
        )}
        {blockingClarifications.length > 0 ? (
          <p className="fp-goal__blocking">
            有 {blockingClarifications.length} 项阻塞澄清未完成：澄清前不会进行任何写操作。
          </p>
        ) : null}
      </section>
    </section>
  );
}