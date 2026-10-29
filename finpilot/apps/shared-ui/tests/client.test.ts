import { describe, expect, it, vi } from "vitest";
import {
  apiBaseUrl,
  configureClient,
  DEFAULT_API_BASE_URL,
  get,
  isSuccessEnvelope,
  modeFromEnvelope,
  post,
  request,
  resetClientConfig,
  type ApiResult,
} from "../src/api/client";
import { resourceCache } from "../src/api/cache";
import { getAuthToken, setSession } from "../src/api/session";
import type { Envelope } from "../src/types";

interface FetchCall {
  url: string;
  init: RequestInit;
}

function makeFetch(handler: (call: FetchCall) => { status: number; body: unknown } | "network-error") {
  const calls: FetchCall[] = [];
  const impl = (async (url: string | URL, init?: RequestInit) => {
    const call: FetchCall = { url: String(url), init: init ?? {} };
    calls.push(call);
    const result = handler(call);
    if (result === "network-error") throw new TypeError("Failed to fetch");
    return {
      status: result.status,
      ok: result.status >= 200 && result.status < 300,
      text: async () => (typeof result.body === "string" ? result.body : JSON.stringify(result.body)),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  return { impl, calls };
}

function envelope(overrides: Partial<Envelope<unknown>> = {}): Envelope<unknown> {
  return {
    schema_version: "1.0",
    request_id: "req_test_1",
    task_id: "task_1",
    operation_id: "op_server_1",
    status: "ok",
    data: { ok: true },
    error: null,
    provenance: {
      adapter: "finpilot-api",
      observed_at: "2026-10-27T08:00:00+00:00",
      schema_version: "1.0",
      mode: "mock",
    },
    ...overrides,
  };
}

describe("HTTP 客户端（统一信封）", () => {
  it("base URL 默认取 http://127.0.0.1:58000，可由 VITE_API_BASE_URL 覆盖", () => {
    expect(DEFAULT_API_BASE_URL).toBe("http://127.0.0.1:58000");
    expect(["http://127.0.0.1:58000", undefined]).toContain(
      import.meta.env?.VITE_API_BASE_URL ?? "http://127.0.0.1:58000",
    );
    vi.stubEnv("VITE_API_BASE_URL", "http://api.test:9000/");
    expect(apiBaseUrl()).toBe("http://api.test:9000");
    vi.unstubAllEnvs();
    resetClientConfig();
  });

  it("HTTP 200 但 status=failed 必须当作失败（HTTP 200 不等于成功）", async () => {
    const { impl } = makeFetch(() => ({
      status: 200,
      body: envelope({
        status: "failed",
        data: null,
        error: { code: "PROVIDER_UNAVAILABLE", safe_message: "业务提供方暂时不可用，未产生资金效果。" },
      }),
    }));
    configureClient({ fetchImpl: impl });

    const result = await get<{ ok: boolean }>("/v1/tasks/task_1");
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.code).toBe("PROVIDER_UNAVAILABLE");
    expect(result.error.message).toBe("业务提供方暂时不可用，未产生资金效果。");
    expect(result.error.httpStatus).toBe(200);
    expect(result.error.status).toBe("failed");
    expect(result.error.transport).toBe(false);
  });

  it("HTTP 200 但 status=unknown 是未知而非失败，并要求查询原订单", async () => {
    const { impl } = makeFetch(() => ({
      status: 200,
      body: envelope({
        status: "unknown",
        data: null,
        error: { code: "RESULT_UNKNOWN", safe_message: "结果未知，正在用原操作编号核对，请勿重复提交。" },
      }),
    }));
    configureClient({ fetchImpl: impl });

    const result = await get("/v1/operations/op_1");
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.status).toBe("unknown");
    expect(result.error.requiresQueryOfOriginalOperation).toBe(true);
    expect(result.error.hint).toContain("原操作编号");
  });

  it("status=ok 但带 error 也按失败处理（与 envelope.is_success 一致）", async () => {
    const { impl } = makeFetch(() => ({
      status: 200,
      body: envelope({ status: "ok", error: { code: "SYSTEM_ERROR" } }),
    }));
    configureClient({ fetchImpl: impl });
    const result = await get("/v1/tasks/task_1");
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.code).toBe("SYSTEM_ERROR");
  });

  it("合法信封成功时返回 data 与 provenance（模式如实读取，缺失不默认 live）", async () => {
    const { impl } = makeFetch(() => ({ status: 200, body: envelope({ data: { task_id: "task_1" } }) }));
    configureClient({ fetchImpl: impl });

    const result = await get<{ task_id: string }>("/v1/tasks/task_1");
    expect(result.ok).toBe(true);
    if (!result.ok) throw new Error("unreachable");
    expect(result.data.task_id).toBe("task_1");
    expect(isSuccessEnvelope(result.envelope)).toBe(true);
    expect(modeFromEnvelope(result.envelope)).toBe("mock");
    expect(modeFromEnvelope(envelope({ provenance: { adapter: "x", observed_at: "t", schema_version: "1.0" } }))).toBeNull();
  });

  it("写操作按契约把幂等键写入指定字段，并带 expected_revision", async () => {
    const { impl, calls } = makeFetch(() => ({ status: 200, body: envelope({ data: { task_id: "task_1" } }) }));
    configureClient({ fetchImpl: impl });

    const result = await post(
      "/v1/tasks/task_1/messages",
      { message: "改成 300 元" },
      { expectedRevision: 7, idempotencyField: "operation_id" },
    );
    expect(result.ok).toBe(true);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.init.method).toBe("POST");
    const body = JSON.parse(String(calls[0]?.init.body)) as Record<string, unknown>;
    expect(body.message).toBe("改成 300 元");
    expect(body.expected_revision).toBe(7);
    expect(String(body.operation_id)).toMatch(/^op_[a-z0-9]+$/);
    expect(body.idempotency_key).toBeUndefined();
    expect(calls[0]?.url).toBe("http://127.0.0.1:58000/v1/tasks/task_1/messages");
  });

  it("创建任务用 client_message_id，不塞 operation_id（接口 additionalProperties=false）", async () => {
    const { impl, calls } = makeFetch(() => ({ status: 200, body: envelope() }));
    configureClient({ fetchImpl: impl });

    await post("/v1/tasks", { message: "给妈妈买礼物", channel: "app" }, { idempotencyField: "client_message_id" });
    const body = JSON.parse(String(calls[0]?.init.body)) as Record<string, unknown>;
    expect(String(body.client_message_id)).toMatch(/^op_[a-z0-9]+$/);
    expect(body.operation_id).toBeUndefined();
    expect(body.expected_revision).toBeUndefined();
  });

  it("确认计划用 idempotency_key；挑战校验不带任何幂等键", async () => {
    const { impl, calls } = makeFetch(() => ({ status: 200, body: envelope() }));
    configureClient({ fetchImpl: impl });

    await post(
      "/v1/plans/plan_1/confirm",
      { scope_hash: "a".repeat(64) },
      { expectedRevision: 5, requireExpectedRevision: true, idempotencyField: "idempotency_key" },
    );
    const confirmBody = JSON.parse(String(calls[0]?.init.body)) as Record<string, unknown>;
    expect(confirmBody.scope_hash).toBe("a".repeat(64));
    expect(confirmBody.expected_revision).toBe(5);
    expect(String(confirmBody.idempotency_key)).toMatch(/^op_[a-z0-9]+$/);
    expect(confirmBody.operation_id).toBeUndefined();

    await post("/v1/auth/challenges/chal_1/verify", { factor_response: "123456" });
    const verifyBody = JSON.parse(String(calls[1]?.init.body)) as Record<string, unknown>;
    expect(Object.keys(verifyBody)).toEqual(["factor_response"]);
  });

  it("重试复用同一个幂等键，不会静默换新的键", async () => {
    const { impl, calls } = makeFetch(() => ({ status: 200, body: envelope() }));
    configureClient({ fetchImpl: impl });

    const operationId = "op_fixed_key_123";
    await post("/v1/tasks/task_1/resume", {}, { expectedRevision: 3, requireExpectedRevision: true, idempotencyField: "operation_id", idempotencyKey: operationId });
    await post("/v1/tasks/task_1/resume", {}, { expectedRevision: 3, requireExpectedRevision: true, idempotencyField: "operation_id", idempotencyKey: operationId });

    const bodies = calls.map((call) => JSON.parse(String(call.init.body)) as Record<string, unknown>);
    expect(bodies[0]?.operation_id).toBe(operationId);
    expect(bodies[1]?.operation_id).toBe(operationId);
  });

  it("缺少 expected_revision 的写操作被阻止（不发请求）", async () => {
    const { impl, calls } = makeFetch(() => ({ status: 200, body: envelope() }));
    configureClient({ fetchImpl: impl });

    const result = await request("/v1/tasks/task_1/cancel", {
      method: "POST",
      body: {},
      requireExpectedRevision: true,
    });
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.code).toBe("CLIENT_MISSING_EXPECTED_REVISION");
    expect(calls).toHaveLength(0);
  });

  it("401 清除会话缓存（token 与用户级视图缓存都清空）", async () => {
    setSession({ token: "tok_a", userId: "user_demo_01", channel: "web" });
    resourceCache.set("task:task_1", "user_demo_01", { task_id: "task_1" });
    expect(getAuthToken()).toBe("tok_a");
    expect(resourceCache.size()).toBe(1);

    const { impl, calls } = makeFetch(() => ({
      status: 401,
      body: envelope({ status: "failed", data: null, error: { code: "UNAUTHENTICATED" } }),
    }));
    configureClient({ fetchImpl: impl });

    const result = await get("/v1/tasks/task_1");
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.code).toBe("UNAUTHENTICATED");
    expect(getAuthToken()).toBeNull();
    expect(resourceCache.size()).toBe(0);
    const headers = calls[0]?.init.headers as Record<string, string>;
    expect(headers.Authorization).toBe("Bearer tok_a");
    expect(headers["X-FinPilot-Client"]).toBe("web");
  });

  it("网络异常与超时属于传输层失败，不算成功", async () => {
    configureClient({ fetchImpl: makeFetch(() => "network-error").impl });
    const networkResult: ApiResult<unknown> = await get("/v1/tasks/task_1");
    expect(networkResult.ok).toBe(false);
    if (networkResult.ok) throw new Error("unreachable");
    expect(networkResult.error.code).toBe("NETWORK_ERROR");
    expect(networkResult.error.transport).toBe(true);
    expect(networkResult.error.message).toContain("未获得业务结果");

    const hanging = (async (_url: string | URL, init?: RequestInit) =>
      await new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener("abort", () => {
          const error = new Error("aborted");
          error.name = "AbortError";
          reject(error);
        });
      })) as unknown as typeof fetch;
    configureClient({ fetchImpl: hanging, timeoutMs: 10 });
    const timeoutResult = await get("/v1/tasks/task_1");
    expect(timeoutResult.ok).toBe(false);
    if (timeoutResult.ok) throw new Error("unreachable");
    expect(timeoutResult.error.code).toBe("REQUEST_TIMEOUT");
    expect(timeoutResult.error.requiresQueryOfOriginalOperation).toBe(true);
  });

  it("非标准响应体（无信封）不得当作成功", async () => {
    const { impl } = makeFetch(() => ({ status: 200, body: "ok" }));
    configureClient({ fetchImpl: impl });
    const result = await get("/v1/tasks/task_1");
    expect(result.ok).toBe(false);
    if (result.ok) throw new Error("unreachable");
    expect(result.error.code).toBe("INVALID_ENVELOPE");

    const nonEnvelope = makeFetch(() => ({ status: 200, body: { hello: "world" } }));
    configureClient({ fetchImpl: nonEnvelope.impl });
    const result2 = await get("/v1/tasks/task_1");
    expect(result2.ok).toBe(false);
    if (result2.ok) throw new Error("unreachable");
    expect(result2.error.code).toBe("INVALID_ENVELOPE");
  });
});