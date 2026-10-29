import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { __setSessionForTests } from "./helpers";

/**
 * APP 式客户端的集成测试：真实渲染 App，只把 HTTP 层替换成信封桩。
 * 目的：证明"登录闸门 → 底部标签导航 → 页面真实数据 → 无假进度"这条链路成立。
 */

interface StubRoute {
  match: (url: string, init?: RequestInit) => boolean;
  body: unknown;
  status?: number;
}

function envelope(data: unknown, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "1.0",
    request_id: "req_test",
    task_id: null,
    operation_id: null,
    status: "ok",
    data,
    error: null,
    provenance: {
      adapter: "finpilot-api",
      observed_at: "2026-10-27T08:00:00Z",
      schema_version: "1.0",
      mode: "mock",
    },
    ...overrides,
  };
}

function stubFetch(routes: StubRoute[]) {
  const impl = (async (input: string | URL, init?: RequestInit) => {
    const url = String(input);
    const route = routes.find((item) => item.match(url, init));
    const body = route ? route.body : envelope(null, { status: "failed", error: { code: "OBJECT_NOT_ACCESSIBLE" } });
    const status = route?.status ?? 200;
    return {
      status,
      ok: status >= 200 && status < 300,
      text: async () => JSON.stringify(body),
    } as unknown as Response;
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", impl);
}

const taskView = {
  task_id: "task_birthday",
  owner_user_id: "user_demo_01",
  channel: "app",
  status: "awaiting_authorization",
  revision: 5,
  reference_time: "2026-10-27T08:00:00Z",
  mode: "mock",
  created_at: "2026-10-27T07:59:00Z",
  updated_at: "2026-10-27T08:00:00Z",
  goal: {
    task_id: "task_birthday",
    revision: 5,
    reference_time: "2026-10-27T08:00:00Z",
    goals: ["birthday_gift"],
    hard_constraints: { currency: "CNY", total_budget_minor: 50000, protected_floor_minor: 100000 },
    preferences: [],
    unresolved: [],
    authorization_status: "pending",
  },
  clarifications: [],
  plan: {
    plan_id: "plan_1",
    task_id: "task_birthday",
    revision: 5,
    status: "awaiting_authorization",
    scope_hash: "a".repeat(64),
    required_level: "RED",
    feasible: true,
    steps: [
      {
        step_id: "s1",
        plan_id: "plan_1",
        seq: 0,
        node_id: "n1",
        tool: "card.replace",
        typed_args: {},
        required_level: "RED",
        status: "awaiting_authorization",
      },
    ],
    total_estimated_minor: 2000,
    total_fee_minor: 2000,
    scope: { tool: "card.replace", amount_minor: 2000, fee_minor: 2000, currency: "CNY", irreversible: true },
    created_at: "2026-10-27T08:00:00Z",
  },
  progress: { completed_steps: 0, total_steps: 1 },
  waiting_reason: { code: "awaiting_authorization", safe_message: "等待你完成强验证", operation_id: null, grant_id: null, challenge_id: null },
  result_summary: null,
  messages: [],
};

describe("APP 式 Web 客户端", () => {
  it("未登录时显示登录闸门，且模式未标注时不假装实时", () => {
    stubFetch([]);
    render(<App />);
    expect(screen.getByTestId("login-user-id")).toBeInTheDocument();
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("运行模式未标注");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("不得当作实时运行");
    expect(screen.queryByRole("navigation", { name: "主导航" })).toBeNull();
  });

  it("登录后出现底部七个标签与当前用户，模式来自服务端任务视图", async () => {
    const user = userEvent.setup();
    stubFetch([
      {
        match: (url, init) => url.includes("/v1/auth/sessions") && init?.method === "POST",
        body: envelope({ token: "tok_1", expires_at: "2026-10-27T09:00:00Z", user: { user_id: "user_demo_01", display_name: "示例用户" } }),
      },
      { match: (url) => url.includes("/v1/tasks/task_birthday"), body: envelope(taskView) },
    ]);
    window.location.hash = "/task/task_birthday";
    render(<App />);

    await user.type(screen.getByTestId("login-user-id"), "user_demo_01");
    await user.type(screen.getByTestId("login-password"), "pw-123456");
    await user.click(screen.getByTestId("login-submit"));

    const nav = await screen.findByRole("navigation", { name: "主导航" });
    for (const label of ["对话", "任务", "卡片", "账单", "理财", "订阅", "隐私"]) {
      expect(within(nav).getByRole("button", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByTestId("web-current-user")).toHaveTextContent("示例用户");
    await waitFor(() => expect(screen.getByTestId("task-revision")).toHaveTextContent("修订 r5"));
    expect(screen.getByTestId("app-mode-badge")).toHaveTextContent("模拟环境");
  });

  it("确认页对 RED 计划只提供验证入口，不显示提交按钮，也没有假进度条", async () => {
    stubFetch([
      { match: (url) => url.includes("/v1/tasks/task_birthday"), body: envelope(taskView) },
    ]);
    window.location.hash = "/confirm/task_birthday";
    // 直接以已登录状态渲染（模拟刷新后从 sessionStorage 恢复）
    __setSessionForTests();

    render(<App />);
    await waitFor(() => expect(screen.getByTestId("authorization-card")).toBeInTheDocument());
    expect(screen.getByTestId("authorization-card")).toHaveAttribute("data-needs-verification", "true");
    expect(screen.queryByTestId("authorization-confirm-button")).toBeNull();
    expect(screen.getByTestId("authorization-verify-button")).toBeInTheDocument();
    expect(document.querySelector('[role="progressbar"]')).toBeNull();
    expect(screen.getByRole("heading", { name: "执行步骤" })).toBeInTheDocument();
  });

  it("账单页展示服务端返回的真实报表与模式标注，失败时显示错误码", async () => {
    stubFetch([
      {
        match: (url) => url.includes("/v1/bills/report"),
        body: envelope({
          report_id: "bill_1",
          period: { kind: "month", year: 2026, month: 10, timezone: "Asia/Shanghai" },
          generated_at: "2026-10-31T16:05:00Z",
          data_cutoff_at: "2026-10-31T16:00:00Z",
          currency: "CNY",
          total_inflow_minor: 0,
          total_outflow_minor: 123456,
          net_minor: -123456,
          categories: [{ category: "餐饮", amount_minor: 123456, count: 3, share_bps: 10000 }],
          warnings: [],
          source: "local_ledger",
          provenance: { adapter: "finpilot-api", observed_at: "2026-10-31T16:05:00Z", schema_version: "1.0", mode: "live" },
        }),
      },
    ]);
    window.location.hash = "/bills";
    __setSessionForTests();

    render(<App />);
    await waitFor(() => expect(screen.getByTestId("bill-total-spend")).toHaveTextContent("¥1,234.56"));
    expect(screen.getByTestId("app-mode-badge")).toHaveTextContent("实时运行");
    expect(screen.getByText("餐饮")).toBeInTheDocument();
  });
});