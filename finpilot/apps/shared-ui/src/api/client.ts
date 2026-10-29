/**
 * 统一 HTTP 客户端（两个客户端共用的唯一入口）。
 *
 * 契约来源：docs/INTERFACES.md 第 4、5 节 + services/api/src/finpilot/envelope.py。
 *
 * 关键约定（缺一不可）：
 * 1. base URL：`import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:58000"`。
 * 2. 所有响应按统一信封解析（`status` / `data` / `error` / `provenance`）。
 * 3. **HTTP 200 但 `status !== "ok"` 一律当失败**；`status: "ok"` 但带 `error` 也当失败
 *    （与 envelope.is_success 的判定一致）。
 * 4. 写操作自动带 `expected_revision`（调用方从最新视图读取）与客户端生成的 `operation_id`；
 *    重试必须复用同一个 operation_id（幂等键），客户端不会静默换新键。
 * 5. 401 清除会话缓存（token + 用户级视图缓存），避免串户。
 * 6. 网络失败/超时属于"结果未知"，不得当作成功，提示用原操作编号查询。
 */

import type { AppMode, Envelope, EnvelopeStatus, ErrorObject } from "../types";
import { resolveUserMessage } from "./errorMessages";
import { newOperationId } from "./ids";
import { clearSessionCache, getAuthToken, getSession } from "./session";
import { traceEvent } from "./trace";

/** 默认后端地址（见 README/契约：本地 API 监听 58000）。 */
export const DEFAULT_API_BASE_URL = "http://127.0.0.1:58000";

export type HttpMethod = "GET" | "POST" | "PUT" | "PATCH" | "DELETE";

const WRITE_METHODS: readonly HttpMethod[] = ["POST", "PUT", "PATCH", "DELETE"];

export function isWriteMethod(method: HttpMethod): boolean {
  return WRITE_METHODS.includes(method);
}

/** 唯一的 base URL 解析函数；空字符串也回落到默认值，避免请求打到相对路径。 */
export function apiBaseUrl(): string {
  // 契约原文：base URL 取 import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:58000"
  const configured = import.meta.env?.VITE_API_BASE_URL ?? DEFAULT_API_BASE_URL;
  const trimmed = String(configured).trim();
  return trimmed === "" ? DEFAULT_API_BASE_URL : trimmed.replace(/\/+$/, "");
}

export interface ClientConfig {
  /** 覆盖 base URL（测试或同源代理时使用）。 */
  baseUrl: string | null;
  /** 客户端渠道标识，写入请求头，便于后端审计区分来源。 */
  channel: string;
  timeoutMs: number;
  fetchImpl: typeof fetch | null;
}

const DEFAULT_CONFIG: ClientConfig = {
  baseUrl: null,
  channel: "web",
  timeoutMs: 20_000,
  fetchImpl: null,
};

let config: ClientConfig = { ...DEFAULT_CONFIG };

export function configureClient(patch: Partial<ClientConfig>): void {
  config = { ...config, ...patch };
}

export function resetClientConfig(): void {
  config = { ...DEFAULT_CONFIG };
}

export function currentClientConfig(): Readonly<ClientConfig> {
  return config;
}

export interface ApiMeta {
  method: HttpMethod;
  path: string;
  url: string;
  httpStatus: number | null;
  status: EnvelopeStatus | null;
  requestId: string | null;
  operationId: string | null;
}

export interface ApiFailure {
  code: string;
  message: string;
  hint: string;
  retryMode: string;
  knownCode: boolean;
  requiresQueryOfOriginalOperation: boolean;
  httpStatus: number | null;
  status: EnvelopeStatus | null;
  reasonCodes: string[];
  details: Record<string, unknown> | null;
  requestId: string | null;
  operationId: string | null;
  envelope: Envelope<unknown> | null;
  /** true 表示请求没有到达业务层（网络/超时/解析失败）。 */
  transport: boolean;
}

export type ApiResult<T> =
  | { ok: true; data: T; envelope: Envelope<T>; meta: ApiMeta }
  | { ok: false; error: ApiFailure; meta: ApiMeta };

/**
 * 幂等键写入位置（OpenAPI 各写接口的 additionalProperties=false，
 * 因此必须按接口规定选择字段，不能一律塞 operation_id）：
 * - operation_id：/v1/tasks/{id}/messages、/plan、/cancel、/resume、/grants/{id}/revoke、/v1/privacy/delete
 * - idempotency_key：/v1/plans/{id}/confirm
 * - client_message_id：POST /v1/tasks（创建聚合根）
 * - null：一次性接口（登录、TOTP 注册/确认、挑战校验）不得携带幂等键
 */
export type IdempotencyField = "operation_id" | "idempotency_key" | "client_message_id";

export interface RequestOptions {
  method?: HttpMethod;
  body?: Record<string, unknown> | null;
  query?: Record<string, string | number | boolean | null | undefined>;
  /** 从最新任务/计划视图读到的修订号；写操作必须带。 */
  expectedRevision?: number | null;
  /** true 时缺少 expectedRevision 直接阻止请求（不发到后端）。 */
  requireExpectedRevision?: boolean;
  /** 客户端幂等键写入的字段；默认 null（该接口不接受幂等键）。 */
  idempotencyField?: IdempotencyField | null;
  /** 复用已有幂等键（重试同一逻辑操作时必须传入同一个值）。 */
  idempotencyKey?: string | null;
  auth?: boolean;
  signal?: AbortSignal;
  timeoutMs?: number;
  headers?: Record<string, string>;
  baseUrl?: string;
}

function metaOf(method: HttpMethod, path: string, url: string): ApiMeta {
  return {
    method,
    path,
    url,
    httpStatus: null,
    status: null,
    requestId: null,
    operationId: null,
  };
}

function failure(
  code: string,
  meta: ApiMeta,
  options: {
    message?: string | null;
    retryMode?: string | null;
    envelope?: Envelope<unknown> | null;
    httpStatus?: number | null;
    transport?: boolean;
    reasonCodes?: string[];
    details?: Record<string, unknown> | null;
    requestId?: string | null;
    operationId?: string | null;
    status?: EnvelopeStatus | null;
  } = {},
): ApiFailure {
  const resolved = resolveUserMessage({
    code,
    serverMessage: options.message ?? null,
    retryMode: options.retryMode ?? null,
  });
  return {
    code,
    message: resolved.message,
    hint: resolved.hint,
    retryMode: String(resolved.retryMode),
    knownCode: resolved.knownCode,
    requiresQueryOfOriginalOperation: resolved.requiresQueryOfOriginalOperation,
    httpStatus: options.httpStatus ?? meta.httpStatus,
    status: options.status ?? meta.status,
    reasonCodes: options.reasonCodes ?? [],
    details: options.details ?? null,
    requestId: options.requestId ?? meta.requestId,
    operationId: options.operationId ?? meta.operationId,
    envelope: options.envelope ?? null,
    transport: options.transport ?? false,
  };
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isEnvelopeLike(value: unknown): value is Envelope<unknown> {
  if (!isPlainObject(value)) return false;
  if (typeof value.status !== "string") return false;
  if (!("data" in value) && !("error" in value)) return false;
  return true;
}

function errorFromEnvelope(envelope: Envelope<unknown>): ErrorObject | null {
  const error = (envelope as { error?: unknown }).error;
  if (!isPlainObject(error)) return null;
  if (typeof error.code !== "string" || error.code.trim() === "") return null;
  return error as unknown as ErrorObject;
}

/** 从信封里如实读取运行模式；缺失返回 null（绝不默认 live）。 */
export function modeFromEnvelope(envelope: Envelope<unknown> | null | undefined): AppMode | string | null {
  const mode = envelope?.provenance?.mode;
  return typeof mode === "string" && mode.trim() !== "" ? mode : null;
}

export function isSuccessEnvelope(envelope: Envelope<unknown> | null | undefined): boolean {
  return !!envelope && envelope.status === "ok" && !envelope.error;
}

interface PreparedRequest {
  url: string;
  method: HttpMethod;
  body: Record<string, unknown> | undefined;
  headers: Record<string, string>;
  operationId: string | null;
}

function prepare(
  path: string,
  options: RequestOptions,
): { ok: true; request: PreparedRequest } | { ok: false; errorCode: string; meta: ApiMeta } {
  const method = options.method ?? (options.body ? "POST" : "GET");
  const base = options.baseUrl ?? config.baseUrl ?? apiBaseUrl();
  const url = new URL(`${base.replace(/\/+$/, "")}${path.startsWith("/") ? path : `/${path}`}`);
  for (const [key, value] of Object.entries(options.query ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  const meta = metaOf(method, path, url.toString());
  const write = isWriteMethod(method);

  let body: Record<string, unknown> | undefined;
  let operationId: string | null = null;

  if (write) {
    if (options.body !== undefined && options.body !== null && !isPlainObject(options.body)) {
      return { ok: false, errorCode: "SCHEMA_INVALID", meta };
    }
    body = { ...(options.body ?? {}) };

    const field = options.idempotencyField ?? null;
    if (field) {
      const existing = typeof body[field] === "string" ? (body[field] as string) : null;
      const key = options.idempotencyKey ?? existing ?? newOperationId();
      if (body[field] === undefined) body[field] = key;
      if (field === "operation_id") operationId = key;
    }

    const expectedRevision =
      options.expectedRevision ?? (typeof body.expected_revision === "number" ? body.expected_revision : null);
    if (options.requireExpectedRevision && (expectedRevision === null || expectedRevision === undefined)) {
      return { ok: false, errorCode: "CLIENT_MISSING_EXPECTED_REVISION", meta };
    }
    if (expectedRevision !== null && expectedRevision !== undefined && body.expected_revision === undefined) {
      body.expected_revision = expectedRevision;
    }
  }

  const headers: Record<string, string> = {
    Accept: "application/json",
    "X-FinPilot-Client": config.channel,
    ...(options.headers ?? {}),
  };
  if (write) headers["Content-Type"] = "application/json";
  if (options.auth !== false) {
    const token = getAuthToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  return { ok: true, request: { url: url.toString(), method, body, headers, operationId } };
}

/** 发送请求并解析统一信封。任何非 ok 的业务状态都返回 `{ ok: false }`。 */
export async function request<T>(path: string, options: RequestOptions = {}): Promise<ApiResult<T>> {
  const prepared = prepare(path, options);
  if (!prepared.ok) {
    const fail = failure(prepared.errorCode, prepared.meta);
    traceEvent("http.blocked", `${prepared.meta.method} ${path} ${prepared.errorCode}`);
    return { ok: false, error: fail, meta: prepared.meta };
  }

  const { url, method, body, headers, operationId } = prepared.request;
  const meta = metaOf(method, path, url);
  const timeoutMs = options.timeoutMs ?? config.timeoutMs;

  const controller = new AbortController();
  let timedOut = false;
  const onExternalAbort = (): void => controller.abort();
  if (options.signal) {
    if (options.signal.aborted) controller.abort();
    else options.signal.addEventListener("abort", onExternalAbort);
  }
  const timer =
    timeoutMs > 0
      ? setTimeout(() => {
          timedOut = true;
          controller.abort();
        }, timeoutMs)
      : null;

  const fetchImpl: typeof fetch | null =
    config.fetchImpl ?? (typeof fetch === "function" ? fetch : null);
  if (!fetchImpl) {
    if (timer) clearTimeout(timer);
    const fail = failure("NETWORK_ERROR", meta, { transport: true });
    return { ok: false, error: fail, meta };
  }

  let httpStatus: number | null = null;
  let text = "";
  try {
    traceEvent("http.request", `${method} ${path}${operationId ? ` ${operationId}` : ""}`);
    const response = await fetchImpl(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    httpStatus = response.status;
    text = await response.text();
  } catch (err) {
    if (timer) clearTimeout(timer);
    if (options.signal) options.signal.removeEventListener("abort", onExternalAbort);
    const aborted = (err as { name?: string } | null)?.name === "AbortError";
    const code = timedOut ? "REQUEST_TIMEOUT" : aborted ? "REQUEST_ABORTED" : "NETWORK_ERROR";
    const fail = failure(code, meta, {
      transport: true,
      httpStatus,
      operationId,
      status: null,
    });
    traceEvent("http.error", `${method} ${path} ${code}`);
    return { ok: false, error: fail, meta: { ...meta, httpStatus, operationId } };
  } finally {
    if (timer) clearTimeout(timer);
    if (options.signal) options.signal.removeEventListener("abort", onExternalAbort);
  }

  const resolvedMeta: ApiMeta = { ...meta, httpStatus, operationId };

  // 401：立刻清除会话与用户级缓存，避免旧 token 的视图继续渲染。
  if (httpStatus === 401) {
    clearSessionCache("http_401");
  }

  let parsed: unknown = null;
  if (text.trim() !== "") {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = null;
    }
  }

  if (!isEnvelopeLike(parsed)) {
    const code = httpStatus === 401 ? "UNAUTHENTICATED" : "INVALID_ENVELOPE";
    const fail = failure(code, resolvedMeta, {
      httpStatus,
      status: null,
      transport: true,
    });
    traceEvent("http.envelope_invalid", `${method} ${path} http=${httpStatus}`);
    return { ok: false, error: fail, meta: resolvedMeta };
  }

  const envelope = parsed as Envelope<unknown>;
  const envelopeMeta: ApiMeta = {
    ...resolvedMeta,
    status: envelope.status,
    requestId: typeof envelope.request_id === "string" ? envelope.request_id : null,
    operationId:
      typeof envelope.operation_id === "string" && envelope.operation_id !== ""
        ? envelope.operation_id
        : operationId,
  };

  // 非 2xx：一律失败，即便信封里写着 ok（HTTP 语义与业务状态冲突时以更保守的一方为准）。
  if (httpStatus < 200 || httpStatus >= 300) {
    const err = errorFromEnvelope(envelope);
    const code = httpStatus === 401 ? "UNAUTHENTICATED" : (err?.code ?? "HTTP_ERROR");
    const fail = failure(code, envelopeMeta, {
      message: err?.safe_message ?? null,
      retryMode: err?.retry_mode ?? null,
      envelope,
      httpStatus,
      status: envelope.status,
      reasonCodes: err?.reason_codes ?? [],
      details: err?.details ?? null,
      transport: false,
    });
    traceEvent("http.http_error", `${method} ${path} http=${httpStatus} code=${code}`);
    return { ok: false, error: fail, meta: envelopeMeta };
  }

  // HTTP 200：业务状态只看 status + error。
  if (envelope.status !== "ok" || envelope.error) {
    const err = errorFromEnvelope(envelope);
    const code = err?.code ?? "INVALID_ENVELOPE";
    const fail = failure(code, envelopeMeta, {
      message: err?.safe_message ?? null,
      retryMode: err?.retry_mode ?? null,
      envelope,
      httpStatus,
      status: envelope.status,
      reasonCodes: err?.reason_codes ?? [],
      details: err?.details ?? null,
      transport: false,
    });
    traceEvent("http.business_failure", `${method} ${path} status=${envelope.status} code=${code}`);
    return { ok: false, error: fail, meta: envelopeMeta };
  }

  return {
    ok: true,
    data: envelope.data as T,
    envelope: envelope as Envelope<T>,
    meta: envelopeMeta,
  };
}

export function get<T>(path: string, options: Omit<RequestOptions, "method" | "body"> = {}): Promise<ApiResult<T>> {
  return request<T>(path, { ...options, method: "GET" });
}

export function post<T>(
  path: string,
  body: Record<string, unknown> | null = null,
  options: Omit<RequestOptions, "method" | "body"> = {},
): Promise<ApiResult<T>> {
  return request<T>(path, { ...options, method: "POST", body });
}

/** 当前会话的渠道（用于登录与任务创建时的 channel 字段）。 */
export function currentChannel(): string {
  return getSession()?.channel ?? config.channel;
}

/** 抛出式封装：给事件处理器用，避免到处 if (!result.ok)。 */
export class ApiClientError extends Error {
  readonly failure: ApiFailure;

  constructor(failureBody: ApiFailure) {
    super(failureBody.message);
    this.name = "ApiClientError";
    this.failure = failureBody;
  }
}

export async function unwrap<T>(promise: Promise<ApiResult<T>>): Promise<T> {
  const result = await promise;
  if (!result.ok) throw new ApiClientError(result.error);
  return result.data;
}

/**
 * 把成功结果里的 data 换成归一化后的形状（列表接口兼容数组/包一层对象两种返回），
 * 保留 envelope 与 meta 不变；失败原样返回。
 */
export function mapResult<T, U>(result: ApiResult<T>, mapper: (data: T) => U): ApiResult<U> {
  if (!result.ok) return result;
  const data = mapper(result.data);
  return { ...result, data, envelope: { ...result.envelope, data } };
}