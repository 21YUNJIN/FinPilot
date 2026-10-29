/**
 * 状态 → 中文标签的唯一映射表。
 *
 * 来源与硬约束（V5 第 7.1、18.2 节；docs/INTERFACES.md 第 1 节）：
 * - 键名逐字对应 `services/api/src/finpilot/models.py` 顶部的状态常量元组，前端不得发明新状态。
 * - 标签只描述**已发生的事实**，不得把非成功状态写成"成功/完成"。
 * - 未登记的状态值一律显示 `未知状态（<原值>）`，并保留原值，禁止静默当成成功。
 * - 风险等级必须文字化（NFR-03：风险不得只用颜色表示）。
 */

import type { PlanStepDisplayStatus, TaskStatus } from "../types";

/** 任务终态（models.py TASK_TERMINAL）。 */
export const TASK_TERMINAL_STATUSES: readonly TaskStatus[] = [
  "succeeded",
  "partially_succeeded",
  "failed",
  "cancelled",
];

export function isTaskTerminal(status: string | null | undefined): boolean {
  return typeof status === "string" && (TASK_TERMINAL_STATUSES as readonly string[]).includes(status);
}

/** 运行模式（settings.app_mode）。缺失即"未标注"，绝不默认 live。 */
export const APP_MODE_LABELS: Readonly<Record<string, string>> = {
  live: "实时运行（live）",
  offline_replay: "离线重放（offline_replay，非实时）",
  mock: "模拟环境（mock，非真实资金）",
};

export const APP_MODE_SHORT_LABELS: Readonly<Record<string, string>> = {
  live: "live",
  offline_replay: "offline_replay",
  mock: "mock",
};

/** 任务状态（models.py TASK_STATUSES）。 */
export const TASK_STATUS_LABELS: Readonly<Record<string, string>> = {
  draft: "草稿",
  clarifying: "澄清中",
  planning: "规划中",
  awaiting_authorization: "等待授权",
  ready: "已就绪",
  running: "执行中",
  reconciling: "核对中",
  replanning: "重新规划",
  awaiting_human: "等待人工接管",
  succeeded: "已完成",
  partially_succeeded: "部分成功",
  failed: "失败",
  cancelled: "已取消",
  blocked: "已阻塞（条件未满足）",
};

/** 操作状态（models.py OPERATION_STATUSES）。 */
export const OPERATION_STATUS_LABELS: Readonly<Record<string, string>> = {
  created: "已创建（未提交）",
  awaiting_authorization: "等待授权",
  authorized: "已授权（未提交）",
  dispatching: "已提交（尚未确认受理）",
  pending: "已受理（等待结果）",
  unknown: "结果未知（需查询原订单）",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

/** 授权记录状态（models.py GRANT_STATUSES）。 */
export const GRANT_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "待激活",
  active: "有效",
  consumed: "已使用",
  revoked: "已撤销",
  expired: "已过期",
};

/** 权限等级。RED 必须走真实强验证，文案不得暗示"点击即成功"。 */
export const AUTH_LEVEL_LABELS: Readonly<Record<string, string>> = {
  GREEN: "绿色等级（服务端权限检查后自动执行）",
  YELLOW: "黄色等级（需要你明确确认）",
  RED: "红色等级（必须完成强验证后才会提交，未验证不得提交）",
};

export const AUTH_LEVEL_SHORT_LABELS: Readonly<Record<string, string>> = {
  GREEN: "绿色 GREEN",
  YELLOW: "黄色 YELLOW",
  RED: "红色 RED",
};

export const AUTH_LEVEL_ACTIONS: Readonly<Record<string, string>> = {
  GREEN: "无需你确认：后端完成身份与归属检查后执行。",
  YELLOW: "需要你确认：确认后按展示参数执行，参数变化会生成新的 scope_hash。",
  RED: "必须完成强验证（TOTP）：验证码走独立请求，不进入对话与日志。",
};

/** 计划步骤展示状态（SRS 18.2 要求逐条显示的真实状态）。 */
export const PLAN_STEP_STATUS_LABELS: Readonly<Record<PlanStepDisplayStatus, string>> = {
  not_started: "未开始（等待前置步骤）",
  awaiting_confirmation: "待确认",
  verifying: "验证",
  running: "执行中（尚未确认结果）",
  submitted: "已提交",
  awaiting_result: "等待结果",
  succeeded: "已完成",
  failed: "失败",
  stopped: "已停止",
  result_unknown: "结果未知（正在核对原订单）",
};

/**
 * 契约状态（plan_step.json：pending/running/awaiting_authorization/succeeded/failed/skipped/unknown/cancelled）
 * 与中间层可能出现的状态 → 展示状态。
 * 只做归并，不改变事实：任何"可能已产生外部效果"的状态都归入 submitted/awaiting_result/result_unknown，
 * 绝不能归入 succeeded。
 */
const PLAN_STEP_STATUS_ALIASES: Readonly<Record<string, PlanStepDisplayStatus>> = {
  // 契约：未开始
  pending: "not_started",
  queued: "not_started",
  planned: "not_started",
  // 契约：待确认 / 验证
  awaiting_authorization: "awaiting_confirmation",
  pending_confirmation: "awaiting_confirmation",
  awaiting_confirmation: "awaiting_confirmation",
  needs_confirmation: "awaiting_confirmation",
  pending_user_action: "awaiting_confirmation",
  verifying: "verifying",
  awaiting_verification: "verifying",
  challenge_required: "verifying",
  challenge_pending: "verifying",
  // 契约：执行中（本地执行、尚无外部结果）
  running: "running",
  in_progress: "running",
  // 已提交给外部提供方
  submitted: "submitted",
  dispatching: "submitted",
  dispatched: "submitted",
  accepted: "submitted",
  // 等待外部结果
  awaiting_result: "awaiting_result",
  reconciling: "awaiting_result",
  awaiting_provider: "awaiting_result",
  // 完成
  succeeded: "succeeded",
  completed: "succeeded",
  // 失败
  failed: "failed",
  error: "failed",
  rejected: "failed",
  // 停止
  stopped: "stopped",
  cancelled: "stopped",
  canceled: "stopped",
  skipped: "stopped",
  revoked: "stopped",
  // 未知
  unknown: "result_unknown",
  result_unknown: "result_unknown",
};

/** 把后端步骤状态归一到展示状态；未登记返回 null（调用方显示原值）。 */
export function normalizePlanStepStatus(raw: string | null | undefined): PlanStepDisplayStatus | null {
  if (!raw) return null;
  const key = String(raw).trim().toLowerCase();
  return PLAN_STEP_STATUS_ALIASES[key] ?? null;
}

/** 回执口径（A03：已安排/已受理/待确认/已到账/部分成功/失败/未知）。 */
export const RECEIPT_OUTCOME_LABELS: Readonly<Record<string, string>> = {
  scheduled: "已安排（尚未执行）",
  accepted: "已受理（等待结果）",
  awaiting_confirmation: "待确认",
  settled: "已到账",
  partially_succeeded: "部分成功",
  failed: "失败",
  unknown: "结果未知",
};

/** 卡片生命周期（models.py CARD_LIFECYCLE）。 */
export const CARD_LIFECYCLE_LABELS: Readonly<Record<string, string>> = {
  inactive: "未激活",
  active: "正常",
  lost: "已挂失",
  closed: "已销户",
};

/** 卡片申请状态（models.py CARD_APPLICATION_STATUS）。 */
export const CARD_APPLICATION_LABELS: Readonly<Record<string, string>> = {
  submitted: "申请已提交",
  under_review: "申请审核中",
  approved: "申请已批准",
  rejected: "申请被拒绝",
  withdrawn: "申请已撤回",
};

/** 订阅合同状态（models.py SUBSCRIPTION_STATUS）。 */
export const SUBSCRIPTION_STATUS_LABELS: Readonly<Record<string, string>> = {
  active: "合同生效中",
  cancelled: "合同已取消",
  pending_cancellation: "合同取消处理中",
  ended: "合同已结束",
};

/** 代扣授权状态（models.py MANDATE_STATUS）。 */
export const MANDATE_STATUS_LABELS: Readonly<Record<string, string>> = {
  active: "代扣授权有效",
  revoked: "代扣授权已撤销",
  pending_revocation: "代扣撤销处理中",
};

/** 理财订单状态（models.py WEALTH_ORDER_STATUS）。 */
export const WEALTH_ORDER_STATUS_LABELS: Readonly<Record<string, string>> = {
  submitted: "已提交",
  accepted: "已受理",
  awaiting_confirmation: "待确认",
  confirmed: "已确认",
  rejected: "已拒绝",
  redeeming: "赎回中",
  settled: "已结算",
  failed: "失败",
  cancelled: "已取消",
};

/** 定时任务状态（models.py JOB_STATES）。 */
export const JOB_STATE_LABELS: Readonly<Record<string, string>> = {
  scheduled: "已安排",
  leased: "已领取",
  running: "执行中",
  completed: "已完成",
  awaiting_authorization: "等待授权",
  missed: "已错过",
  failed: "失败",
  cancelled: "已取消",
};

/** 风险等级：必须文字化，不能只靠颜色。 */
export const RISK_LEVEL_LABELS: Readonly<Record<string, string>> = {
  R1: "低风险 R1",
  R2: "中低风险 R2",
  R3: "中风险 R3",
  R4: "中高风险 R4",
  R5: "高风险 R5",
};

export const RISK_LEVEL_BY_NAME: Readonly<Record<string, string>> = {
  低: "低风险 R1",
  中低: "中低风险 R2",
  中: "中风险 R3",
  中高: "中高风险 R4",
  高: "高风险 R5",
};

/** 卡控类型（card_control.json control_type）。 */
export const CARD_CONTROL_TYPE_LABELS: Readonly<Record<string, string>> = {
  user_lock: "用户自设锁定",
  channel_lock: "渠道锁定",
  user_limit: "用户自设限额",
  risk_freeze: "风控冻结",
  risk_limit: "风控限额",
};

/** 卡控创建者角色（card_control.json created_by_role）。 */
export const CARD_CONTROL_ROLE_LABELS: Readonly<Record<string, string>> = {
  customer: "客户",
  operator: "运营",
  risk: "风控",
  system: "系统",
};

/** 只有客户角色创建的限制可由客户解除；风控冻结只能由风控解除。 */
export function cardControlReleasableBy(role: string | null | undefined): string[] {
  switch (role) {
    case "customer":
      return ["customer"];
    case "operator":
      return ["operator"];
    case "risk":
      return ["risk"];
    case "system":
      return [];
    default:
      return [];
  }
}

/** 理财类型（wealth_product.json product_type）。 */
export const PRODUCT_TYPE_LABELS: Readonly<Record<string, string>> = {
  fixed_income: "固定收益",
  money_market: "货币市场",
  mixed: "混合",
  equity: "权益类",
};

/** 订阅周期（subscription.json period）。 */
export const SUBSCRIPTION_PERIOD_LABELS: Readonly<Record<string, string>> = {
  weekly: "每周",
  monthly: "每月",
  quarterly: "每季",
  yearly: "每年",
};

/** 强验证挑战状态（auth_challenge.json status）。 */
export const CHALLENGE_STATUS_LABELS: Readonly<Record<string, string>> = {
  pending: "待验证",
  verified: "已验证",
  failed: "验证失败",
  locked: "已锁定",
  expired: "已过期",
};

/** 回执验签/状态（receipt.json）。 */
export const RECEIPT_VERIFIED_LABELS: Readonly<Record<string, string>> = {
  verified: "已验签",
  unverified: "未验签",
  conflict: "回执冲突（待人工核验）",
  rejected: "回执被拒绝",
};

export const RECEIPT_STATE_LABELS: Readonly<Record<string, string>> = {
  accepted: "已接收",
  applied: "已入账",
  duplicate: "重复回执（已去重）",
  conflict: "冲突（已隔离）",
  quarantined: "隔离待核验",
};

/** 领域结果来源（domain_result.json source）。 */
export const DOMAIN_SOURCE_LABELS: Readonly<Record<string, string>> = {
  local_ledger: "本地账本",
  provider_query: "提供方查询",
  provider_callback: "提供方回调",
  synthetic_dataset: "合成数据集",
  offline_replay: "离线重放",
  operator_input: "运营录入",
};

/** 通用标签查询：未登记时返回 `未知状态（原值）`，永不返回空串。 */
export function labelOf(
  table: Readonly<Record<string, string>>,
  value: string | null | undefined,
  fallbackPrefix = "未知状态",
): string {
  if (value === null || value === undefined || String(value).trim() === "") {
    return `${fallbackPrefix}（未提供）`;
  }
  const key = String(value);
  return table[key] ?? `${fallbackPrefix}（${key}）`;
}

/** 风险等级文字：兼容 R1—R5、中文档位与未登记值。 */
export function riskLevelLabel(level: string | null | undefined): string {
  if (level === null || level === undefined || String(level).trim() === "") {
    return "风险等级未提供";
  }
  const key = String(level).trim().toUpperCase();
  if (RISK_LEVEL_LABELS[key]) return RISK_LEVEL_LABELS[key] as string;
  const byName = RISK_LEVEL_BY_NAME[String(level).trim()];
  if (byName) return byName;
  return `未知风险等级（${String(level)}）`;
}

/** 计划步骤文字：优先用归一后的标准标签，未登记则显示原值。 */
export function planStepStatusLabel(raw: string | null | undefined): string {
  const normalized = normalizePlanStepStatus(raw);
  if (normalized) return PLAN_STEP_STATUS_LABELS[normalized];
  if (!raw) return "状态未提供";
  return `未知状态（${String(raw)}）`;
}

/** 展示状态是否代表"已经有真实外部效果"（用于解释为何不能撤回）。 */
export function isStepInFlight(raw: string | null | undefined): boolean {
  const normalized = normalizePlanStepStatus(raw);
  return normalized === "submitted" || normalized === "awaiting_result" || normalized === "result_unknown";
}