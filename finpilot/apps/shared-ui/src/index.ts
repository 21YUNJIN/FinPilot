import "./styles.css";

// --- 契约常量与类型（显式列出，避免与同名展示组件产生导出歧义）---
export { APP_MODES, ENVELOPE_STATUSES, RETRY_MODES } from "./types";
export type {
  AARequest,
  AAParticipant,
  AccountSnapshot,
  AppMode,
  AuthChallenge,
  AuthLevel,
  BillAnomaly,
  BillCategory,
  BillPeriod,
  BillReport as BillReportData,
  Card,
  CardApplicationStatus,
  CardControl,
  CardControlRole,
  CardControlType,
  CardLifecycle,
  Channel,
  ChallengeStatus,
  Clarification,
  ClarificationOption,
  CompensationPolicy,
  DomainName,
  DomainResult,
  DomainSource,
  Envelope,
  EnvelopeStatus,
  ErrorObject,
  GoalAuthorizationStatus,
  GoalHardConstraints,
  GoalPreference,
  GoalSpec,
  Grant,
  GrantStatus,
  HandoffAuthorization,
  HandoffCase,
  HandoffFunds,
  HandoffItem,
  JobState,
  LocalConfirmedAction,
  LoginCredentials,
  Mandate,
  MandateStatus,
  NextAction,
  OperationStatus,
  OperationView,
  OperatorAction,
  Plan,
  PlanSolverInfo,
  PlanStatus,
  PlanStep,
  PlanStepDisplayStatus,
  PlanStepStatus,
  PrivacyDeleteRequest,
  PrivacyDeleteView,
  PrivacyExportView,
  Provenance,
  Receipt,
  ReceiptOutcome,
  ReceiptSource,
  ReceiptState,
  ReceiptVerifiedStatus,
  ReservationStatus,
  ResultEffect,
  RetentionRule,
  RetryMode,
  RiskProfile,
  ScopeCounterparty,
  ScopeSummary,
  ScopeTimeWindow,
  SessionView,
  Snapshot,
  Subscription,
  SubscriptionPeriod,
  SubscriptionStatus,
  TaskMessage,
  TaskMessageAttachment,
  TaskProgress,
  TaskResultView,
  TaskStatus,
  TaskTerminalStatus,
  TaskView,
  ToolCatalogEntry,
  UserProfile,
  VersionConflictDetail,
  WaitingReason,
  WealthOrderStatus,
  WealthProduct,
} from "./types";

// --- 契约标签（状态 → 中文，逐字对应 models.py 与 contracts/schemas）---
export * from "./contract/labels";

// --- HTTP 客户端、错误映射与展示适配器 ---
export * from "./api/client";
export * from "./api/errorMessages";
export * from "./api/endpoints";
export * from "./api/adapters";
export * from "./api/session";
export * from "./api/cache";
export * from "./api/ids";
export * from "./api/trace";

// --- 格式化 ---
export * from "./format/money";
export * from "./format/datetime";

// --- 组件 ---
export * from "./components/ModeBadge";
export * from "./components/StatusChip";
export * from "./components/MoneyText";
export * from "./components/ErrorBanner";
export * from "./components/VersionConflictNotice";
export * from "./components/GoalSummaryCard";
export * from "./components/PlanSteps";
export * from "./components/AuthorizationCard";
export * from "./components/ChallengeDialog";
export * from "./components/OperationReceipt";
export * from "./components/CardPanel";
export * from "./components/BillReport";
export * from "./components/WealthCompare";
export * from "./components/SubscriptionList";
export * from "./components/HandoffPanel";
export * from "./components/PrivacyPanel";
export * from "./components/TaskPanel";
export * from "./components/SessionGate";

// --- Hooks ---
export * from "./hooks/useTask";
export * from "./hooks/useSession";
export * from "./hooks/useResource";

// --- 工具 ---
export * from "./lib/cx";