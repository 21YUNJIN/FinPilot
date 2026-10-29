import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { __setSessionForTests } from "./setup";

/**
 * IM 客户端集成测试：只替换 HTTP 层，验证
 * 会话列表 + 消息流 + 气泡内嵌授权卡片 + RED 只给验证入口 + 回执气泡。
 */

function envelope(data: unknown, overrides: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    schema_version: "1.0",
    request_id: "req_im",
    task_id: null,
    operation_id: null,
    status: "ok",
    data,
    error: null,
    provenance: { adapter: "finpilot-api", observed_at: "2026-10-27T08:00:00Z", schema_version: "1.0", mode: "mock" },
    ...overrides,
  };
}

const taskView = {
  task_id: "task_sub",
  owner_user_id: "user_demo_01",
  channel: "im",
  status: "awaiting_authorization",
  revision: 3,
  reference_time: "2026-10-27T08:00:00Z",
  mode: "mock",
  created_at: "2026-10-27T07:59:00Z",
  updated_at: "2026-10-27T08:00:00Z",
  goal: {
    task_id: "task_sub",
    revision: 3,
    reference_time: "2026-10-27T08:00:00Z",
    goals: ["cancel_subscription"],
    hard_constraints: { currency: "CNY", total_budget_minor: 0, protected_floor_minor: 0 },
    preferences: [],
    unresolved: [],
    authorization_status: "pending",
  },
  clarifications: [],
  messages: [
    { message_id: "m1", role: "user", text: "帮我取消云存储订阅", created_at: "2026-10-27T07:59:00Z", task_revision: 1 },
    {
      message_id: "m2",
      role: "assistant",
      text: "可以取消合同；代扣授权需要单独撤销，属于红色等级。",
      created_at: "2026-10-27T08:00:00Z",
      task_revision: 3,
      operation_id: "op_rev_1",
    },
  ],
  plan: {
    plan_id: "plan_9",
    task_id: "task_sub",
    revision: 3,
    status: "awaiting_authorization",
    scope_hash: "b".repeat(64),
    required_level: "RED",
    feasible: true,
    steps: [
      {
        step_id: "s1",
        plan_id: "plan_9",
        seq: 0,
        node_id: "n1",
        tool: "mandate.revoke",
        typed_args: {},
        required_level: "RED",
        status: "awaiting_authorization",
      },
    ],
    total_estimated_minor: 0,
    total_fee_minor: 0,
    scope: { tool: "mandate.revoke", amount_minor: 0, currency: "CNY", irreversible: true },
    created_at: "2026-10-27T08:00:00Z",
  },
  progress: { completed_steps: 0, total_steps: 1 },
  waiting_reason: { code: "awaiting_authorization", safe_message: "等待你完成强验证", operation_id: null, grant_id: null, challenge_id: null },
  result_summary: null,
};

const operation = {
  operation_id: "op_rev_1",
  tool: "mandate.revoke",
  status: "unknown",
  required_level: "RED",
  amount_minor: 0,
  fee_minor: 0,
  currency: "CNY",
  version: 2,
  unknown_since: "2026-10-27T08:01:00Z",
  created_at: "2026-10-27T08:00:00Z",
  updated_at: "2026-10-27T08:01:00Z",
};

function stubFetch(): void {
  const impl = (async (input: string | URL) => {
    const url = String(input);
    const body = url.includes("/v1/operations/op_rev_1")
      ? envelope(operation)
      : url.includes("/v1/tasks/task_sub")
        ? envelope(taskView)
        : url.includes("/v1/tasks")
          ? envelope({ tasks: [taskView] })
          : envelope(null, { status: "failed", error: { code: "OBJECT_NOT_ACCESSIBLE" } });
    return { status: 200, ok: true, text: async () => JSON.stringify(body) } as unknown as Response;
  }) as unknown as typeof fetch;
  vi.stubGlobal("fetch", impl);
}

describe("IM 风格客户端", () => {
  it("未登录时显示登录闸门（与 APP 客户端同一账号体系）", () => {
    stubFetch();
    render(<App />);
    expect(screen.getByTestId("login-user-id")).toBeInTheDocument();
    expect(screen.getByText(/两个客户端连接同一后端/)).toBeInTheDocument();
  });

  it("登录后展示会话列表与消息流，气泡内嵌计划与授权卡片", async () => {
    stubFetch();
    __setSessionForTests();
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("im-conversation-list")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("im-message-m1")).toHaveTextContent("帮我取消云存储订阅"));
    expect(screen.getByTestId("im-message-m2")).toHaveTextContent("代扣授权需要单独撤销");

    const planBubble = screen.getByTestId("im-plan-bubble");
    expect(planBubble).toHaveTextContent("mandate.revoke");
    expect(screen.getByTestId("im-task-status")).toHaveTextContent("等待授权");
    expect(screen.getByTestId("im-mode-badge")).toHaveTextContent("模拟环境");
  });

  it("RED 计划在气泡内只提供验证入口，不出现提交按钮或假进度", async () => {
    stubFetch();
    __setSessionForTests();
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("authorization-card")).toBeInTheDocument());
    expect(screen.getByTestId("authorization-card")).toHaveAttribute("data-needs-verification", "true");
    expect(screen.queryByTestId("authorization-confirm-button")).toBeNull();
    expect(screen.getByTestId("authorization-verify-button")).toBeInTheDocument();
    expect(document.querySelector('[role="progressbar"]')).toBeNull();
  });

  it("点击消息里的操作编号会查询权威回执；未知状态给出查询原订单说明", async () => {
    stubFetch();
    __setSessionForTests();
    const user = userEvent.setup();
    render(<App />);

    await waitFor(() => expect(screen.getByRole("button", { name: /查看回执（op_rev_1）/ })).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /查看回执（op_rev_1）/ }));

    await waitFor(() => expect(screen.getByTestId("im-receipt-bubble")).toBeInTheDocument());
    expect(screen.getByTestId("receipt-outcome-chip")).toHaveTextContent("结果未知");
    expect(screen.getByTestId("receipt-unknown-notice")).toHaveTextContent("原操作编号");
  });

  it("发送消息按钮在空输入时禁用，且未选择会话时不可用", async () => {
    stubFetch();
    __setSessionForTests();
    render(<App />);

    await waitFor(() => expect(screen.getByTestId("im-composer")).toBeInTheDocument());
    expect(screen.getByTestId("im-send")).toBeDisabled();
  });
});