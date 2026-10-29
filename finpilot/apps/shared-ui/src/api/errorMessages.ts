/**
 * 错误码 → 用户可读文案（**只读镜像，前端不得新增业务规则**）。
 *
 * 来源：`services/api/src/finpilot/errors.py` 的 `ERROR_SPECS`（V5 16.2）。
 * 说明：
 * - 这里是逐字复制的一份前端副本，只为在网络层失败、后端未返回 `safe_message` 或需要
 *   本地提示时兜底；后端返回了 `error.safe_message` 时**一律以后端为准**。
 * - 与后端的一致性由 `tests/error-mirror.parity.test.ts` 直接读取 errors.py 校验（含 HTTP 语义与
 *   retry_mode），任何漂移都会让该测试失败。
 * - 不在本文件新增、修改、翻译任何消息；不在前端重写业务判定。
 */

import type { RetryMode } from "../types";

/** 镜像来源（便于评审与自动校验定位）。 */
export const ERROR_SPEC_SOURCE =
  "services/api/src/finpilot/errors.py::ERROR_SPECS（V5 16.2 统一错误码）";

export interface ErrorMirrorEntry {
  readonly http_status: number;
  readonly retry_mode: RetryMode;
  readonly safe_message: string;
}

export const ERROR_MIRROR: Readonly<Record<string, ErrorMirrorEntry>> = {
  PARAM_INVALID: { http_status: 400, retry_mode: "after_input_fix", safe_message: "请求参数不合法，请修正后重试。" },
  AMOUNT_PRECISION: { http_status: 400, retry_mode: "after_input_fix", safe_message: "金额最多两位小数，请重新输入。" },
  SCHEMA_INVALID: { http_status: 400, retry_mode: "after_input_fix", safe_message: "请求结构与约定的 Schema 不一致。" },
  UNKNOWN_TOOL: { http_status: 400, retry_mode: "after_input_fix", safe_message: "该能力未在工具目录中登记，不能执行。" },
  EXTRA_DANGEROUS_FIELD: { http_status: 400, retry_mode: "after_input_fix", safe_message: "请求包含不允许的额外字段。" },
  UNAUTHENTICATED: { http_status: 401, retry_mode: "after_reauthorization", safe_message: "登录状态无效，请重新登录。" },
  FORBIDDEN: { http_status: 403, retry_mode: "never", safe_message: "当前身份没有执行该操作的权限。" },
  OBJECT_NOT_ACCESSIBLE: { http_status: 403, retry_mode: "never", safe_message: "无法访问该对象。" },
  POLICY_DENIED: { http_status: 403, retry_mode: "never", safe_message: "服务端策略不允许执行该操作。" },
  GRANT_REQUIRED: { http_status: 403, retry_mode: "never", safe_message: "该操作需要用户明确确认。" },
  GRANT_INVALID: { http_status: 403, retry_mode: "never", safe_message: "授权无效或已失效。" },
  GRANT_SCOPE_VIOLATION: { http_status: 403, retry_mode: "never", safe_message: "该操作超出已授权范围，需要重新确认。" },
  CHALLENGE_EXPIRED: { http_status: 403, retry_mode: "after_reauthorization", safe_message: "验证已过期，请重新发起验证。" },
  CHALLENGE_FAILED: { http_status: 403, retry_mode: "after_reauthorization", safe_message: "验证码不正确。" },
  CHALLENGE_LOCKED: { http_status: 429, retry_mode: "retry_after_backoff", safe_message: "验证尝试次数过多，请稍后再试。" },
  FACTOR_NOT_ENROLLED: { http_status: 403, retry_mode: "after_reauthorization", safe_message: "尚未配置强验证因子，无法完成该操作。" },
  VERSION_CONFLICT: { http_status: 409, retry_mode: "after_input_fix", safe_message: "对象已被其他操作更新，请刷新后重试。" },
  IDEMPOTENCY_CONFLICT: { http_status: 409, retry_mode: "never", safe_message: "同一幂等键被用于不同参数，已拒绝。" },
  NOT_REVERSIBLE: { http_status: 409, retry_mode: "never", safe_message: "该操作按条款不可撤销。" },
  CANCEL_NOT_EFFECTIVE: { http_status: 409, retry_mode: "never", safe_message: "撤销请求未生效，原操作仍按实际结果处理。" },
  INSUFFICIENT_FUNDS: { http_status: 422, retry_mode: "after_input_fix", safe_message: "可用余额不足。" },
  PROTECTED_FLOOR_VIOLATION: { http_status: 422, retry_mode: "after_input_fix", safe_message: "该操作会穿透已确认的应急金底线。" },
  RESERVATION_EXCEEDED: { http_status: 422, retry_mode: "after_input_fix", safe_message: "预留金额超过该账户可用余额。" },
  RISK_MISMATCH: { http_status: 422, retry_mode: "after_input_fix", safe_message: "该产品与当前风险测评结果不匹配。" },
  ASSESSMENT_REQUIRED: { http_status: 422, retry_mode: "after_input_fix", safe_message: "需要先完成有效的风险测评。" },
  INFEASIBLE: { http_status: 422, retry_mode: "after_input_fix", safe_message: "在现有硬约束下没有可行方案。" },
  PLANNING_LIMIT: { http_status: 422, retry_mode: "after_input_fix", safe_message: "规划达到工程限额而中止，不代表任务无解。" },
  PROVIDER_REJECTED: { http_status: 422, retry_mode: "never", safe_message: "业务提供方拒绝了该请求。" },
  RATE_LIMITED: { http_status: 429, retry_mode: "retry_after_backoff", safe_message: "请求过于频繁，请稍后重试。" },
  PROVIDER_TIMEOUT: { http_status: 503, retry_mode: "query_same_operation", safe_message: "请求结果尚未确认，正在查询原订单。" },
  PROVIDER_UNAVAILABLE: { http_status: 503, retry_mode: "retry_same_operation", safe_message: "业务提供方暂时不可用，未产生资金效果。" },
  PROVIDER_CAPABILITY_MISSING: { http_status: 503, retry_mode: "never", safe_message: "提供方不支持稳定业务键或结果查询，已关闭自动写入。" },
  RESULT_UNKNOWN: { http_status: 503, retry_mode: "query_same_operation", safe_message: "结果未知，正在用原操作编号核对，请勿重复提交。" },
  SANDBOX_UNAVAILABLE: { http_status: 503, retry_mode: "never", safe_message: "分析沙箱不可用，已关闭生成代码执行。" },
  SANDBOX_VIOLATION: { http_status: 422, retry_mode: "never", safe_message: "生成代码触发了沙箱禁止能力，已阻止执行。" },
  AUDIT_UNAVAILABLE: { http_status: 503, retry_mode: "retry_after_backoff", safe_message: "审计记录不可写，高风险新提交已暂停。" },
  CIRCUIT_OPEN: { http_status: 503, retry_mode: "retry_after_backoff", safe_message: "连续失败触发熔断，冷却后仅允许有限探测。" },
  SAFETY_LOCKED: { http_status: 429, retry_mode: "retry_after_backoff", safe_message: "检测到可疑行为，高风险操作已暂时锁定。" },
  CAPABILITY_NOT_SUPPORTED: { http_status: 422, retry_mode: "after_input_fix", safe_message: "当前版本不支持该业务能力。" },
  DEPENDENCY_FAILED: { http_status: 503, retry_mode: "retry_after_backoff", safe_message: "依赖的离线或缓存能力失败，未伪造结果。" },
  CLARIFICATION_REQUIRED: { http_status: 409, retry_mode: "after_input_fix", safe_message: "关键信息未确定，需要先澄清。" },
  SYSTEM_ERROR: { http_status: 500, retry_mode: "retry_after_backoff", safe_message: "系统内部错误，未产生资金效果。" },
};

/** 错误码 → 文案（由镜像派生，保持同一来源）。 */
export const ERROR_MESSAGES: Readonly<Record<string, string>> = Object.freeze(
  Object.fromEntries(Object.entries(ERROR_MIRROR).map(([code, spec]) => [code, spec.safe_message])),
);

/** 客户端自身产生的错误码（不是后端契约的一部分，前缀 CLIENT_/NETWORK_ 以示区分）。 */
export const CLIENT_ERROR_MESSAGES: Readonly<Record<string, string>> = {
  NETWORK_ERROR: "无法连接到服务端，本次请求未获得业务结果；若为写操作请用原操作编号查询。",
  REQUEST_TIMEOUT: "请求超时，业务结果未知；请用原操作编号查询，不要重复提交。",
  REQUEST_ABORTED: "请求已被取消。",
  INVALID_ENVELOPE: "服务端未返回标准信封，无法确认业务状态（HTTP 200 也不代表成功）。",
  HTTP_ERROR: "服务端返回了非预期的 HTTP 状态，且未包含标准错误信封。",
  CLIENT_MISSING_EXPECTED_REVISION: "缺少 expected_revision：写操作必须先读取最新修订，已阻止本次提交。",
  CLIENT_MALFORMED_RESPONSE: "服务端响应无法解析，未确认任何业务效果。",
};

/** retry_mode → 用户可执行的下一步提示（语义来自 errors.py 的 RETRY_* 常量）。 */
export const RETRY_MODE_HINTS: Readonly<Record<string, string>> = {
  never: "该情况不可重试，请不要重复提交。",
  safe_query: "可以重新查询以确认结果。",
  query_same_operation: "请使用原操作编号查询结果，不要重复提交。",
  retry_same_operation: "可以使用同一操作编号重试。",
  retry_after_backoff: "请稍后再试。",
  after_reauthorization: "请重新登录或重新完成验证。",
  after_input_fix: "请修正输入或刷新后重试。",
};

export interface UserFacingError {
  code: string;
  message: string;
  hint: string;
  retryMode: RetryMode | string;
  /** 是否是 errors.py 登记过的错误码。 */
  knownCode: boolean;
  /** 是否需要"查询原订单"而不是重试提交。 */
  requiresQueryOfOriginalOperation: boolean;
}

export function isKnownErrorCode(code: string | null | undefined): boolean {
  return !!code && Object.prototype.hasOwnProperty.call(ERROR_MIRROR, code);
}

export function errorMessageFor(code: string | null | undefined): string {
  if (!code) return CLIENT_ERROR_MESSAGES.CLIENT_MALFORMED_RESPONSE as string;
  const mirrored = ERROR_MESSAGES[code] ?? CLIENT_ERROR_MESSAGES[code];
  if (mirrored) return mirrored;
  return `未登记的错误码 ${code}：请以服务端说明为准，若涉及资金请查询原操作编号。`;
}

export function retryModeHint(mode: string | null | undefined): string {
  if (!mode) return "请根据服务端说明处理；若涉及资金请先查询原操作编号。";
  return RETRY_MODE_HINTS[mode] ?? `未知的重试方式（${mode}）：请先查询原操作编号再决定。`;
}

export function defaultRetryModeFor(code: string | null | undefined): RetryMode | string {
  if (!code) return "never";
  const mirrored = ERROR_MIRROR[code];
  if (mirrored) return mirrored.retry_mode;
  const clientMode: Readonly<Record<string, RetryMode>> = {
    NETWORK_ERROR: "never",
    REQUEST_TIMEOUT: "query_same_operation",
    REQUEST_ABORTED: "never",
    INVALID_ENVELOPE: "never",
    HTTP_ERROR: "never",
    CLIENT_MISSING_EXPECTED_REVISION: "after_input_fix",
    CLIENT_MALFORMED_RESPONSE: "never",
  };
  return clientMode[code] ?? "never";
}

/**
 * 生成面向用户的错误说明。
 * `serverMessage` 存在时优先使用（后端 safe_message 是权威的、且不泄露内部信息）。
 */
export function resolveUserMessage(input: {
  code?: string | null;
  serverMessage?: string | null;
  retryMode?: string | null;
  reasonCodes?: string[];
}): UserFacingError {
  const code = input.code ?? "CLIENT_MALFORMED_RESPONSE";
  const retryMode = input.retryMode ?? defaultRetryModeFor(code);
  const knownCode = isKnownErrorCode(code);
  const message =
    (input.serverMessage && input.serverMessage.trim() !== "" ? input.serverMessage : null) ??
    errorMessageFor(code);
  return {
    code,
    message,
    hint: retryModeHint(retryMode),
    retryMode,
    knownCode,
    requiresQueryOfOriginalOperation:
      retryMode === "query_same_operation" ||
      code === "RESULT_UNKNOWN" ||
      code === "PROVIDER_TIMEOUT" ||
      code === "REQUEST_TIMEOUT",
  };
}