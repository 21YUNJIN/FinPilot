import type { ReactElement } from "react";
import type { Mandate, Subscription } from "../types";
import { MANDATE_STATUS_LABELS, SUBSCRIPTION_PERIOD_LABELS, SUBSCRIPTION_STATUS_LABELS } from "../contract/labels";
import { subscriptionEvidenceLabel } from "../api/adapters";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, toneForStatus } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 订阅与代扣列表（S5 / A05）。
 *
 * 规则：
 * - **合同状态**（subscription.json）与**代扣授权状态**（mandate.json）分别展示：
 *   解除代扣不等于取消合同，反之亦然；
 * - 只成功一半时必须提示"部分成功"，并指出缺失的那一步（撤销代扣 / 取消合同）；
 * - `evidence=inferred` 的疑似周期不得冒充合同，必须标注为推算；
 * - 金额、下次扣款时间、取消时间、代扣撤销时间如实展示；缺失显示"不适用"。
 */
export interface SubscriptionListProps {
  subscriptions: Subscription[];
  mandates?: Mandate[];
  title?: string;
  busyId?: string | null;
  emptyHint?: string;
  onCancelContract?: (subscription: Subscription) => void;
  onRevokeMandate?: (subscription: Subscription) => void;
  className?: string;
  "data-testid"?: string;
}

function mandateFor(subscription: Subscription, mandates: Mandate[]): Mandate | null {
  if (subscription.mandate) return subscription.mandate;
  return (
    mandates.find((mandate) => mandate.mandate_id && mandate.mandate_id === subscription.mandate_id) ??
    mandates.find((mandate) => mandate.subscription_id && mandate.subscription_id === subscription.subscription_id) ??
    null
  );
}

function partialNotice(subscription: Subscription, mandate: Mandate | null): string | null {
  const contract = String(subscription.status ?? "");
  const mandateStatus = mandate ? String(mandate.status ?? "") : null;
  const contractEnded = contract === "cancelled" || contract === "ended";
  const mandateEnded = mandateStatus === "revoked";
  if (contractEnded && mandate && !mandateEnded) {
    return "部分成功：合同已取消/结束，但代扣授权仍然有效，商户仍可能发起扣款。请单独撤销代扣授权。";
  }
  if (mandateEnded && !contractEnded) {
    return "部分成功：代扣授权已撤销，但合同仍生效（可能产生欠费或违约条款）。请单独取消合同。";
  }
  if (!mandate && subscription.mandate_id) {
    return "部分成功待核对：合同记录引用了代扣授权，但当前未取到授权状态，请刷新后再确认。";
  }
  return null;
}

export function SubscriptionList({
  subscriptions,
  mandates = [],
  title = "订阅与代扣",
  busyId,
  emptyHint = "没有订阅记录。",
  onCancelContract,
  onRevokeMandate,
  className,
  "data-testid": dataTestId = "subscription-list",
}: SubscriptionListProps): ReactElement {
  return (
    <section className={cx("fp-card", "fp-subs", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
      </header>

      {subscriptions.length === 0 ? (
        <p className="fp-subs__empty">{emptyHint}</p>
      ) : (
        <ul className="fp-subs__list">
          {subscriptions.map((subscription) => {
            const busy = busyId === subscription.subscription_id;
            const mandate = mandateFor(subscription, mandates);
            const notice = partialNotice(subscription, mandate);
            const contractKey = String(subscription.status ?? "");
            const mandateKey = mandate ? String(mandate.status ?? "") : null;
            return (
              <li className="fp-subs__item" key={subscription.subscription_id} data-subscription-id={subscription.subscription_id}>
                <div className="fp-subs__head">
                  <span className="fp-subs__merchant">{subscription.merchant_name || "商户名称未提供"}</span>
                  <span className="fp-subs__amount">
                    <MoneyText amountMinor={subscription.amount_minor} />
                    {subscription.period
                      ? ` / ${SUBSCRIPTION_PERIOD_LABELS[String(subscription.period)] ?? String(subscription.period)}`
                      : ""}
                  </span>
                </div>

                <div className="fp-subs__statuses">
                  <div className="fp-subs__status-block" data-testid={`subscription-contract-${subscription.subscription_id}`}>
                    <span className="fp-subs__status-title">合同状态</span>
                    <StatusChip
                      label={SUBSCRIPTION_STATUS_LABELS[contractKey] ?? `未知状态（${contractKey || "未提供"}）`}
                      tone={toneForStatus(contractKey)}
                      rawValue={contractKey}
                      srPrefix="合同状态"
                    />
                  </div>
                  <div className="fp-subs__status-block" data-testid={`subscription-mandate-${subscription.subscription_id}`}>
                    <span className="fp-subs__status-title">代扣授权状态</span>
                    {mandateKey ? (
                      <StatusChip
                        label={MANDATE_STATUS_LABELS[mandateKey] ?? `未知状态（${mandateKey}）`}
                        tone={toneForStatus(mandateKey)}
                        rawValue={mandateKey}
                        srPrefix="代扣授权状态"
                      />
                    ) : (
                      <span className="fp-subs__na">不适用（该合同没有关联的代扣授权）</span>
                    )}
                  </div>
                </div>

                <p className="fp-subs__evidence" data-testid={`subscription-evidence-${subscription.subscription_id}`}>
                  周期依据：{subscriptionEvidenceLabel(subscription)}
                  {subscription.terms_version ? ` · 条款版本 ${subscription.terms_version}` : ""}
                </p>

                <dl className="fp-subs__meta">
                  <div>
                    <dt>下次扣款</dt>
                    <dd>{formatDateTime(subscription.next_charge_at, "不适用")}</dd>
                  </div>
                  <div>
                    <dt>最近扣款</dt>
                    <dd>{formatDateTime(subscription.last_charged_at, "不适用")}</dd>
                  </div>
                  <div>
                    <dt>合同取消时间</dt>
                    <dd>{formatDateTime(subscription.cancelled_at, "不适用")}</dd>
                  </div>
                  <div>
                    <dt>代扣撤销时间</dt>
                    <dd>{formatDateTime(mandate?.revoked_at ?? null, "不适用")}</dd>
                  </div>
                </dl>

                {subscription.cancel_terms ? <p className="fp-subs__note">取消条款：{subscription.cancel_terms}</p> : null}
                {notice ? (
                  <p className="fp-subs__partial" role="status" data-testid={`subscription-partial-${subscription.subscription_id}`}>
                    {notice}
                  </p>
                ) : null}

                <div className="fp-subs__actions">
                  {onCancelContract ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onCancelContract(subscription)} disabled={busy}>
                      取消合同（黄色等级：需确认）
                    </button>
                  ) : null}
                  {onRevokeMandate ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onRevokeMandate(subscription)} disabled={busy}>
                      撤销代扣授权（红色等级：需强验证）
                    </button>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <p className="fp-subs__footer">
        订阅查询属于官方黄色例外（查询也要确认）；取消合同与撤销代扣是两个独立动作，
        未确认前不显示为已生效。
      </p>
    </section>
  );
}