import type { ReactElement } from "react";
import type { OperationView, Receipt } from "../types";
import { isScheduledOnly, receiptOutcomeOf, type DerivedOutcome } from "../api/adapters";
import { RECEIPT_OUTCOME_LABELS, RECEIPT_STATE_LABELS, RECEIPT_VERIFIED_LABELS } from "../contract/labels";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, type StatusTone } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 操作回执（A03）：由权威 OperationView（+ 可选 Receipt）推导用户可见口径，
 * 区分已安排 / 已受理 / 待确认 / 已到账 / 部分成功 / 失败 / 未知。
 *
 * 硬规则：
 * - 只有 `status=succeeded` 且 `effect_confirmed=true`（或有已验签回执）才显示"已到账"；
 *   若服务端状态是 succeeded 但没有权威依据，界面显示"状态不一致"并降级为未知；
 * - "已受理"不等于"已到账"；"已安排"不等于"已转账"——文案写清楚；
 * - 未知必须给出"查询原订单"的说明与入口，不得展示为失败或完成；
 * - 部分成功必须分别显示成功金额与未完成金额。
 */
export interface OperationReceiptProps {
  operation: OperationView;
  receipt?: Receipt | null;
  /** 部分成功拆分（展示补充：由领域结果给出）。 */
  partial?: {
    succeeded_minor?: number | null;
    failed_minor?: number | null;
    pending_minor?: number | null;
  } | null;
  remainingItems?: string[];
  querying?: boolean;
  queryHint?: string;
  onQueryOriginalOrder?: (operationId: string) => void;
  className?: string;
  "data-testid"?: string;
}

const OUTCOME_TONES: Readonly<Record<DerivedOutcome, StatusTone>> = {
  scheduled: "pending",
  accepted: "pending",
  awaiting_confirmation: "pending",
  settled: "success",
  partially_succeeded: "warning",
  failed: "danger",
  unknown: "warning",
  cancelled: "neutral",
};

const OUTCOME_LABELS: Readonly<Record<DerivedOutcome, string>> = {
  ...(RECEIPT_OUTCOME_LABELS as Record<string, string>),
  cancelled: "已取消",
} as Readonly<Record<DerivedOutcome, string>>;

const OUTCOME_EXPLANATIONS: Readonly<Record<DerivedOutcome, string>> = {
  scheduled: "已安排：定时任务已登记，尚未执行，当前没有发生转账。到点后会重新分级并按当日额度判定。",
  accepted: "已受理：请求已提交给业务提供方，但尚未确认最终结果；受理不等于到账。",
  awaiting_confirmation: "待确认：尚未提交，等待你的确认或强验证，当前没有资金效果。",
  settled: "已到账：以权威回执与账本记录为准，可在流水中核对。",
  partially_succeeded: "部分成功：只有部分金额完成，未完成部分如下，不能当作全部完成。",
  failed: "失败：以提供方可验证的拒绝或未提交为据，未产生资金效果；如与实情不符请查询原订单。",
  unknown: "结果未知：传输或外部确认失败，尚未确认成功也未确认失败；系统用原操作编号持续对账。",
  cancelled: "已取消：取消操作已确认生效，不会继续提交；已完成的外部效果不因此消失。",
};

export function OperationReceipt({
  operation,
  receipt,
  partial,
  remainingItems,
  querying = false,
  queryHint,
  onQueryOriginalOrder,
  className,
  "data-testid": dataTestId = "operation-receipt",
}: OperationReceiptProps): ReactElement {
  const derived = receiptOutcomeOf(operation, receipt);
  const scheduled = isScheduledOnly(operation) && derived === "accepted";
  const outcome: DerivedOutcome = scheduled ? "scheduled" : derived;
  const outcomeLabel = OUTCOME_LABELS[outcome] ?? `未登记的回执口径（${String(outcome)}）`;
  const unknown = outcome === "unknown";
  const currency = operation.currency ?? "CNY";

  // 防御性一致性检查：succeeded 但无权威依据时不得展示为已到账。
  const inconsistentSuccess = operation.status === "succeeded" && operation.effect_confirmed !== true && receipt?.state !== "applied";

  return (
    <section
      className={cx("fp-card", "fp-receipt", `fp-receipt--${outcome}`, className)}
      aria-label="操作回执"
      data-testid={dataTestId}
      data-outcome={outcome}
    >
      <header className="fp-card__header">
        <h2 className="fp-card__title">操作回执</h2>
        <StatusChip
          label={outcomeLabel}
          tone={OUTCOME_TONES[outcome] ?? "info"}
          srPrefix="回执结果"
          rawValue={String(operation.status)}
          data-testid="receipt-outcome-chip"
        />
        <StatusChip
          label={`操作状态：${operation.status}`}
          tone="info"
          srPrefix="操作状态"
          rawValue={String(operation.status)}
          data-testid="receipt-operation-status-chip"
        />
      </header>

      <p className="fp-receipt__explanation" data-testid="receipt-explanation">
        {OUTCOME_EXPLANATIONS[outcome] ?? "结果口径未登记：请以服务端回执与服务端查询为准。"}
      </p>

      {inconsistentSuccess ? (
        <div className="fp-receipt__inconsistent" role="alert" data-testid="receipt-inconsistent-success">
          <p>
            状态不一致：服务端返回 succeeded，但没有权威回执或 effect_confirmed=true 依据。
            界面按"结果未知"处理，请查询原订单确认。
          </p>
        </div>
      ) : null}

      <dl className="fp-receipt__amounts">
        <div>
          <dt>金额</dt>
          <dd>
            <MoneyText
              amountMinor={operation.amount_minor}
              currency={currency}
              fallback="不适用（尚未确定）"
              data-testid="receipt-actual-amount"
            />
          </dd>
        </div>
        <div>
          <dt>费用</dt>
          <dd>
            <MoneyText amountMinor={operation.fee_minor} currency={currency} fallback="不适用（尚未确定）" />
          </dd>
        </div>
        <div>
          <dt>提交时间</dt>
          <dd>{formatDateTime(operation.submitted_at, "不适用（尚未提交）")}</dd>
        </div>
        <div>
          <dt>到账时间</dt>
          <dd>{formatDateTime(operation.settled_at, "不适用（尚未到账）")}</dd>
        </div>
        <div>
          <dt>操作编号</dt>
          <dd>
            <code data-testid="receipt-operation-id">{operation.operation_id}</code>
          </dd>
        </div>
        {operation.idempotency_key ? (
          <div>
            <dt>幂等键</dt>
            <dd>
              <code>{operation.idempotency_key}</code>
            </dd>
          </div>
        ) : null}
        {operation.parameters_hash ? (
          <div>
            <dt>参数摘要</dt>
            <dd>
              <code>{operation.parameters_hash}</code>
            </dd>
          </div>
        ) : null}
        {operation.provider_order_ref ? (
          <div>
            <dt>提供方流水号</dt>
            <dd>
              <code>{operation.provider_order_ref}</code>
            </dd>
          </div>
        ) : null}
        {operation.error_code ? (
          <div>
            <dt>错误码</dt>
            <dd>
              <code>{operation.error_code}</code>
            </dd>
          </div>
        ) : null}
      </dl>

      {receipt ? (
        <div className="fp-receipt__source" data-testid="receipt-source">
          <p>
            权威回执：{RECEIPT_STATE_LABELS[String(receipt.state)] ?? `未登记状态（${String(receipt.state)}）`} ·
            {RECEIPT_VERIFIED_LABELS[String(receipt.verified_status)] ?? String(receipt.verified_status)} ·
            来源 {String(receipt.source)} · 事件版本 {receipt.event_version} · 接收于 {formatDateTime(receipt.received_at)}
          </p>
          {receipt.conflict_reason ? <p>冲突原因：{receipt.conflict_reason}</p> : null}
        </div>
      ) : null}

      {outcome === "partially_succeeded" || partial ? (
        <div className="fp-receipt__partial" data-testid="receipt-partial">
          <h3 className="fp-receipt__subtitle">部分成功明细</h3>
          <ul>
            <li>
              已完成金额：<MoneyText amountMinor={partial?.succeeded_minor ?? null} currency={currency} fallback="不适用" />
            </li>
            <li>
              未完成金额：<MoneyText amountMinor={partial?.failed_minor ?? null} currency={currency} fallback="不适用" />
            </li>
            <li>
              仍在处理金额：<MoneyText amountMinor={partial?.pending_minor ?? null} currency={currency} fallback="不适用" />
            </li>
          </ul>
        </div>
      ) : null}

      {remainingItems && remainingItems.length > 0 ? (
        <div className="fp-receipt__remaining" data-testid="receipt-remaining">
          <h3 className="fp-receipt__subtitle">未完成项</h3>
          <ul>
            {remainingItems.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {unknown ? (
        <div className="fp-receipt__unknown" role="status" aria-live="polite" data-testid="receipt-unknown-notice">
          <p>
            未知状态处理说明：请使用「原操作编号」查询原订单（系统会按 1、2、5、10、30 秒的节奏持续对账，
            60 秒仍未确认时会展示等待说明，15 分钟仍未确认会升级人工队列）。在结果确认前不要重复提交，
            重复提交可能造成两笔业务。
          </p>
          {operation.unknown_since ? <p>进入未知状态时间：{formatDateTime(operation.unknown_since)}</p> : null}
        </div>
      ) : null}

      {operation.pending_actions && operation.pending_actions.length > 0 ? (
        <p className="fp-receipt__next">待处理事项：{operation.pending_actions.join("；")}</p>
      ) : null}
      {queryHint ? <p className="fp-receipt__hint">{queryHint}</p> : null}

      <div className="fp-receipt__actions">
        {onQueryOriginalOrder ? (
          <button
            type="button"
            className="fp-button fp-button--primary"
            onClick={() => onQueryOriginalOrder(operation.operation_id)}
            disabled={querying}
            aria-busy={querying}
            data-testid="receipt-query-button"
          >
            {querying ? "正在查询原订单…" : "查询原订单（使用原操作编号）"}
          </button>
        ) : null}
      </div>
    </section>
  );
}