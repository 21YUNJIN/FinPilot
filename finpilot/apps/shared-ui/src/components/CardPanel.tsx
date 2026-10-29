import type { ReactElement } from "react";
import type { Card, CardControl } from "../types";
import {
  CARD_APPLICATION_LABELS,
  CARD_CONTROL_ROLE_LABELS,
  CARD_CONTROL_TYPE_LABELS,
  CARD_LIFECYCLE_LABELS,
  cardControlReleasableBy,
} from "../contract/labels";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, toneForStatus } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 卡片面板（A04）：卡生命周期与申请状态**分开展示**，叠加卡控逐条列出。
 *
 * 规则：
 * - 生命周期（inactive/active/lost/closed）与申请状态是两个对象，不能混成一个标签；
 * - 卡控（card_control.json）显示类型、来源角色、限额、渠道与是否仍生效；
 *   风控冻结只能由风控解除，客户与 Agent 均无权——界面必须写明；
 * - 界面绝不展示完整卡号/CVV/有效期（契约禁止）；只显示 masked_pan；
 * - 挂失/补卡/解挂为 RED（需强验证），额度与卡控为 YELLOW。
 */
export interface CardPanelProps {
  cards: Card[];
  title?: string;
  emptyHint?: string;
  busyCardId?: string | null;
  onApply?: () => void;
  onSetLimit?: (card: Card) => void;
  onReportLost?: (card: Card) => void;
  onReplace?: (card: Card) => void;
  onUnfreeze?: (card: Card) => void;
  onSetControl?: (card: Card) => void;
  className?: string;
  "data-testid"?: string;
}

function limitText(control: CardControl): ReactElement {
  if (control.control_type === "user_limit" || control.control_type === "risk_limit") {
    return <MoneyText amountMinor={control.limit_minor ?? null} fallback="不适用（未设置限额）" />;
  }
  if (control.control_type === "channel_lock") {
    return <span>渠道：{control.channel ?? "不适用（未指定渠道）"}</span>;
  }
  return <span>不适用</span>;
}

function releaseText(role: string | null | undefined): string {
  const releasable = cardControlReleasableBy(role);
  switch (role) {
    case "customer":
      return `可解除角色：${releasable.join("、")}（你自己）`;
    case "operator":
      return "仅运营角色可解除（客户无权）";
    case "risk":
      return "仅风控角色可解除（客户与 Agent 均无权）";
    case "system":
      return "系统设置，不可由用户直接解除";
    default:
      return "解除权限未登记，请联系客服确认";
  }
}

export function CardPanel({
  cards,
  title = "我的卡片",
  emptyHint = "当前没有卡片记录。",
  busyCardId,
  onApply,
  onSetLimit,
  onReportLost,
  onReplace,
  onUnfreeze,
  onSetControl,
  className,
  "data-testid": dataTestId = "card-panel",
}: CardPanelProps): ReactElement {
  return (
    <section className={cx("fp-card", "fp-cards", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
        {onApply ? (
          <button type="button" className="fp-button fp-button--small" onClick={onApply}>
            申请新卡（黄色等级）
          </button>
        ) : null}
      </header>

      {cards.length === 0 ? (
        <p className="fp-cards__empty">{emptyHint}</p>
      ) : (
        <ul className="fp-cards__list">
          {cards.map((card) => {
            const busy = busyCardId === card.card_id;
            const lifecycleKey = String(card.lifecycle_status ?? "");
            const applicationKey = card.application_status ? String(card.application_status) : null;
            const activeControls = (card.controls ?? []).filter((control) => control.active !== false);
            return (
              <li className="fp-cards__item" key={card.card_id} data-card-id={card.card_id}>
                <div className="fp-cards__item-head">
                  <span className="fp-cards__product">
                    {card.brand ? `${card.brand} ` : ""}
                    {card.masked_pan ?? "卡号未提供"}
                  </span>
                  <span className="fp-cards__number">账户：{card.account_id}</span>
                </div>

                <div className="fp-cards__status-row">
                  <span className="fp-cards__status-label">卡状态：</span>
                  <StatusChip
                    label={CARD_LIFECYCLE_LABELS[lifecycleKey] ?? `未知状态（${lifecycleKey || "未提供"}）`}
                    tone={toneForStatus(lifecycleKey)}
                    rawValue={lifecycleKey}
                    data-testid={`card-lifecycle-${card.card_id}`}
                  />
                  <span className="fp-cards__status-label">申请状态：</span>
                  {applicationKey ? (
                    <StatusChip
                      label={CARD_APPLICATION_LABELS[applicationKey] ?? `未知状态（${applicationKey}）`}
                      tone={toneForStatus(applicationKey)}
                      rawValue={applicationKey}
                      data-testid={`card-application-${card.card_id}`}
                    />
                  ) : (
                    <span className="fp-cards__na">不适用（任务视图中未返回申请对象）</span>
                  )}
                </div>

                <dl className="fp-cards__limits">
                  <div>
                    <dt>信用额度</dt>
                    <dd>
                      <MoneyText amountMinor={card.credit_limit_minor} fallback="不适用" />
                    </dd>
                  </div>
                  <div>
                    <dt>版本</dt>
                    <dd>v{card.version}</dd>
                  </div>
                  {card.replaced_by_card_id || card.replaces_card_id ? (
                    <div>
                      <dt>换卡关系</dt>
                      <dd>
                        {card.replaces_card_id ? `替代 ${card.replaces_card_id}` : ""}
                        {card.replaced_by_card_id ? ` 由 ${card.replaced_by_card_id} 替代` : ""}
                      </dd>
                    </div>
                  ) : null}
                </dl>

                {activeControls.length > 0 ? (
                  <div className="fp-cards__controls">
                    <h3 className="fp-cards__subtitle">生效中的叠加限制（{activeControls.length}）</h3>
                    <ul data-testid={`card-controls-${card.card_id}`}>
                      {activeControls.map((control) => {
                        const releasable = cardControlReleasableBy(control.created_by_role);
                        return (
                          <li key={control.control_id}>
                            <span className="fp-cards__control-name">
                              {CARD_CONTROL_TYPE_LABELS[String(control.control_type)] ??
                                `未登记限制类型（${String(control.control_type)}）`}
                            </span>
                            <span className="fp-cards__control-value">{limitText(control)}</span>
                            <span className="fp-cards__control-source">
                              来源：{CARD_CONTROL_ROLE_LABELS[String(control.created_by_role)] ?? String(control.created_by_role)}
                              {control.created_by ? `（${control.created_by}）` : ""}
                            </span>
                            <span className="fp-cards__control-release">{releaseText(control.created_by_role)}</span>
                            {control.reason ? <span className="fp-cards__control-note">原因：{control.reason}</span> : null}
                            <span className="fp-cards__control-note">创建于 {formatDateTime(control.created_at)}</span>
                          </li>
                        );
                      })}
                    </ul>
                    <p className="fp-cards__control-note">
                      解除用户自设锁定不会同时解除风控冻结；补卡后旧卡按条款处理（挂失卡不会自动恢复）。
                    </p>
                  </div>
                ) : (
                  <p className="fp-cards__na">叠加限制：不适用（服务端未返回生效中的卡控）。</p>
                )}

                <div className="fp-cards__actions">
                  {onSetLimit ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onSetLimit(card)} disabled={busy}>
                      调整额度（黄色等级）
                    </button>
                  ) : null}
                  {onSetControl ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onSetControl(card)} disabled={busy}>
                      设置卡控（黄色等级）
                    </button>
                  ) : null}
                  {onUnfreeze ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onUnfreeze(card)} disabled={busy}>
                      解挂（红色等级：需强验证）
                    </button>
                  ) : null}
                  {onReportLost ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onReportLost(card)} disabled={busy}>
                      挂失（红色等级：需强验证）
                    </button>
                  ) : null}
                  {onReplace ? (
                    <button type="button" className="fp-button fp-button--small" onClick={() => onReplace(card)} disabled={busy}>
                      补卡（红色等级：需强验证）
                    </button>
                  ) : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
      <p className="fp-cards__footer">
        完整卡号、CVV 与有效期不会出现在接口、日志或页面中；本页只展示脱敏卡号。
      </p>
    </section>
  );
}