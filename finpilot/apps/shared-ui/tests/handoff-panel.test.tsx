import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { HandoffPanel } from "../src/components/HandoffPanel";
import type { HandoffCase } from "../src/types";

const handoff: HandoffCase = {
  case_id: "case_2026_10_27_001",
  task_id: "task_birthday",
  status: "awaiting_human",
  reason: "转账结果 15 分钟仍未确认，升级人工队列",
  opened_at: "2026-10-27T08:00:00Z",
  completed: [{ label: "支付生日蛋糕定金", operation_id: "op_done_1", amount_minor: 10000 }],
  unknown: [{ label: "转账给花店", operation_id: "op_unknown_2", amount_minor: 30000, detail: "提供方超时未回执" }],
  not_submitted: [{ label: "预订餐厅", detail: "尚未提交" }],
  funds: {
    currency: "CNY",
    spent_minor: 10000,
    fees_minor: 0,
    reserved_minor: 30000,
    unknown_minor: 30000,
    refund_pending_minor: null,
  },
  authorizations: [{ grant_id: "grant_1", plan_id: "plan_1", scope_hash: "sh_abc123", status: "active" }],
  operator_actions: [{ action: "已向提供方发起对账", at: "2026-10-27T08:10:00Z", by: "operator_07" }],
};

describe("HandoffPanel 人工接管", () => {
  it("已完成 / 结果未知 / 未提交 分区展示，未知项带操作编号且不被写成完成", () => {
    render(<HandoffPanel handoff={handoff} />);
    const completed = screen.getByTestId("handoff-completed");
    const unknown = screen.getByTestId("handoff-unknown");
    const notSubmitted = screen.getByTestId("handoff-not-submitted");

    expect(within(completed).getByText(/op_done_1/)).toBeInTheDocument();
    expect(within(unknown).getByText(/op_unknown_2/)).toBeInTheDocument();
    expect(within(completed).queryByText(/op_unknown_2/)).toBeNull();
    expect(within(unknown).getByText(/提供方超时未回执/)).toBeInTheDocument();
    expect(within(notSubmitted).getByText("预订餐厅")).toBeInTheDocument();
    expect(within(notSubmitted).getByText(/不适用（尚未提交）/)).toBeInTheDocument();
    expect(screen.getByText(/未知项不得当作完成或失败/)).toBeInTheDocument();
  });

  it("资金与授权清单分开展示，缺失金额显示'不适用'", () => {
    render(<HandoffPanel handoff={handoff} />);
    const funds = screen.getByTestId("handoff-funds");
    expect(within(funds).getByText("¥100.00")).toBeInTheDocument();
    // 仍被预留与结果未知金额都是 300.00：逐项核对而不是唯一匹配
    expect(within(funds).getAllByText("¥300.00")).toHaveLength(2);
    expect(within(funds).getAllByText("不适用").length).toBeGreaterThanOrEqual(1);
    const auths = screen.getByTestId("handoff-authorizations");
    expect(within(auths).getByText(/grant_1/)).toBeInTheDocument();
    expect(within(auths).getByText("sh_abc123")).toBeInTheDocument();
  });

  it("明确写出操作员不能代替用户同意", () => {
    render(<HandoffPanel handoff={handoff} />);
    expect(screen.getByText(/操作员不能代替用户同意任何高风险操作/)).toBeInTheDocument();
    expect(screen.getByTestId("handoff-operator-actions")).toHaveTextContent("operator_07");
  });

  it("未开案时提供申请入口，并说明不影响资金结果", async () => {
    const onRequestHandoff = vi.fn();
    render(<HandoffPanel handoff={null} onRequestHandoff={onRequestHandoff} />);
    expect(screen.getByText(/申请接管不会改变资金结果/)).toBeInTheDocument();
    await screen.getByRole("button", { name: "申请人工接管" }).click();
    expect(onRequestHandoff).toHaveBeenCalledTimes(1);
  });
});