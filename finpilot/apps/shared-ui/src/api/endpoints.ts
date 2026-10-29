/**
 * 类型化接口封装：路径、请求字段与响应类型逐条对应 `contracts/openapi.yaml`。
 *
 * 关键约束（各接口 additionalProperties: false）：
 * - 幂等键字段按接口选择：messages/plan/cancel/resume/revoke → `operation_id`；
 *   confirm → `idempotency_key`；创建任务 → `client_message_id`；
 *   登录/TOTP/挑战校验 → 不带幂等键（一次性因子，重放必须失败）。
 * - 并发基线：messages/plan/cancel/resume/revoke/confirm 必须带 `expected_revision`。
 * - 模型不可调用的人类入口（confirm、verify）在这里只由界面按钮触发。
 * - 未在 OpenAPI 登记但实现需要的路径集中在文件末尾 `experimentalApi`，并标注状态。
 */

import type {
  AARequest,
  AuthChallenge,
  BillReport,
  Card,
  Grant,
  LoginCredentials,
  OperationView,
  Plan,
  PrivacyDeleteView,
  PrivacyExportView,
  Receipt,
  RiskProfile,
  SessionView,
  Snapshot,
  Subscription,
  TaskView,
  ToolCatalogEntry,
  UserProfile,
  WealthProduct,
} from "../types";
import { asList } from "./adapters";
import { get, mapResult, post, request, type ApiResult, type RequestOptions } from "./client";

const V1 = "/v1";

// ---------------------------------------------------------------------------
// 身份与会话
// ---------------------------------------------------------------------------

export const authApi = {
  login(credentials: LoginCredentials): Promise<ApiResult<SessionView>> {
    // 一次性凭据：不带幂等键（契约 x-idempotency-exempt）。
    return post<SessionView>(`${V1}/auth/sessions`, {
      user_id: credentials.user_id,
      password: credentials.password,
      channel: credentials.channel,
    });
  },

  enrollTotp(): Promise<ApiResult<{ secret: string; otpauth_uri: string; confirmed: boolean }>> {
    return post(`${V1}/auth/totp/enroll`, {});
  },

  /** 验证码只出现在这一个请求体里：不写对话、不写日志、不进模型上下文。 */
  confirmTotp(code: string): Promise<ApiResult<{ confirmed: boolean }>> {
    return post<{ confirmed: boolean }>(`${V1}/auth/totp/confirm`, { code });
  },

  /** 展示补充：OpenAPI 未登记 /v1/auth/me；未实现时返回 INVALID_ENVELOPE，界面按"不支持"显示。 */
  me(): Promise<ApiResult<UserProfile>> {
    return get<UserProfile>(`${V1}/auth/me`);
  },
};

// ---------------------------------------------------------------------------
// 任务（tasks）
// ---------------------------------------------------------------------------

export interface CreateTaskInput {
  message: string;
  channel: "app" | "im";
  referenceTime?: string;
  /** 客户端幂等键（映射到同一逻辑任务）。 */
  clientMessageId?: string;
}

export const taskApi = {
  create(input: CreateTaskInput): Promise<ApiResult<TaskView>> {
    return post<TaskView>(
      `${V1}/tasks`,
      {
        message: input.message,
        channel: input.channel,
        ...(input.referenceTime ? { reference_time: input.referenceTime } : {}),
      },
      { idempotencyField: "client_message_id", idempotencyKey: input.clientMessageId ?? null },
    );
  },

  get(taskId: string, options: { signal?: AbortSignal } = {}): Promise<ApiResult<TaskView>> {
    return get<TaskView>(`${V1}/tasks/${encodeURIComponent(taskId)}`, options);
  },

  async list(query: { limit?: number } = {}): Promise<ApiResult<TaskView[]>> {
    const result = await get<unknown>(`${V1}/tasks`, { query });
    return mapResult(result, (data) => asList<TaskView>(data, "tasks"));
  },

  sendMessage(
    taskId: string,
    input: { message: string; expectedRevision: number; operationId?: string | null; channel?: "app" | "im" },
  ): Promise<ApiResult<TaskView>> {
    return post<TaskView>(
      `${V1}/tasks/${encodeURIComponent(taskId)}/messages`,
      { message: input.message, ...(input.channel ? { channel: input.channel } : {}) },
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },

  requestPlan(
    taskId: string,
    input: { expectedRevision: number; operationId?: string | null; reason?: string },
  ): Promise<ApiResult<Plan>> {
    return post<Plan>(
      `${V1}/tasks/${encodeURIComponent(taskId)}/plan`,
      input.reason ? { reason: input.reason } : {},
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },

  cancel(
    taskId: string,
    input: { expectedRevision: number; operationId?: string | null; reason?: string },
  ): Promise<ApiResult<TaskView>> {
    return post<TaskView>(
      `${V1}/tasks/${encodeURIComponent(taskId)}/cancel`,
      input.reason ? { reason: input.reason } : {},
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },

  resume(
    taskId: string,
    input: { expectedRevision: number; operationId?: string | null },
  ): Promise<ApiResult<TaskView>> {
    return post<TaskView>(
      `${V1}/tasks/${encodeURIComponent(taskId)}/resume`,
      {},
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },
};

// ---------------------------------------------------------------------------
// 确认与强验证（人类专用入口）
// ---------------------------------------------------------------------------

export interface ConfirmPlanResult {
  grant: Grant | null;
  challengeId: string | null;
  challenge: AuthChallenge | null;
}

export const planApi = {
  /**
   * 确认计划范围。YELLOW 返回 grant；RED 返回 challenge（不返回 grant）。
   * 服务端核验 scope_hash 与当前修订一致。
   */
  async confirm(
    planId: string,
    input: { scopeHash: string; expectedRevision: number; idempotencyKey?: string | null; acknowledgedDiff?: string[] },
  ): Promise<ApiResult<ConfirmPlanResult>> {
    const result = await post<Record<string, unknown>>(
      `${V1}/plans/${encodeURIComponent(planId)}/confirm`,
      {
        scope_hash: input.scopeHash,
        ...(input.acknowledgedDiff && input.acknowledgedDiff.length > 0
          ? { acknowledged_diff: input.acknowledgedDiff }
          : {}),
      },
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "idempotency_key",
        idempotencyKey: input.idempotencyKey ?? null,
      },
    );
    return mapResult(result, (raw) => {
      const data = (raw ?? {}) as Record<string, unknown>;
      const challengeId = typeof data.challenge_id === "string" ? (data.challenge_id as string) : null;
      const grant = typeof data.grant_id === "string" ? (data as unknown as Grant) : null;
      const challenge = data.challenge && typeof data.challenge === "object" ? (data.challenge as AuthChallenge) : null;
      return { grant, challengeId, challenge } satisfies ConfirmPlanResult;
    });
  },
};

export const challengeApi = {
  /** 验证码走独立请求：不进入对话、模型上下文或前端日志（AUTH-05）。 */
  verify(challengeId: string, factorResponse: string): Promise<ApiResult<Grant>> {
    return post<Grant>(`${V1}/auth/challenges/${encodeURIComponent(challengeId)}/verify`, {
      factor_response: factorResponse,
    });
  },
};

export const grantApi = {
  revoke(
    grantId: string,
    input: { expectedRevision: number; operationId?: string | null; reason?: string },
  ): Promise<ApiResult<Grant>> {
    return post<Grant>(
      `${V1}/grants/${encodeURIComponent(grantId)}/revoke`,
      input.reason ? { reason: input.reason } : {},
      {
        expectedRevision: input.expectedRevision,
        requireExpectedRevision: true,
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },
};

// ---------------------------------------------------------------------------
// 操作与回执（权威查询）
// ---------------------------------------------------------------------------

export const operationApi = {
  get(operationId: string, options: { signal?: AbortSignal } = {}): Promise<ApiResult<OperationView>> {
    return get<OperationView>(`${V1}/operations/${encodeURIComponent(operationId)}`, options);
  },

  async list(query: { task_id?: string; limit?: number } = {}): Promise<ApiResult<OperationView[]>> {
    const result = await get<unknown>(`${V1}/operations`, { query });
    return mapResult(result, (data) => asList<OperationView>(data, "operations"));
  },

  receipt(receiptId: string): Promise<ApiResult<Receipt>> {
    return get<Receipt>(`${V1}/receipts/${encodeURIComponent(receiptId)}`);
  },
};

// ---------------------------------------------------------------------------
// 场景只读接口
// ---------------------------------------------------------------------------

export const domainApi = {
  async cards(): Promise<ApiResult<Card[]>> {
    const result = await get<unknown>(`${V1}/cards`);
    return mapResult(result, (data) => asList<Card>(data, "cards"));
  },

  billReport(
    query: {
      kind?: "month" | "year" | "custom";
      year?: number;
      month?: number;
      start_date?: string;
      end_date?: string;
    } = {},
  ): Promise<ApiResult<BillReport>> {
    return get<BillReport>(`${V1}/bills/report`, { query });
  },

  async wealthProducts(query: { product_type?: string } = {}): Promise<ApiResult<WealthProduct[]>> {
    const result = await get<unknown>(`${V1}/wealth/products`, { query });
    return mapResult(result, (data) => asList<WealthProduct>(data, "products"));
  },

  async wealthComparison(query: { budget_minor: number; horizon_days: number; preference: string }): Promise<ApiResult<WealthProduct[]>> {
    const result = await get<unknown>(`${V1}/wealth/comparison`, { query });
    return mapResult(result, (data) => asList<WealthProduct>(data, "items"));
  },

  async confirmedSubscriptions(operationId: string): Promise<ApiResult<Subscription[]>> {
    const result = await post<unknown>(`${V1}/subscriptions/query`, {confirmed: true, operation_id: operationId});
    return mapResult(result, (data) => asList<Subscription>(data, "items"));
  },

  async subscriptions(): Promise<ApiResult<Subscription[]>> {
    const result = await get<unknown>(`${V1}/subscriptions`);
    return mapResult(result, (data) => asList<Subscription>(data, "subscriptions"));
  },

  tools(): Promise<ApiResult<{ tools: ToolCatalogEntry[] }>> {
    return get<{ tools: ToolCatalogEntry[] }>(`${V1}/meta/tools`);
  },
};

// ---------------------------------------------------------------------------
// 隐私（SEC-04）
// ---------------------------------------------------------------------------

export const privacyApi = {
  exportData(): Promise<ApiResult<PrivacyExportView>> {
    return get<PrivacyExportView>(`${V1}/privacy/export`);
  },

  requestDelete(input: {
    scope: string;
    reason?: string;
    expectedRevision?: number;
    operationId?: string | null;
  }): Promise<ApiResult<PrivacyDeleteView>> {
    return post<PrivacyDeleteView>(
      `${V1}/privacy/delete`,
      { scope: input.scope, ...(input.reason ? { reason: input.reason } : {}) },
      {
        expectedRevision: input.expectedRevision ?? null,
        requireExpectedRevision: typeof input.expectedRevision === "number",
        idempotencyField: "operation_id",
        idempotencyKey: input.operationId ?? null,
      },
    );
  },
};

// ---------------------------------------------------------------------------
// 运营接管（独立角色；客户端只读 + 申请）
// ---------------------------------------------------------------------------

export const operatorApi = {
  cases(query: { status?: string } = {}): Promise<ApiResult<unknown>> {
    return get(`/operator/cases`, { query });
  },

  caseDetail(caseId: string): Promise<ApiResult<unknown>> {
    return get(`/operator/cases/${encodeURIComponent(caseId)}`);
  },

  action(caseId: string, payload: Record<string, unknown>): Promise<ApiResult<unknown>> {
    return post(`/operator/cases/${encodeURIComponent(caseId)}/actions`, payload);
  },
};

// ---------------------------------------------------------------------------
// 未在 OpenAPI 登记但界面需要的接口：集中在此；缺省时界面显示真实错误，不伪造结果。
// ---------------------------------------------------------------------------

export const experimentalApi = {
  /** 账户快照（INTERFACES.md §2 定义的数据结构；HTTP 路径待 B 确认）。 */
  snapshot(accountIds?: string[]): Promise<ApiResult<Snapshot>> {
    return get<Snapshot>(`${V1}/accounts/snapshot`, {
      query: accountIds && accountIds.length > 0 ? { account_ids: accountIds.join(",") } : {},
    });
  },

  async aaRequests(query: { task_id?: string } = {}): Promise<ApiResult<AARequest[]>> {
    const result = await request<unknown>(`${V1}/aa/requests`, { method: "GET", query });
    return mapResult(result, (data) => asList<AARequest>(data, "requests"));
  },

  riskProfile(): Promise<ApiResult<RiskProfile>> {
    return get<RiskProfile>(`${V1}/wealth/assessment`);
  },
};

/** 低层逃生口：契约新增接口时不必改本文件（仍走同一信封与写操作规则）。 */
export function rawRequest<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
  return request<T>(path, options);
}