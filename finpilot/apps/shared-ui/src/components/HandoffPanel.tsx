import type { ReactElement } from "react";
import type { HandoffCase, HandoffItem } from "../types";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 人工接管面板（A05 / S6-05）。
 *
 * 规则（A05 验收）：
 * - 展示操作 ID、已花金额、未知结果、未做事项，以及当前授权清单；
 * - 已完成 / 结果未知 / 未提交 三类必须分开，未知项不得写成完成；
 * - 明确写"操作员不能代替用户同意"，运营审批是独立角色；
 * - 未开案时提供"申请人工接管"入口，并说明不会因此改变资金结果。
 */
export interface HandoffPanelProps {
  handoff?: HandoffCase | null;
  loading?: boolean;
  busy?: boolean;
  onRequestHandoff?: () => void;
  onRefresh?: () => void;
  contactHint?: string;
  className?: string;
  "data-testid"?: string;
}

function ItemList({
  items,
  emptyText,
  sectionId,
}: {
  items: HandoffItem[];
  emptyText: string;
  sectionId: string;
}): ReactElement {
  if (items.length === 0) return <p className="fp-handoff__empty">{emptyText}</p>;
  return (
    <ul className="fp-handoff__list" data-testid={sectionId}>
      {items.map((item, index) => (
        <li key={item.operation_id ?? `${item.label}-${index}`}>
          <span className="fp-handoff__item-label">{item.label}</span>
          {typeof item.amount_minor === "number" ? (
            <span className="fp-handoff__item-amount">
              <MoneyText amountMinor={item.amount_minor} />
            </span>
          ) : null}
          {item.operation_id ? (
            <span className="fp-handoff__item-op">
              操作编号：<code>{item.operation_id}</code>
            </span>
          ) : (
            <span className="fp-handoff__item-op fp-na">操作编号：不适用（尚未提交）</span>
          )}
          {item.detail ? <span className="fp-handoff__item-detail">{item.detail}</span> : null}
          {item.reason ? <span className="fp-handoff__item-detail">原因：{item.reason}</span> : null}
        </li>
      ))}
    </ul>
  );
}

export function HandoffPanel({
  handoff,
  loading = false,
  busy = false,
  onRequestHandoff,
  onRefresh,
  contactHint,
  className,
  "data-testid": dataTestId = "handoff-panel",
}: HandoffPanelProps): ReactElement {
  if (!handoff) {
    return (
      <section className={cx("fp-card", "fp-handoff", className)} aria-label="人工接管" data-testid={dataTestId}>
        <header className="fp-card__header">
          <h2 className="fp-card__title">人工接管</h2>
        </header>
        <p className="fp-handoff__empty" aria-busy={loading}>
          {loading ? "正在查询接管状态…" : "当前未开立人工接管案件。"}
        </p>
        <p className="fp-handoff__rule">
          申请接管不会改变资金结果，也不会替你同意任何高风险操作；未提交的动作仍可在任务页停止。
        </p>
        <div className="fp-handoff__actions">
          {onRequestHandoff ? (
            <button type="button" className="fp-button fp-button--primary" onClick={onRequestHandoff} disabled={busy}>
              申请人工接管
            </button>
          ) : null}
          {contactHint ? <span className="fp-handoff__contact">{contactHint}</span> : null}
        </div>
      </section>
    );
  }

  return (
    <section className={cx("fp-card", "fp-handoff", className)} aria-label="人工接管" data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">人工接管</h2>
        <StatusChip label={handoff.status} tone="pending" srPrefix="接管状态" rawValue={handoff.status} />
        {onRefresh ? (
          <button type="button" className="fp-button fp-button--small" onClick={onRefresh} disabled={busy}>
            刷新接管状态
          </button>
        ) : null}
      </header>

      <dl className="fp-handoff__meta">
        <div>
          <dt>案件编号</dt>
          <dd>
            <code>{handoff.case_id}</code>
          </dd>
        </div>
        <div>
          <dt>关联任务</dt>
          <dd>{handoff.task_id ? <code>{handoff.task_id}</code> : "不适用"}</dd>
        </div>
        <div>
          <dt>开案原因</dt>
          <dd>{handoff.reason || "未提供原因"}</dd>
        </div>
        <div>
          <dt>开案时间</dt>
          <dd>{formatDateTime(handoff.opened_at, "未提供")}</dd>
        </div>
      </dl>

      <section className="fp-handoff__section" aria-labelledby="handoff-done">
        <h3 id="handoff-done">已完成（{handoff.completed.length}）</h3>
        <ItemList items={handoff.completed} emptyText="没有已确认完成的事项。" sectionId="handoff-completed" />
      </section>

      <section className="fp-handoff__section" aria-labelledby="handoff-unknown">
        <h3 id="handoff-unknown">结果未知（{handoff.unknown.length}）</h3>
        <p className="fp-handoff__hint">
          未知项不得当作完成或失败：需要按原操作编号向提供方核对，核对前不会重复提交。
        </p>
        <ItemList items={handoff.unknown} emptyText="没有结果未知的事项。" sectionId="handoff-unknown" />
      </section>

      <section className="fp-handoff__section" aria-labelledby="handoff-not-submitted">
        <h3 id="handoff-not-submitted">未提交（{handoff.not_submitted.length}）</h3>
        <p className="fp-handoff__hint">这些步骤没有产生资金效果；接管后可按你的新指令重排。</p>
        <ItemList items={handoff.not_submitted} emptyText="没有未提交的事项。" sectionId="handoff-not-submitted" />
      </section>

      <section className="fp-handoff__section" aria-labelledby="handoff-funds">
        <h3 id="handoff-funds">资金</h3>
        <dl className="fp-handoff__funds" data-testid="handoff-funds">
          <div>
            <dt>已花金额</dt>
            <dd>
              <MoneyText amountMinor={handoff.funds.spent_minor ?? null} currency={handoff.funds.currency} fallback="不适用" />
            </dd>
          </div>
          <div>
            <dt>已产生费用</dt>
            <dd>
              <MoneyText amountMinor={handoff.funds.fees_minor ?? null} currency={handoff.funds.currency} fallback="不适用" />
            </dd>
          </div>
          <div>
            <dt>仍被预留</dt>
            <dd>
              <MoneyText amountMinor={handoff.funds.reserved_minor ?? null} currency={handoff.funds.currency} fallback="不适用" />
            </dd>
          </div>
          <div>
            <dt>结果未知金额</dt>
            <dd>
              <MoneyText amountMinor={handoff.funds.unknown_minor ?? null} currency={handoff.funds.currency} fallback="不适用" />
            </dd>
          </div>
          <div>
            <dt>待到账退款</dt>
            <dd>
              <MoneyText
                amountMinor={handoff.funds.refund_pending_minor ?? null}
                currency={handoff.funds.currency}
                fallback="不适用"
              />
            </dd>
          </div>
        </dl>
      </section>

      <section className="fp-handoff__section" aria-labelledby="handoff-authorizations">
        <h3 id="handoff-authorizations">授权清单（{handoff.authorizations.length}）</h3>
        {handoff.authorizations.length === 0 ? (
          <p className="fp-handoff__empty">没有仍有效的授权。</p>
        ) : (
          <ul className="fp-handoff__auths" data-testid="handoff-authorizations">
            {handoff.authorizations.map((auth, index) => (
              <li key={auth.grant_id ?? `${auth.plan_id}-${index}`}>
                <span>授权 {auth.grant_id ?? "编号未提供"}</span>
                <span>状态：{auth.status ?? "未标注"}</span>
                {auth.scope_hash ? (
                  <span>
                    范围摘要：<code>{auth.scope_hash}</code>
                  </span>
                ) : null}
                {auth.note ? <span>{auth.note}</span> : null}
              </li>
            ))}
          </ul>
        )}
      </section>

      {handoff.operator_actions && handoff.operator_actions.length > 0 ? (
        <section className="fp-handoff__section" aria-labelledby="handoff-operator">
          <h3 id="handoff-operator">运营操作记录</h3>
          <ul className="fp-handoff__ops" data-testid="handoff-operator-actions">
            {handoff.operator_actions.map((action, index) => (
              <li key={`${action.at}-${index}`}>
                {formatDateTime(action.at)} · {action.action}
                {action.by ? ` · 处理人 ${action.by}` : ""}
                {action.note ? ` · ${action.note}` : ""}
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      <p className="fp-handoff__rule">
        操作员不能代替用户同意任何高风险操作；运营审批是独立角色，不能由 Agent 或客户直接批准。
        接管期间已提交的操作仍按提供方实际结果处理。
      </p>
    </section>
  );
}