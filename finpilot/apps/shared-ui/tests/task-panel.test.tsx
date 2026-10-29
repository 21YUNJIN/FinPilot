import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { TaskPanel } from "../src/components/TaskPanel";
import type { TaskView } from "../src/types";

const task: TaskView = {
  task_id: "task_birthday",
  owner_user_id: "user_demo_01",
  channel: "app",
  status: "awaiting_authorization",
  revision: 5,
  reference_time: "2026-10-27T08:00:00Z",
  mode: "mock",
  created_at: "2026-10-27T07:59:00Z",
  updated_at: "2026-10-27T08:00:00Z",
  messages: [
    { message_id: "m1", role: "user", text: "帮我在后天给妈妈订一束花", created_at: "2026-10-27T07:59:00Z", task_revision: 1 },
    {
      message_id: "m2",
      role: "assistant",
      text: "已生成计划，需要你确认黄色等级操作。",
      created_at: "2026-10-27T08:00:00Z",
      task_revision: 5,
    },
  ],
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
    scope_hash: "f".repeat(64),
    required_level: "YELLOW",
    feasible: true,
    steps: [
      { step_id: "s1", plan_id: "plan_1", seq: 0, node_id: "n1", tool: "merchant.quote", typed_args: {}, required_level: "GREEN", status: "succeeded" },
      {
        step_id: "s2",
        plan_id: "plan_1",
        seq: 1,
        node_id: "n2",
        tool: "merchant.book",
        typed_args: {},
        required_level: "YELLOW",
        status: "awaiting_authorization",
      },
    ],
    total_estimated_minor: 30000,
    total_fee_minor: 0,
    scope: {
      tool: "merchant.book",
      amount_minor: 30000,
      fee_minor: 0,
      currency: "CNY",
      counterparty: { name: "花店" },
      irreversible: false,
    },
    created_at: "2026-10-27T08:00:00Z",
  },
  progress: { completed_steps: 1, total_steps: 2, pending_effects: ["下单鲜花"] },
  waiting_reason: {
    code: "awaiting_authorization",
    safe_message: "等待你确认黄色等级操作",
    operation_id: null,
    grant_id: null,
    challenge_id: null,
    since: "2026-10-27T08:00:00Z",
  },
  result_summary: null,
};

describe("TaskPanel 任务面板（契约 task_view.json）", () => {
  it("同时呈现对话、目标摘要、计划步骤与等待原因", () => {
    render(<TaskPanel task={task} />);
    expect(screen.getByText("帮我在后天给妈妈订一束花")).toBeInTheDocument();
    expect(screen.getByText("已生成计划，需要你确认黄色等级操作。")).toBeInTheDocument();
    expect(screen.getByTestId("task-status-chip")).toHaveTextContent("等待授权");
    expect(screen.getByTestId("task-revision")).toHaveTextContent("修订 r5");
    expect(screen.getByTestId("task-waiting-reason")).toHaveTextContent("等待原因：等待你确认黄色等级操作");
    expect(screen.getByTestId("task-waiting-reason")).toHaveTextContent("原因码：awaiting_authorization");
    expect(screen.getByTestId("task-waiting-reason")).toHaveTextContent("等待不是成功");
    expect(screen.getByTestId("goal-summary")).toBeInTheDocument();
    expect(screen.getByTestId("plan-steps")).toBeInTheDocument();
    expect(screen.getByTestId("task-progress-text")).toHaveTextContent("已完成 1 / 共 2 步");
    expect(screen.getByTestId("task-progress-text")).toHaveTextContent("待完成效果：下单鲜花");
  });

  it("模式如实标注（mock 不得当作实时运行）", () => {
    render(<TaskPanel task={task} />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("模拟环境");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("非真实资金");
    expect(screen.getByText("渠道：app")).toBeInTheDocument();
  });

  it("计划需要确认时渲染授权卡片；YELLOW 提供确认入口并回传计划", async () => {
    const user = userEvent.setup();
    const onConfirmPlan = vi.fn();
    render(<TaskPanel task={task} onConfirmPlan={onConfirmPlan} />);

    expect(screen.getByTestId("authorization-card")).toHaveAttribute("data-level", "YELLOW");
    expect(screen.getByTestId("authorization-scope-hash")).toHaveTextContent("f".repeat(64));
    await user.click(screen.getByTestId("authorization-confirm-button"));
    expect(onConfirmPlan.mock.calls[0]?.[0]).toMatchObject({ plan_id: "plan_1" });
  });

  it("RED 计划在未验证时只提供验证入口（不显示提交按钮）", () => {
    render(
      <TaskPanel
        task={{
          ...task,
          plan: { ...(task.plan as NonNullable<TaskView["plan"]>), required_level: "RED" },
        }}
        onConfirmPlan={vi.fn()}
        onVerifyPlan={vi.fn()}
      />,
    );
    expect(screen.getByTestId("authorization-card")).toHaveAttribute("data-needs-verification", "true");
    expect(screen.queryByTestId("authorization-confirm-button")).toBeNull();
    expect(screen.getByTestId("authorization-verify-button")).toBeInTheDocument();
  });

  it("追加指令：空输入禁用发送，Ctrl+Enter 也能发送", async () => {
    const user = userEvent.setup();
    const onSendMessage = vi.fn();
    render(<TaskPanel task={task} onSendMessage={onSendMessage} />);

    const send = screen.getByTestId("task-send-button");
    expect(send).toBeDisabled();
    await user.type(screen.getByTestId("task-composer"), "预算改成 300");
    expect(send).toBeEnabled();
    await user.keyboard("{Control>}{Enter}{/Control}");
    expect(onSendMessage).toHaveBeenCalledWith("预算改成 300");
  });

  it("结束态任务不再允许追加指令，并提示新建任务", () => {
    render(<TaskPanel task={{ ...task, status: "succeeded" }} onSendMessage={vi.fn()} />);
    expect(screen.getByTestId("task-composer")).toBeDisabled();
    expect(screen.getByTestId("task-composer")).toHaveAttribute("placeholder", "任务已结束：如需继续请新建任务");
  });

  it("结果摘要显示已完成/待完成/未知/失败四类，未知不得写成完成", () => {
    render(
      <TaskPanel
        task={{
          ...task,
          status: "partially_succeeded",
          spent_minor: 30000,
          result_summary: {
            task_id: "task_birthday",
            task_status: "partially_succeeded",
            revision: 9,
            mode: "mock",
            generated_at: "2026-10-27T08:10:00Z",
            completed_effects: [
              { kind: "merchant_order", description: "已下单鲜花", verified: true, authority: "receipt", operation_id: "op_1", amount_minor: 30000 },
            ],
            pending_effects: ["蛋糕缺货，等待改选"],
            unknown_effects: ["补卡结果未知"],
            failed_effects: ["餐厅预订被拒绝"],
            next_actions: [{ action_id: "act_1", label: "查询原订单", kind: "retry_same_operation" }],
            total_spent_minor: 30000,
            total_fee_minor: 0,
          },
        }}
      />,
    );
    const result = screen.getByTestId("task-result");
    expect(within(result).getByTestId("task-result-completed")).toHaveTextContent("已下单鲜花");
    expect(within(result).getByTestId("task-result-completed")).toHaveTextContent("¥300.00");
    expect(within(result).getByTestId("task-result-pending")).toHaveTextContent("蛋糕缺货");
    expect(within(result).getByTestId("task-result-unknown")).toHaveTextContent("补卡结果未知");
    expect(within(result).getByTestId("task-result-failed")).toHaveTextContent("餐厅预订被拒绝");
    expect(within(result).getByText(/不得当作成功或失败/)).toBeInTheDocument();
    expect(within(result).getByTestId("task-result-next-actions")).toHaveTextContent("查询原订单");
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("版本冲突时展示冲突提示并保留已确认动作", () => {
    render(
      <TaskPanel
        task={task}
        conflict={{
          code: "VERSION_CONFLICT",
          message: "对象已被其他操作更新，请刷新后重试。",
          expectedRevision: 5,
          serverRevision: 7,
          detectedAt: "2026-10-27T08:00:10Z",
        }}
        pendingActions={[
          { actionId: "act_1", kind: "confirm_plan", label: "已确认计划 r5", at: "2026-10-27T08:00:05Z", expectedRevision: 5 },
        ]}
        onRefresh={vi.fn()}
      />,
    );
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("你基于修订 r5");
    expect(screen.getByTestId("version-conflict-pending-actions")).toHaveTextContent("已确认计划 r5");
  });

  it("未选择任务时显示空态而不是伪造内容", () => {
    render(<TaskPanel task={null} loading />);
    expect(screen.getByText("正在读取任务状态…")).toBeInTheDocument();
    expect(screen.queryByTestId("plan-steps")).toBeNull();
  });
});