/**
 * 展示层适配器：把契约对象翻译成界面需要的展示口径。
 *
 * 原则：
 * - 只做**如实翻译**，不做业务判定（等级、金额、可行性由服务端决定）；
 * - 任何"成功"口径必须有权威依据（effect_confirmed / receipt.state === "applied"）；
 *   缺少依据时只能显示"受理/等待结果/未知"，绝不能显示成功；
 * - 未登记的值一律显示原值，便于与后端对齐（禁止猜测）。
 */

import type {
  AuthChallenge,
  DomainResult,
  Mandate,
  OperationView,
  Receipt,
  ReceiptOutcome,
  Subscription,
  TaskView,
} from "../types";

/** 列表响应兼容：网关可能返回数组，也可能包一层 {key: [...]}。 */
export function asList<T>(data: unknown, key: string): T[] {
  if (Array.isArray(data)) return data as T[];
  if (data && typeof data === "object") {
    const value = (data as Record<string, unknown>)[key];
    if (Array.isArray(value)) return value as T[];
  }
  return [];
}

/**
 * 由权威 OperationView（+ 可选 Receipt）推导用户可见的回执口径（A03）。
 *
 * 规则（SRS 7.1、7.2、A03）：
 * - succeeded 且 effect_confirmed === true → settled（已到账，有权威依据）
 * - succeeded 但没有 effect_confirmed → unknown（不得显示成功）
 * - dispatching → accepted（已提交，尚未确认受理）
 * - pending → accepted（已受理，等待结果）
 * - unknown → unknown（必须查原操作）
 * - failed → failed；created/authorized/awaiting_authorization → awaiting_confirmation
 * - cancelled → failed？不：取消是独立事实，这里归入 awaiting_confirmation 之外的 "failed" 会失真；
 *   因此返回 "cancelled" 由调用方展示"已取消"（下面 ReceiptOutcome 之外单独处理）
 */
export type DerivedOutcome = ReceiptOutcome | "cancelled";

export function receiptOutcomeOf(operation: OperationView | null | undefined, receipt?: Receipt | null): DerivedOutcome {
  if (!operation) return "unknown";
  const status = String(operation.status ?? "");
  const applied = receipt?.state === "applied" || receipt?.verified_status === "verified";

  switch (status) {
    case "succeeded":
      return operation.effect_confirmed === true || applied ? "settled" : "unknown";
    case "failed":
      return "failed";
    case "cancelled":
      return "cancelled";
    case "unknown":
      return "unknown";
    case "pending":
      return "accepted";
    case "dispatching":
      return "accepted";
    case "created":
    case "authorized":
    case "awaiting_authorization":
      return "awaiting_confirmation";
    default:
      return "unknown";
  }
}

/** 已安排（定时任务）与"已转账"必须分开：这里用 operation 的领域结果判断是否只是登记。 */
export function isScheduledOnly(operation: OperationView | null | undefined): boolean {
  if (!operation) return false;
  const domain = operation.domain_result as DomainResult | null | undefined;
  const resultType = String(domain?.result_type ?? "");
  return /schedul/i.test(resultType) || /schedul/i.test(String(operation.tool ?? ""));
}

/** 挑战剩余尝试次数：契约给的是 attempts（已用次数）+ max_attempts。 */
export function challengeAttemptsRemaining(challenge: AuthChallenge | null | undefined): number | null {
  if (!challenge) return null;
  const used = Number(challenge.attempts ?? 0);
  const max = Number(challenge.max_attempts ?? 5);
  if (!Number.isFinite(used) || !Number.isFinite(max)) return null;
  return Math.max(0, max - used);
}

export function challengeLockedUntil(challenge: AuthChallenge | null | undefined): string | null {
  return challenge?.locked_until ?? null;
}

/** 订阅关联的代扣授权：优先用独立对象，其次用内联字段。 */
export function mandateOf(subscription: Subscription): Mandate | null {
  if (subscription.mandate) return subscription.mandate;
  return null;
}

/** 订阅合同证据：推断出来的周期不得冒充合同（S5-01）。 */
export function subscriptionEvidenceLabel(subscription: Subscription): string {
  if (subscription.evidence === "contract" || subscription.is_contract_evidence === true) return "合同证据";
  if (subscription.evidence === "inferred") {
    const confidence = subscription.inference_confidence_bps;
    return typeof confidence === "number"
      ? `疑似周期（依据流水推断，置信度 ${(confidence / 100).toFixed(2)}%）`
      : "疑似周期（依据流水推断，非合同）";
  }
  return `证据类型未登记（${String(subscription.evidence)}）`;
}

/**
 * 任务视图中的结果摘要：网关可能内联完整 result_view.json，也可能只给摘要对象。
 * 这里做保守读取，缺少的字段按"未提供"处理（不编造）。
 */
export interface ResultSummaryNormalized {
  completed: Array<{ label: string; operationId?: string | null; amountMinor?: number | null }>;
  pending: string[];
  unknown: string[];
  failed: string[];
  nextActions: Array<{ action_id: string; label: string; kind?: string }>;
  totalSpentMinor: number | null;
  totalFeeMinor: number | null;
  generatedAt: string | null;
}

export function normalizeResultSummary(value: TaskView["result_summary"]): ResultSummaryNormalized | null {
  if (!value || typeof value !== "object") return null;
  const record = value as Record<string, unknown>;
  const completedRaw = Array.isArray(record.completed_effects) ? record.completed_effects : [];
  const completed = completedRaw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => ({
      label: String(item.description ?? item.kind ?? "已核验效果"),
      operationId: (item.operation_id as string | null | undefined) ?? null,
      amountMinor:
        typeof item.amount_minor === "number" ? (item.amount_minor as number) : null,
    }));
  const strings = (key: string): string[] =>
    Array.isArray(record[key]) ? (record[key] as unknown[]).map((item) => String(item)) : [];
  const nextActionsRaw = Array.isArray(record.next_actions) ? record.next_actions : [];
  const nextActions = nextActionsRaw
    .filter((item): item is Record<string, unknown> => !!item && typeof item === "object")
    .map((item) => ({
      action_id: String(item.action_id ?? ""),
      label: String(item.label ?? ""),
      kind: item.kind ? String(item.kind) : undefined,
    }));

  const anyContent =
    completed.length > 0 ||
    strings("pending_effects").length > 0 ||
    strings("unknown_effects").length > 0 ||
    strings("failed_effects").length > 0 ||
    nextActions.length > 0;
  if (!anyContent && typeof record.total_spent_minor !== "number") return null;

  return {
    completed,
    pending: strings("pending_effects"),
    unknown: strings("unknown_effects"),
    failed: strings("failed_effects"),
    nextActions,
    totalSpentMinor: typeof record.total_spent_minor === "number" ? (record.total_spent_minor as number) : null,
    totalFeeMinor: typeof record.total_fee_minor === "number" ? (record.total_fee_minor as number) : null,
    generatedAt: typeof record.generated_at === "string" ? (record.generated_at as string) : null,
  };
}