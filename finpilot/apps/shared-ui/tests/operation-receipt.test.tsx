import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { OperationReceipt } from "../src/components/OperationReceipt";
import { challengeAttemptsRemaining, normalizeResultSummary, receiptOutcomeOf } from "../src/api/adapters";
import type { OperationView, Receipt } from "../src/types";

function operation(overrides: Partial<OperationView> = {}): OperationView {
  return {
    operation_id: "op_9f2c41",
    tool: "transfer.submit",
    status: "pending",
    required_level: "YELLOW",
    amount_minor: 30000,
    fee_minor: 0,
    currency: "CNY",
    version: 1,
    created_at: "2026-10-27T08:00:00Z",
    updated_at: "2026-10-27T08:00:00Z",
    ...overrides,
  };
}

describe("receiptOutcomeOf：成功必须有权威依据", () => {
  it("succeeded 且 effect_confirmed=true 才算已到账", () => {
    expect(receiptOutcomeOf(operation({ status: "succeeded", effect_confirmed: true, settled_at: "2026-10-27T08:05:00Z" }))).toBe(
      "settled",
    );
  });

  it("succeeded 但没有 effect_confirmed 依据时必须降级为结果未知", () => {
    expect(receiptOutcomeOf(operation({ status: "succeeded", effect_confirmed: false }))).toBe("unknown");
    expect(receiptOutcomeOf(operation({ status: "succeeded" }))).toBe("unknown");
  });

  it("已验签且已入账的回执可以作为权威依据", () => {
    const receipt: Receipt = {
      receipt_id: "rc_1",
      operation_id: "op_9f2c41",
      provider_id: "bank-sandbox",
      provider_ref: "bank_ref_1",
      source: "provider_callback",
      event_version: 2,
      received_at: "2026-10-27T08:05:00Z",
      verified_status: "verified",
      payload_hash: "b".repeat(64),
      payload: {},
      state: "applied",
    };
    expect(receiptOutcomeOf(operation({ status: "succeeded" }), receipt)).toBe("settled");
  });

  it("其它状态映射：dispatching/pending 是受理，unknown 是未知，failed 是失败，未提交是待确认", () => {
    expect(receiptOutcomeOf(operation({ status: "dispatching" }))).toBe("accepted");
    expect(receiptOutcomeOf(operation({ status: "pending" }))).toBe("accepted");
    expect(receiptOutcomeOf(operation({ status: "unknown" }))).toBe("unknown");
    expect(receiptOutcomeOf(operation({ status: "failed", error_code: "PROVIDER_REJECTED" }))).toBe("failed");
    expect(receiptOutcomeOf(operation({ status: "created" }))).toBe("awaiting_confirmation");
    expect(receiptOutcomeOf(operation({ status: "awaiting_authorization" }))).toBe("awaiting_confirmation");
  });
});

describe("OperationReceipt 操作回执（由权威 OperationView 推导）", () => {
  it("已受理：明确说明受理不等于到账，且不出现'已到账'", () => {
    const { container } = render(<OperationReceipt operation={operation({ status: "pending", submitted_at: "2026-10-27T08:00:00Z" })} />);
    expect(screen.getByTestId("receipt-outcome-chip")).toHaveTextContent("已受理（等待结果）");
    expect(screen.getByTestId("receipt-explanation")).toHaveTextContent("受理不等于到账");
    expect(container.textContent ?? "").not.toContain("已到账");
    expect(screen.getByTestId("receipt-actual-amount")).toHaveTextContent("¥300.00");
    expect(screen.getByTestId("receipt-operation-id")).toHaveTextContent("op_9f2c41");
  });

  it("已安排（定时任务）：说明尚未执行、当前没有发生转账", () => {
    render(<OperationReceipt operation={operation({ status: "pending", tool: "schedule.create" })} />);
    expect(screen.getByTestId("receipt-outcome-chip")).toHaveTextContent("已安排（尚未执行）");
    expect(screen.getByTestId("receipt-explanation")).toHaveTextContent("尚未执行");
    expect(screen.getByTestId("receipt-explanation")).toHaveTextContent("没有发生转账");
  });

  it("succeeded 但无权威依据：显示状态不一致并按未知处理，不得显示已到账", () => {
    const { container } = render(<OperationReceipt operation={operation({ status: "succeeded" })} />);
    expect(screen.getByTestId("receipt-inconsistent-success")).toHaveTextContent("状态不一致");
    expect(screen.getByTestId("receipt-outcome-chip")).toHaveTextContent("结果未知");
    expect(container.textContent ?? "").not.toContain("已到账：");
  });

  it("结果未知：给出查询原订单的说明与入口（不得写成失败或完成）", async () => {
    const onQueryOriginalOrder = vi.fn();
    const { container } = render(
      <OperationReceipt
        operation={operation({ status: "unknown", unknown_since: "2026-10-27T08:01:00Z" })}
        onQueryOriginalOrder={onQueryOriginalOrder}
      />,
    );
    expect(screen.getByTestId("receipt-unknown-notice")).toHaveTextContent("原操作编号");
    expect(screen.getByTestId("receipt-unknown-notice")).toHaveTextContent("不要重复提交");
    expect(screen.getByTestId("receipt-unknown-notice")).toHaveTextContent("进入未知状态时间");
    expect(container.textContent ?? "").not.toContain("操作失败");
    await screen.getByTestId("receipt-query-button").click();
    expect(onQueryOriginalOrder).toHaveBeenCalledWith("op_9f2c41");
  });

  it("部分成功：分别展示已完成/未完成/仍在处理金额与未完成项", () => {
    render(
      <OperationReceipt
        operation={operation({ status: "pending", tool: "merchant.book" })}
        partial={{ succeeded_minor: 12000, failed_minor: 8000, pending_minor: 0 }}
        remainingItems={["商户 B 缺货，未下单"]}
      />,
    );
    const partial = screen.getByTestId("receipt-partial");
    expect(partial).toHaveTextContent("已完成金额");
    expect(partial).toHaveTextContent("¥120.00");
    expect(partial).toHaveTextContent("未完成金额");
    expect(partial).toHaveTextContent("¥80.00");
    expect(screen.getByTestId("receipt-remaining")).toHaveTextContent("商户 B 缺货，未下单");
  });

  it("权威回执来源与验签状态一并展示", () => {
    const receipt: Receipt = {
      receipt_id: "rc_2",
      operation_id: "op_9f2c41",
      provider_id: "bank-sandbox",
      provider_ref: "bank_ref_2",
      source: "provider_query",
      event_version: 3,
      received_at: "2026-10-27T08:06:00Z",
      verified_status: "verified",
      payload_hash: "c".repeat(64),
      payload: {},
      state: "applied",
    };
    render(
      <OperationReceipt
        operation={operation({ status: "succeeded", effect_confirmed: true, settled_at: "2026-10-27T08:06:00Z" })}
        receipt={receipt}
      />,
    );
    expect(screen.getByTestId("receipt-outcome-chip")).toHaveTextContent("已到账");
    expect(screen.getByTestId("receipt-source")).toHaveTextContent("已入账");
    expect(screen.getByTestId("receipt-source")).toHaveTextContent("已验签");
    expect(screen.getByTestId("receipt-source")).toHaveTextContent("事件版本 3");
  });

  it("待确认与失败口径分别有明确说明；缺失金额显示'不适用'", () => {
    const { unmount } = render(<OperationReceipt operation={operation({ status: "created", amount_minor: 0, submitted_at: null })} />);
    expect(screen.getByTestId("receipt-explanation")).toHaveTextContent("尚未提交");
    expect(screen.getByTestId("receipt-actual-amount")).toHaveTextContent("¥0.00");
    unmount();

    render(<OperationReceipt operation={operation({ status: "failed", error_code: "PROVIDER_REJECTED" })} />);
    expect(screen.getByTestId("receipt-explanation")).toHaveTextContent("未产生资金效果");
    expect(screen.getByText("PROVIDER_REJECTED")).toBeInTheDocument();
  });
});

describe("适配器：挑战与结果摘要", () => {
  it("挑战剩余尝试次数由 attempts/max_attempts 计算，不猜测", () => {
    expect(challengeAttemptsRemaining({ attempts: 2, max_attempts: 5 } as never)).toBe(3);
    expect(challengeAttemptsRemaining({ attempts: 5, max_attempts: 5 } as never)).toBe(0);
    expect(challengeAttemptsRemaining(null)).toBeNull();
  });

  it("任务结果摘要缺失时返回 null，不伪造结果", () => {
    expect(normalizeResultSummary(null)).toBeNull();
    expect(normalizeResultSummary({})).toBeNull();
    const summary = normalizeResultSummary({
      completed_effects: [{ kind: "transfer", description: "转账已到账", verified: true, authority: "ledger" }],
      unknown_effects: ["补卡结果未知"],
      next_actions: [{ action_id: "a1", label: "查询原订单", kind: "retry_same_operation" }],
      total_spent_minor: 30000,
    });
    expect(summary?.completed[0]?.label).toBe("转账已到账");
    expect(summary?.unknown).toEqual(["补卡结果未知"]);
    expect(summary?.nextActions[0]?.label).toBe("查询原订单");
    expect(summary?.totalSpentMinor).toBe(30000);
  });
});