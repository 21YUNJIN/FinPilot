import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { PlanSteps } from "../src/components/PlanSteps";
import type { Plan, PlanStep } from "../src/types";

function step(overrides: Partial<PlanStep> & Pick<PlanStep, "step_id" | "status">): PlanStep {
  return {
    plan_id: "plan_1",
    seq: 0,
    node_id: overrides.step_id,
    tool: "account.balance",
    typed_args: {},
    required_level: "GREEN",
    ...overrides,
  };
}

const steps: PlanStep[] = [
  step({ step_id: "s1", seq: 0, status: "pending", tool: "account.balance" }),
  step({ step_id: "s2", seq: 1, status: "awaiting_authorization", tool: "card.replace", required_level: "RED" }),
  step({ step_id: "s3", seq: 2, status: "running", tool: "transfer.prepare" }),
  step({ step_id: "s4", seq: 3, status: "succeeded", tool: "aa.create", write_set: ["aa_request_created"] }),
  step({ step_id: "s5", seq: 4, status: "failed", tool: "wealth.purchase", last_error: "RISK_MISMATCH" }),
  step({ step_id: "s6", seq: 5, status: "skipped", tool: "merchant.book" }),
  step({ step_id: "s7", seq: 6, status: "cancelled", tool: "merchant.book" }),
  step({ step_id: "s8", seq: 7, status: "unknown", tool: "transfer.submit", operation_id: "op_unknown_1" }),
];

const plan: Plan = {
  plan_id: "plan_1",
  task_id: "task_1",
  revision: 5,
  status: "awaiting_authorization",
  scope_hash: "a".repeat(64),
  required_level: "RED",
  feasible: true,
  steps,
  total_estimated_minor: 123456,
  total_fee_minor: 2000,
  created_at: "2026-10-27T08:00:00Z",
};

describe("PlanSteps 计划步骤（契约 plan_step.json）", () => {
  it("逐步展示真实状态：未开始/待确认/执行中/已完成/失败/已停止/结果未知", () => {
    render(<PlanSteps plan={plan} />);

    expect(screen.getByTestId("plan-step-status-s1")).toHaveTextContent("未开始");
    expect(screen.getByTestId("plan-step-status-s2")).toHaveTextContent("待确认");
    expect(screen.getByTestId("plan-step-status-s3")).toHaveTextContent("执行中");
    expect(screen.getByTestId("plan-step-status-s4")).toHaveTextContent("已完成");
    expect(screen.getByTestId("plan-step-status-s5")).toHaveTextContent("失败");
    expect(screen.getByTestId("plan-step-status-s6")).toHaveTextContent("已停止");
    expect(screen.getByTestId("plan-step-status-s7")).toHaveTextContent("已停止");
    expect(screen.getByTestId("plan-step-status-s8")).toHaveTextContent("结果未知");
    expect(screen.getByTestId("plan-scope-hash")).toHaveTextContent("a".repeat(64));
  });

  it("中间层状态（submitted/awaiting_result/verifying）分别映射为已提交/等待结果/验证", () => {
    render(
      <PlanSteps
        steps={[
          step({ step_id: "x1", status: "submitted" }),
          step({ step_id: "x2", status: "awaiting_result" }),
          step({ step_id: "x3", status: "verifying" }),
        ]}
      />,
    );
    expect(screen.getByTestId("plan-step-status-x1")).toHaveTextContent("已提交");
    expect(screen.getByTestId("plan-step-status-x2")).toHaveTextContent("等待结果");
    expect(screen.getByTestId("plan-step-status-x3")).toHaveTextContent("验证");
  });

  it("不渲染假进度条：只有按真实状态统计的文字与计划级金额合计", () => {
    const { container } = render(<PlanSteps plan={plan} />);
    expect(container.querySelector('[role="progressbar"]')).toBeNull();
    expect(container.querySelector("progress")).toBeNull();
    expect(screen.getByTestId("plan-steps-summary")).toHaveTextContent("共 8 步");
    expect(screen.getByTestId("plan-steps-summary")).toHaveTextContent("已完成 1 步");
    expect(screen.getByTestId("plan-steps-summary")).toHaveTextContent("失败 1 步");
    expect(screen.getByTestId("plan-steps-summary")).toHaveTextContent("已停止 2 步");
    expect(within(screen.getByTestId("plan-totals")).getByText("¥1,234.56")).toBeInTheDocument();
    expect(within(screen.getByTestId("plan-totals")).getByText("¥20.00")).toBeInTheDocument();
  });

  it("RED 待确认步骤提示需要强验证；失败步骤展示服务端原因；未知步骤不得显示成功或失败", () => {
    render(<PlanSteps plan={plan} />);
    const red = screen.getByTestId("plan-step-status-s2").closest("li") as HTMLElement;
    expect(within(red).getByText(/必须完成强验证后才会提交/)).toBeInTheDocument();

    const failed = screen.getByTestId("plan-step-status-s5").closest("li") as HTMLElement;
    expect(within(failed).getByText(/失败原因：RISK_MISMATCH/)).toBeInTheDocument();

    const unknown = screen.getByTestId("plan-step-status-s8").closest("li") as HTMLElement;
    expect(within(unknown).getByText(/系统正在用原操作编号核对/)).toBeInTheDocument();
    expect(within(unknown).getByText(/请不要重复提交/)).toBeInTheDocument();
    expect(within(unknown).getByText(/不显示成功或失败/)).toBeInTheDocument();
  });

  it("在途步骤说明可能已产生外部效果；查看操作按钮回调携带 operation_id", async () => {
    const onViewOperation = vi.fn();
    render(<PlanSteps steps={[step({ step_id: "y1", status: "awaiting_result", operation_id: "op_abc" })]} onViewOperation={onViewOperation} />);
    expect(screen.getByText(/已提交但尚未确认结果/)).toBeInTheDocument();
    await screen.getByRole("button", { name: "查询该操作" }).click();
    expect(onViewOperation).toHaveBeenCalledWith("op_abc");
  });

  it("未登记的步骤状态显示原值；不可行的计划如实说明", () => {
    const { unmount } = render(<PlanSteps steps={[step({ step_id: "z1", status: "half_done" })]} />);
    expect(screen.getByTestId("plan-step-status-z1")).toHaveTextContent("未知状态（half_done）");
    unmount();

    render(<PlanSteps plan={{ ...plan, feasible: false, infeasible_reasons: ["预算 500 元不足以覆盖鲜花与蛋糕"] }} />);
    expect(screen.getByTestId("plan-feasible")).toHaveTextContent("不可行");
    expect(screen.getByTestId("plan-feasible")).toHaveTextContent("预算 500 元不足以覆盖鲜花与蛋糕");
  });

  it("空计划显示空提示而不是伪造步骤", () => {
    render(<PlanSteps steps={[]} />);
    expect(screen.getByText(/计划尚未生成/)).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).toBeNull();
  });
});