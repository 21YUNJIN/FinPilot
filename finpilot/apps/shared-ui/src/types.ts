/**
 * 与后端契约一致的 TypeScript 类型。
 *
 * 唯一来源（按优先级）：
 * 1. `contracts/schemas/*.json`（JSON Schema，R0 已冻结；本文件逐字段对齐）
 * 2. `docs/INTERFACES.md` 第 1、4、5 节（HTTP 契约、信封、金额/时间/ID 口径）
 * 3. `services/api/src/finpilot/models.py` 顶部状态常量（状态字符串逐字一致）
 *
 * 约定：
 * - 金额一律 `amount_minor: int`（整数分，币种固定 CNY）；份额 `shares_e8`（1e-8 定点）；
 *   费率用整数基点（1bp = 0.01%），日收益率用十进制定点字符串——均为避免浮点漂移。
 * - 时间一律带时区 ISO 8601 字符串（持久化 UTC），展示统一转 Asia/Shanghai。
 * - 可写对象带 `version` / `revision`；写请求必须带 `expected_revision` 与 `operation_id`。
 * - 标注「展示补充」的字段表示契约尚未定义、由前端为呈现需要而读取；使用处都做了缺省兜底，
 *   一旦后端定稿应替换为契约字段（见文件末尾的说明与 docs/INTERFACES.md）。
 */

// ---------------------------------------------------------------------------
// 运行模式：settings.app_mode ∈ {live, offline_replay, mock}
// 缺失或非法一律标为"未标注"，不得默认 live（NFR-04）。
// ---------------------------------------------------------------------------

export const APP_MODES = ["live", "offline_replay", "mock"] as const;
export type AppMode = (typeof APP_MODES)[number];

export interface Provenance {
  adapter: string;
  observed_at: string;
  schema_version: string;
  /** 信封/任务/领域结果都会带 mode；缺失时不得推断为 live。 */
  mode?: AppMode | string;
  [key: string]: unknown;
}

// ---------------------------------------------------------------------------
// 统一信封（services/api/src/finpilot/envelope.py）
// ---------------------------------------------------------------------------

export const ENVELOPE_STATUSES = ["ok", "pending", "unknown", "failed", "rejected"] as const;
export type EnvelopeStatus = (typeof ENVELOPE_STATUSES)[number];

export const RETRY_MODES = [
  "never",
  "safe_query",
  "query_same_operation",
  "retry_same_operation",
  "retry_after_backoff",
  "after_reauthorization",
  "after_input_fix",
] as const;
export type RetryMode = (typeof RETRY_MODES)[number];

export interface ErrorObject {
  code: string;
  safe_message?: string;
  retry_mode?: RetryMode | string;
  reason_codes?: string[];
  details?: Record<string, unknown>;
}

export interface Envelope<T> {
  schema_version?: string;
  request_id?: string;
  task_id?: string | null;
  operation_id?: string | null;
  status: EnvelopeStatus;
  data: T | null;
  error: ErrorObject | null;
  provenance?: Provenance;
}

// ---------------------------------------------------------------------------
// 状态常量（models.py 顶部；前端只做展示映射，不发明状态值）
// ---------------------------------------------------------------------------

export type TaskStatus =
  | "draft"
  | "clarifying"
  | "planning"
  | "awaiting_authorization"
  | "ready"
  | "running"
  | "reconciling"
  | "replanning"
  | "awaiting_human"
  | "succeeded"
  | "partially_succeeded"
  | "failed"
  | "cancelled"
  | "blocked";

export type OperationStatus =
  | "created"
  | "awaiting_authorization"
  | "authorized"
  | "dispatching"
  | "pending"
  | "unknown"
  | "succeeded"
  | "failed"
  | "cancelled";

export type PlanStatus = "draft" | "awaiting_authorization" | "confirmed" | "running" | "completed" | "superseded" | "cancelled";

/** plan_step.json：步骤状态独立于 Task/Operation；unknown 必须查询原操作后才能判定。 */
export type PlanStepStatus =
  | "pending"
  | "running"
  | "awaiting_authorization"
  | "succeeded"
  | "failed"
  | "skipped"
  | "unknown"
  | "cancelled";

export type GrantStatus = "pending" | "active" | "consumed" | "revoked" | "expired";
export type AuthLevel = "GREEN" | "YELLOW" | "RED";
export type ReservationStatus = "active" | "partially_captured" | "captured" | "released" | "expired";
export type JobState =
  | "scheduled"
  | "leased"
  | "running"
  | "completed"
  | "awaiting_authorization"
  | "missed"
  | "failed"
  | "cancelled";
export type CardLifecycle = "inactive" | "active" | "lost" | "closed";
export type CardApplicationStatus = "submitted" | "under_review" | "approved" | "rejected" | "withdrawn";
export type CardControlType = "user_lock" | "channel_lock" | "user_limit" | "risk_freeze" | "risk_limit";
export type CardControlRole = "customer" | "operator" | "risk" | "system";
export type SubscriptionStatus = "active" | "cancelled" | "pending_cancellation" | "ended";
export type SubscriptionPeriod = "weekly" | "monthly" | "quarterly" | "yearly";
export type MandateStatus = "active" | "revoked" | "pending_revocation";
export type WealthOrderStatus =
  | "submitted"
  | "accepted"
  | "awaiting_confirmation"
  | "confirmed"
  | "rejected"
  | "redeeming"
  | "settled"
  | "failed"
  | "cancelled";
export type ChallengeStatus = "pending" | "verified" | "failed" | "locked" | "expired";
export type ReceiptState = "accepted" | "applied" | "duplicate" | "conflict" | "quarantined";
export type ReceiptVerifiedStatus = "verified" | "unverified" | "conflict" | "rejected";
export type ReceiptSource = "provider_callback" | "provider_query" | "local_ledger" | "operator_input";
export type TaskTerminalStatus = "succeeded" | "partially_succeeded" | "failed" | "cancelled";
export type Channel = "app" | "im" | "operator";
export type DomainName =
  | "cards"
  | "billing"
  | "wealth"
  | "subscriptions"
  | "aa"
  | "merchant"
  | "quant"
  | "transfers";
export type DomainSource =
  | "local_ledger"
  | "provider_query"
  | "provider_callback"
  | "synthetic_dataset"
  | "offline_replay"
  | "operator_input";

// ---------------------------------------------------------------------------
// GoalSpec（contracts/schemas/goal.json）
// 事实、硬约束、软偏好、待澄清项与授权状态分开保存。
// ---------------------------------------------------------------------------

export type GoalAuthorizationStatus = "not_requested" | "pending" | "granted" | "revoked" | "expired";

export interface GoalHardConstraints {
  currency: "CNY";
  total_budget_minor: number;
  budget_includes_fees?: boolean;
  protected_floor_minor: number;
  protected_floor_scope?: string[];
  delivery_deadline?: string;
  max_confirmations?: number;
}

/** 软偏好：成对比较（higher 优先于 lower），没有用户明确优先级时不得自行设权重。 */
export interface GoalPreference {
  higher: string;
  lower: string;
}

export interface GoalSpec {
  schema_version?: "1.0";
  task_id: string;
  revision: number;
  reference_time: string;
  /** 结构化目标标识（不是自由文本）。 */
  goals: string[];
  hard_constraints: GoalHardConstraints;
  preferences: GoalPreference[];
  /** 待澄清字段名；非空时不得执行任何写操作。 */
  unresolved: string[];
  authorization_status: GoalAuthorizationStatus;
  partial_completion_policy?: string;
  funding_sources?: string[];
  unconfirmed_receivables_are_spendable?: false;
  /** 推断项必须显式标记为推断。 */
  inferred_fields?: string[];
  /** 来源证据引用。 */
  evidence?: string[];
}

// ---------------------------------------------------------------------------
// Clarification（contracts/schemas/clarification.json）
// ---------------------------------------------------------------------------

export interface ClarificationOption {
  option_id: string;
  label: string;
  object_ref?: string;
  detail?: string;
}

export interface Clarification {
  clarification_id: string;
  task_id: string;
  revision: number;
  field: string;
  prompt: string;
  blocking: boolean;
  options?: ClarificationOption[];
  status: "open" | "answered" | "cancelled" | "expired";
  answer?: string | null;
  answered_at?: string | null;
  created_at: string;
  reason_code?: string;
}

// ---------------------------------------------------------------------------
// 计划与步骤（plan.json / plan_step.json）
// ---------------------------------------------------------------------------

/**
 * 步骤展示状态（SRS 18.2 / A03 要求逐条显示的真实状态）。
 * 契约状态通过 `normalizePlanStepStatus` 归一到这里；未知值回退为"未知状态（原值）"。
 */
export type PlanStepDisplayStatus =
  | "not_started"
  | "awaiting_confirmation"
  | "verifying"
  | "running"
  | "submitted"
  | "awaiting_result"
  | "succeeded"
  | "failed"
  | "stopped"
  | "result_unknown";

export interface CompensationPolicy {
  mode: "none" | "auto" | "manual" | "within_authorized_scope";
  tool?: string | null;
  reason?: string;
  max_cost_minor?: number;
  requires_reauthorization?: boolean;
}

export interface PlanStep {
  step_id: string;
  plan_id: string;
  /** 0 起的执行序号。 */
  seq: number;
  node_id: string;
  /** 必须来自 contracts/tool-catalog.yaml。 */
  tool: string;
  typed_args: Record<string, unknown>;
  dependencies?: string[];
  read_set?: string[];
  write_set?: string[];
  preconditions?: string[];
  expected_effects?: Record<string, unknown>;
  compensation_policy?: CompensationPolicy;
  required_level: AuthLevel | string;
  status: PlanStepStatus | string;
  operation_id?: string | null;
  attempt?: number;
  /** failed 时必填的错误码/原因（服务端给出）。 */
  last_error?: string | null;
}

export interface PlanSolverInfo {
  name?: string;
  version?: string;
  candidates?: number;
  nodes?: number;
  elapsed_ms?: number;
  /** 仅在满足算法前提时允许声称最优。 */
  optimal_claim?: boolean;
}

export interface Plan {
  plan_id: string;
  task_id: string;
  revision: number;
  status: PlanStatus | string;
  goal_snapshot?: GoalSpec;
  snapshot_ref?: Record<string, unknown>;
  /** 确认范围结构；与 UI 摘要同源（security/scope.build_scope）。 */
  scope?: ScopeSummary | Record<string, unknown>;
  /** sha256 十六进制摘要。 */
  scope_hash: string;
  required_level: AuthLevel | string;
  feasible: boolean;
  infeasible_reasons?: string[];
  alternatives?: Array<Record<string, unknown>>;
  steps: PlanStep[];
  total_estimated_minor: number;
  total_fee_minor: number;
  solver?: PlanSolverInfo;
  confirmed_at?: string | null;
  created_at: string;
}

// ---------------------------------------------------------------------------
// 授权范围摘要（INTERFACES.md §3 security/scope.describe_scope；与 scope_hash 同源）
// 契约中 scope 为 object（结构由 security/scope.py 定义），这里按展示所需字段建模，
// 未识别字段走 `extra` 原样展示，避免隐藏参数。
// ---------------------------------------------------------------------------

export interface ScopeCounterparty {
  name?: string;
  account_id?: string;
  bank?: string;
  payee_id?: string;
}

export interface ScopeTimeWindow {
  start?: string | null;
  end?: string | null;
  label?: string;
}

export interface ScopeSummary {
  tool?: string;
  amount_minor?: number | null;
  fee_minor?: number | null;
  irreversible_cost_minor?: number | null;
  currency?: string;
  source_account_id?: string | null;
  object_label?: string | null;
  counterparty?: ScopeCounterparty | string | null;
  time_window?: ScopeTimeWindow | string | null;
  irreversible?: boolean;
  irreversible_impacts?: string[];
  cancel_conditions?: string[];
  scope_hash?: string;
  notes?: string[];
  /** 未识别键值，按原样列出。 */
  extra?: Record<string, unknown>;
}

// ---------------------------------------------------------------------------
// 操作与回执（operation_view.json / receipt.json）
// ---------------------------------------------------------------------------

export interface OperationView {
  operation_id: string;
  owner_user_id?: string;
  task_id?: string | null;
  plan_id?: string | null;
  plan_revision?: number;
  tool: string;
  typed_args?: Record<string, unknown>;
  parameters_hash?: string;
  idempotency_key?: string;
  status: OperationStatus | string;
  required_level: AuthLevel | string;
  grant_id?: string | null;
  source_account_id?: string | null;
  amount_minor: number;
  fee_minor: number;
  currency: string;
  provider_id?: string | null;
  provider_order_ref?: string | null;
  original_operation_id?: string | null;
  /** 只有权威回执确认后才为 true；HTTP 200 不等于成功。 */
  effect_confirmed?: boolean;
  unknown_since?: string | null;
  submitted_at?: string | null;
  settled_at?: string | null;
  version: number;
  domain_result?: DomainResult | null;
  receipt_id?: string | null;
  /** failed 时必填。 */
  error_code?: string | null;
  pending_actions?: string[];
  created_at: string;
  updated_at: string;
}

export interface Receipt {
  receipt_id: string;
  operation_id: string;
  provider_id: string;
  provider_ref: string;
  source: ReceiptSource | string;
  event_version: number;
  received_at: string;
  verified_status: ReceiptVerifiedStatus | string;
  payload_hash: string;
  payload: Record<string, unknown>;
  state: ReceiptState | string;
  conflict_reason?: string | null;
}

/** 类型化领域结果（domain_result.json）。 */
export interface DomainResult {
  domain: DomainName | string;
  result_type: string;
  data: Record<string, unknown>;
  source: DomainSource | string;
  terms_version?: string;
  computation_version?: string;
  policy_version?: string;
  dataset_hash?: string;
  observed_at: string;
  available_at?: string;
  warnings: string[];
  /** 商户/产品自由文本：不可信，不能与结构化金额混用。 */
  untrusted_text?: string | null;
  operation_id?: string | null;
  task_id?: string | null;
  provenance?: { adapter?: string; mode?: AppMode | string; schema_version?: "1.0" };
}

/**
 * 用户可见的回执口径（A03）：由权威 `OperationView`（+ 可选 `Receipt`）推导，
 * 不是独立契约对象；推导规则见 api/adapters.ts 的 receiptOutcomeOf。
 */
export type ReceiptOutcome =
  | "scheduled"
  | "accepted"
  | "awaiting_confirmation"
  | "settled"
  | "partially_succeeded"
  | "failed"
  | "unknown";

// ---------------------------------------------------------------------------
// 授权与验证（INTERFACES.md §2 kernel/grants.py、kernel/verifier.py；
// 契约中 grant.json / auth_challenge.json 字段以本文件为准对齐）
// ---------------------------------------------------------------------------

export interface Grant {
  grant_id: string;
  owner_user_id?: string;
  task_id?: string | null;
  plan_id: string;
  plan_revision: number;
  scope?: ScopeSummary | Record<string, unknown>;
  scope_hash?: string;
  required_level: AuthLevel | string;
  status: GrantStatus | string;
  max_uses?: number | null;
  max_total_minor?: number | null;
  used_count?: number;
  consumed_minor?: number;
  expires_at?: string | null;
  activated_at?: string | null;
  consumed_at?: string | null;
  revoked_at?: string | null;
  version?: number;
}

/** 强验证挑战（auth_challenge.json）：不含验证码、秘钥或 factor_response。 */
export interface AuthChallenge {
  challenge_id: string;
  user_id: string;
  operation_scope_hash: string;
  scope?: Record<string, unknown>;
  plan_id?: string | null;
  plan_revision?: number;
  factor_type: "totp";
  status: ChallengeStatus | string;
  expires_at: string;
  /** 已尝试次数（0—5）。 */
  attempts: number;
  max_attempts: 5;
  locked_until?: string | null;
  verified_at?: string | null;
  consumed_at?: string | null;
  last_counter?: number | null;
  created_at?: string;
}

// ---------------------------------------------------------------------------
// 账户快照（INTERFACES.md §2 kernel/snapshots.py）
// ---------------------------------------------------------------------------

export interface AccountSnapshot {
  account_id: string;
  currency: string;
  posted_balance_minor: number;
  reserved_minor: number;
  frozen_other_minor: number;
  available_minor: number;
  spendable_minor: number;
  version?: number;
  protected_floor_minor?: number;
}

export interface Snapshot {
  accounts: AccountSnapshot[];
  protected_floor_minor: number;
  protection_scope: string[];
  observed_at: string;
  pending_inflows_minor: number;
  unconfirmed_receivables_minor: number;
}

// ---------------------------------------------------------------------------
// 卡片（card.json / card_control.json）
// ---------------------------------------------------------------------------

export interface CardControl {
  control_id: string;
  card_id: string;
  control_type: CardControlType | string;
  channel?: "atm" | "pos" | "online" | "contactless" | "all" | null;
  limit_minor?: number | null;
  reason?: string;
  created_by: string;
  created_by_role: CardControlRole | string;
  active: boolean;
  released_by?: string | null;
  released_at?: string | null;
  created_at: string;
}

export interface Card {
  card_id: string;
  owner_user_id?: string;
  account_id: string;
  /** 仅脱敏展示：**** **** **** 1234（契约禁止完整卡号/CVV/有效期）。 */
  masked_pan: string;
  brand?: string;
  lifecycle_status: CardLifecycle | string;
  credit_limit_minor: number;
  replaces_card_id?: string | null;
  replaced_by_card_id?: string | null;
  controls?: CardControl[];
  /** 叠加后的限制视图（security/card_controls.effective_controls）。 */
  effective_controls?: Record<string, unknown>;
  version: number;
  created_at?: string;
  updated_at?: string;
  /** 展示补充：申请状态由 card_application.json 单独提供，界面单独展示，不混进卡状态。 */
  application_status?: CardApplicationStatus | string | null;
  /** 展示补充：用户自设限额（如后端在 effective_controls 内提供则优先用它）。 */
  user_limit_minor?: number | null;
}

// ---------------------------------------------------------------------------
// 账单（bill_report.json）
// ---------------------------------------------------------------------------

export interface BillPeriod {
  kind: "month" | "year" | "custom";
  year?: number;
  month?: number;
  start_date?: string;
  end_date?: string;
  timezone: "Asia/Shanghai";
}

export interface BillCategory {
  category: string;
  amount_minor: number;
  count: number;
  share_bps?: number;
}

export interface BillAnomaly {
  label: string;
  /** 提示依据必须可核查；不得表述为欺诈定论。 */
  basis: string;
}

export interface BillReport {
  report_id: string;
  owner_user_id?: string;
  period: BillPeriod;
  generated_at: string;
  data_cutoff_at: string;
  currency: "CNY";
  total_inflow_minor: number;
  total_outflow_minor: number;
  net_minor: number;
  internal_transfer_minor?: number;
  internal_transfers_excluded_from_consumption?: true;
  fee_minor?: number;
  refund_minor?: number;
  categories: BillCategory[];
  uncategorized_minor?: number;
  zero_denominator_flags?: string[];
  incomplete_transactions?: string[];
  corrections?: string[];
  anomalies?: BillAnomaly[];
  warnings: string[];
  untrusted_text?: string | null;
  source?: "synthetic" | "local_ledger" | "offline_replay";
  provenance?: Provenance;
}

// ---------------------------------------------------------------------------
// 理财（wealth_product.json / risk_profile.json）
// ---------------------------------------------------------------------------

export interface QuantRiskReport {
  status: string;
  message?: string;
  source_verified?: boolean;
  request_id?: string;
  data_window?: { start: string; end: string; points: number; returns: number };
  metrics: Record<string, { value: number | null; unit: string; na_reason?: string | null }>;
  provenance?: { method_version?: string; source_hash?: string; source?: string };
  warnings?: string[];
}

export interface WealthProduct {
  eligible?: boolean;
  exclusion_reason_codes?: string[];
  quant_risk?: QuantRiskReport | null;
  product_id: string;
  name: string;
  issuer: string;
  product_type: "fixed_income" | "money_market" | "mixed" | "equity" | string;
  terms_version: string;
  issuer_risk_level: "R1" | "R2" | "R3" | "R4" | "R5" | string;
  min_minor: number;
  fee_bps: number;
  fee_fixed_minor: number;
  term_days?: number;
  open_days?: string[];
  cutoff_time?: string;
  liquidity_terms?: string;
  cancel_terms?: string;
  cooling_off_days?: number;
  settlement_days?: number;
  history_ref?: string;
  nav_e6?: number | null;
  nav_as_of_date?: string | null;
  /** 数据不足时必须为 true，展示为 NA（不适用）。 */
  nav_is_na?: boolean;
  daily_return_bps?: string | null;
  meta?: Record<string, unknown>;
  is_active: boolean;
  /** 展示补充：数据出处（提供方/口径文档）。 */
  source?: string | null;
  assessment_required?: boolean;
}

export interface RiskProfile {
  risk_profile_id?: string;
  user_id?: string;
  level: string;
  valid_until?: string | null;
  assessed_at?: string | null;
  source?: string;
  /** 展示补充：问卷版本。 */
  questionnaire_version?: string;
}

// ---------------------------------------------------------------------------
// 订阅与代扣（subscription.json / mandate.json）
// ---------------------------------------------------------------------------

export interface Subscription {
  subscription_id: string;
  owner_user_id?: string;
  merchant_id: string;
  merchant_name: string;
  amount_minor: number;
  period: SubscriptionPeriod | string;
  next_charge_at?: string | null;
  status: SubscriptionStatus | string;
  /** contract=真实合同；inferred=从一个周期流水推断。 */
  evidence: "contract" | "inferred" | string;
  inference_confidence_bps?: number;
  is_contract_evidence?: boolean;
  terms_version: string;
  cancel_terms?: string;
  mandate_id?: string | null;
  last_charged_at?: string | null;
  cancelled_at?: string | null;
  version: number;
  created_at: string;
  /** 展示补充：网关可能内联代扣授权状态；优先用独立的 Mandate 对象。 */
  mandate?: Mandate | null;
}

export interface Mandate {
  mandate_id: string;
  owner_user_id?: string;
  subscription_id?: string | null;
  merchant_id: string;
  account_id?: string | null;
  status: MandateStatus | string;
  revoked_at?: string | null;
  pending_revocation_at?: string | null;
  version: number;
  created_at: string;
}

// ---------------------------------------------------------------------------
// AA 收款（aa_request.json：结构以 contracts 为准，这里按展示所需字段建模）
// ---------------------------------------------------------------------------

export interface AAParticipant {
  participant_id?: string;
  name: string;
  share_minor: number;
  status?: string;
  paid_at?: string | null;
  phone_ref?: string;
}

export interface AARequest {
  request_id: string;
  task_id?: string;
  owner_user_id?: string;
  title?: string;
  total_amount_minor: number;
  currency?: "CNY";
  share_mode?: string;
  participants: AAParticipant[];
  status: string;
  created_at?: string;
  settled_amount_minor?: number | null;
  unconfirmed_receivables_minor?: number | null;
  note?: string;
}

// ---------------------------------------------------------------------------
// 任务视图与结果（task_view.json / result_view.json）
// ---------------------------------------------------------------------------

export interface WaitingReason {
  code:
    | "awaiting_authorization"
    | "awaiting_human"
    | "awaiting_funds"
    | "awaiting_provider"
    | "awaiting_reconciliation"
    | "planning_limit"
    | string;
  safe_message: string;
  operation_id?: string | null;
  grant_id?: string | null;
  challenge_id?: string | null;
  /** 展示补充：起始时间与可执行动作（契约未定义时留空，界面不编造）。 */
  since?: string | null;
  action_hint?: string | null;
}

export interface TaskProgress {
  completed_steps: number;
  total_steps: number;
  completed_effects?: string[];
  pending_effects?: string[];
}

/** task_view.progress 之外的权威结果摘要（result_view.json 的完整对象）。 */
export interface ResultEffect {
  kind: string;
  description: string;
  /** 只允许展示已核验事实。 */
  verified: true;
  authority: "receipt" | "ledger" | "operator" | string;
  operation_id?: string | null;
  amount_minor?: number;
}

export interface NextAction {
  action_id: string;
  label: string;
  kind?: "confirm" | "verify" | "clarify" | "retry_same_operation" | "wait" | "contact_operator" | "cancel" | string;
}

export interface TaskResultView {
  task_id: string;
  task_status: TaskStatus | string;
  revision: number;
  mode: AppMode | string;
  generated_at: string;
  goal_summary?: Record<string, unknown>;
  completed_effects: ResultEffect[];
  pending_effects?: string[];
  unknown_effects?: string[];
  failed_effects?: string[];
  waiting_reason?: WaitingReason | null;
  next_actions: NextAction[];
  total_spent_minor?: number;
  total_fee_minor?: number;
  receipts?: Receipt[];
  provenance?: Provenance;
}

/**
 * 任务视图（task_view.json）。
 * 注意：`result_summary` 在契约中是 object（摘要），完整结果用 result_view.json；
 * `messages` / `grants` / `handoff` 为展示补充（网关可能内联或另行提供）。
 */
export interface TaskView {
  task_id: string;
  owner_user_id?: string;
  channel: Channel | string;
  status: TaskStatus | string;
  revision: number;
  reference_time: string;
  mode: AppMode | string;
  created_at: string;
  updated_at: string;
  goal_summary?: Record<string, unknown>;
  goal?: GoalSpec | null;
  clarifications?: Clarification[];
  plan_id?: string | null;
  plan?: Plan | null;
  progress?: TaskProgress | null;
  waiting_reason?: WaitingReason | null;
  result_summary?: TaskResultView | Record<string, unknown> | null;
  blocked_reason?: string | null;
  spent_minor?: number;
  fee_minor?: number;
  /** 展示补充：对话消息（消息接口或网关内联；契约未定义字段名，见文件末尾假设）。 */
  messages?: TaskMessage[];
  /** 展示补充：该任务当前授权。 */
  grants?: Grant[];
  /** 展示补充：当前待处理挑战（结构与 auth_challenge.json 一致）。 */
  challenge?: AuthChallenge | null;
  /** 展示补充：人工接管视图（A05；契约尚未定义 schema）。 */
  handoff?: HandoffCase | null;
}

export interface TaskMessageAttachment {
  kind: "authorization" | "receipt" | "challenge" | "plan" | string;
  ref_id: string;
  level?: AuthLevel | string;
  status?: string;
}

export interface TaskMessage {
  message_id: string;
  role: "user" | "assistant" | "system" | "operator" | string;
  text: string;
  created_at: string;
  task_revision?: number;
  operation_id?: string | null;
  attachments?: TaskMessageAttachment[];
  kind?: string;
}

// ---------------------------------------------------------------------------
// 人工接管（A05 / orchestration/handoff.case_handoff）
// 契约尚未提供 JSON Schema：以下为客户端展示结构，标注为「展示补充」。
// ---------------------------------------------------------------------------

export interface HandoffItem {
  label: string;
  detail?: string;
  operation_id?: string | null;
  amount_minor?: number | null;
  status?: string;
  reason?: string;
}

export interface HandoffFunds {
  currency?: string;
  spent_minor?: number | null;
  fees_minor?: number | null;
  reserved_minor?: number | null;
  unknown_minor?: number | null;
  refund_pending_minor?: number | null;
}

export interface HandoffAuthorization {
  grant_id?: string;
  plan_id?: string;
  scope_hash?: string;
  status?: string;
  note?: string;
}

export interface OperatorAction {
  action: string;
  at: string;
  by?: string;
  note?: string;
}

export interface HandoffCase {
  case_id: string;
  task_id?: string;
  status: string;
  reason: string;
  opened_at?: string;
  completed: HandoffItem[];
  unknown: HandoffItem[];
  not_submitted: HandoffItem[];
  funds: HandoffFunds;
  authorizations: HandoffAuthorization[];
  operator_actions?: OperatorAction[];
  operator_may_approve?: boolean;
}

// ---------------------------------------------------------------------------
// 隐私（SEC-04；契约尚未提供 JSON Schema：客户端展示结构）
// ---------------------------------------------------------------------------

export interface RetentionRule {
  data_class: string;
  retention: string;
  note?: string;
}

export interface PrivacyExportView {
  request_id?: string;
  generated_at?: string;
  download_url?: string | null;
  expires_at?: string | null;
  sections?: string[];
  retention?: RetentionRule[];
  note?: string;
}

export interface PrivacyDeleteRequest {
  scope: string;
  reason?: string;
  expected_revision?: number;
}

export interface PrivacyDeleteView {
  request_id?: string;
  status: string;
  scope: string;
  accepted_at?: string;
  effective_at?: string | null;
  retained_basis?: string;
  note?: string;
}

// ---------------------------------------------------------------------------
// 会话与身份（gateway/auth）
// ---------------------------------------------------------------------------

export interface UserProfile {
  user_id: string;
  display_name?: string;
  roles?: string[];
  totp_enrolled?: boolean;
}

export interface SessionView {
  token: string;
  expires_at?: string;
  user: UserProfile;
}

export interface LoginCredentials {
  user_id: string;
  password: string;
  channel: string;
}

// ---------------------------------------------------------------------------
// 工具目录（GET /v1/meta/tools）
// ---------------------------------------------------------------------------

export interface ToolCatalogEntry {
  tool: string;
  description?: string;
  read_only?: boolean;
  required_level?: AuthLevel | string;
  idempotent?: boolean;
  preconditions?: string[];
  effects?: string[];
}

// ---------------------------------------------------------------------------
// 客户端侧（非后端）类型
// ---------------------------------------------------------------------------

export interface VersionConflictDetail {
  code: "VERSION_CONFLICT";
  message: string;
  expectedRevision: number;
  serverRevision?: number | null;
  detectedAt: string;
}

export interface LocalConfirmedAction {
  actionId: string;
  kind: "confirm_plan" | "verify_challenge" | "send_message" | "cancel_task" | "resume_task" | "revoke_grant";
  label: string;
  at: string;
  expectedRevision: number;
  operationId?: string | null;
  acknowledged?: boolean;
}

/**
 * 与契约的已知差异（需与 B/D 对齐后收敛）：
 * 1. task_view.messages / grants / handoff / challenge：契约未定义，前端按展示需要读取；
 *    若网关不内联，改由消息接口与 grants 接口提供。
 * 2. WaitingReason.since / action_hint：契约只有 code/safe_message/operation_id/grant_id/challenge_id。
 * 3. ReceiptOutcome：由 OperationView 推导的展示口径，不是独立契约对象。
 * 4. HandoffCase / PrivacyExportView / RetentionRule：契约暂无 schema（A05 交付物）。
 * 5. Card.application_status / user_limit_minor：申请与额度在契约中另有对象（card_application.json）。
 */
// ---------------------------------------------------------------------------
// 风险测评问卷（S3）：逐题作答，不允许由对话直接给出等级
// ---------------------------------------------------------------------------

export interface QuestionnaireOption {
  option_id: string;
  label: string;
  score: number;
}

export interface QuestionnaireQuestion {
  question_id: string;
  dimension: string;
  dimension_label: string;
  text: string;
  options: QuestionnaireOption[];
}

export interface RiskQuestionnaireData {
  questionnaire_version: string;
  validity_days: number;
  max_score: number;
  dimensions: Array<{ dimension: string; label: string }>;
  questions: QuestionnaireQuestion[];
  disclaimer?: string;
}

export interface RiskAssessmentResult {
  profile_id: string;
  questionnaire_version: string;
  risk_level: string;
  score: number;
  assessed_at: string;
  expires_at: string;
  answers_hash: string;
}