import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { GoalSummaryCard } from "../src/components/GoalSummaryCard";
import type { Clarification, GoalSpec } from "../src/types";

const goal: GoalSpec = {
  schema_version: "1.0",
  task_id: "task_birthday",
  revision: 5,
  reference_time: "2026-10-27T08:00:00Z",
  goals: ["birthday_gift", "deliver_on_time"],
  hard_constraints: {
    currency: "CNY",
    total_budget_minor: 50000,
    budget_includes_fees: true,
    protected_floor_minor: 100000,
    protected_floor_scope: ["account_salary"],
    delivery_deadline: "2026-10-29T10:00:00Z",
    max_confirmations: 2,
  },
  preferences: [{ higher: "鲜花", lower: "蛋糕" }],
  unresolved: ["payee_id"],
  authorization_status: "not_requested",
  inferred_fields: ["birthday_date"],
  evidence: ["联系人表：妈妈（尾号 1234）", "近两年生日流水 2 笔"],
  funding_sources: ["account_salary"],
  unconfirmed_receivables_are_spendable: false,
};

const clarifications: Clarification[] = [
  {
    clarification_id: "cl_1",
    task_id: "task_birthday",
    revision: 5,
    field: "payee_id",
    prompt: "联系人有两位「张三」，请选择收款人",
    blocking: true,
    status: "open",
    created_at: "2026-10-27T08:00:00Z",
    options: [
      { option_id: "payee_1", label: "张三（尾号 1234）" },
      { option_id: "payee_2", label: "张三（尾号 5678）" },
    ],
  },
];

describe("GoalSummaryCard 目标摘要（契约 goal.json）", () => {
  it("硬约束、偏好、事实与来源三个分区各自独立展示", () => {
    render(<GoalSummaryCard goal={goal} clarifications={clarifications} />);
    const constraints = screen.getByRole("region", { name: "硬约束" });
    const preferences = screen.getByRole("region", { name: "偏好" });
    const facts = screen.getByRole("region", { name: "事实与来源" });

    expect(within(constraints).getByText("总预算")).toBeInTheDocument();
    expect(within(constraints).queryByText("来源证据")).toBeNull();
    expect(within(facts).getAllByText("来源证据").length).toBeGreaterThanOrEqual(1);
    expect(within(facts).queryByText("总预算")).toBeNull();
    expect(within(preferences).getByText("鲜花")).toBeInTheDocument();
    expect(within(preferences).queryByText("来源证据")).toBeNull();
  });

  it("硬约束金额按整数分渲染，保护账户组与交付截止可见", () => {
    render(<GoalSummaryCard goal={goal} />);
    const constraints = screen.getByRole("region", { name: "硬约束" });
    expect(within(constraints).getByText("¥500.00")).toBeInTheDocument();
    expect(within(constraints).getByText("¥1,000.00")).toBeInTheDocument();
    expect(within(constraints).getByText("account_salary")).toBeInTheDocument();
    expect(within(constraints).getByText(/2026-10-29 18:00:00/)).toBeInTheDocument();
    expect(within(constraints).getByText("（含费用）")).toBeInTheDocument();
  });

  it("未确定字段非空时明确提示未澄清前零写入", () => {
    render(<GoalSummaryCard goal={goal} clarifications={clarifications} />);
    expect(screen.getByTestId("goal-unresolved-blocking")).toHaveTextContent("payee_id");
    expect(screen.getByTestId("goal-unresolved-blocking")).toHaveTextContent("不会执行任何写操作");
    expect(screen.getByText(/有 1 项阻塞澄清未完成/)).toBeInTheDocument();
  });

  it("推断项被显式标注为推断，来源证据与用户表述分开", () => {
    render(<GoalSummaryCard goal={goal} />);
    const facts = screen.getByRole("region", { name: "事实与来源" });
    expect(within(facts).getByText("推断（非用户明示）")).toBeInTheDocument();
    expect(within(facts).getByText("birthday_date")).toBeInTheDocument();
    expect(within(facts).getByText("联系人表：妈妈（尾号 1234）")).toBeInTheDocument();
  });

  it("待澄清项提供有限选择，选择后回调携带 option_id", async () => {
    const onAnswer = vi.fn();
    render(<GoalSummaryCard goal={goal} clarifications={clarifications} onAnswer={onAnswer} />);
    expect(screen.getByText(/（阻塞执行）/)).toBeInTheDocument();
    await screen.getByRole("button", { name: "张三（尾号 1234）" }).click();
    expect(onAnswer.mock.calls[0]?.[1]).toBe("payee_1");
  });

  it("授权状态如实展示；无目标数据时显示空态", () => {
    const { unmount } = render(<GoalSummaryCard goal={goal} />);
    expect(screen.getByTestId("goal-authorization-status")).toHaveTextContent("授权未申请");
    unmount();

    render(<GoalSummaryCard goal={null} />);
    expect(screen.getByText(/尚未生成目标摘要/)).toBeInTheDocument();
  });
});