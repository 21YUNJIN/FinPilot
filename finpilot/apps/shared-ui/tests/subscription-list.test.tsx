import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { SubscriptionList } from "../src/components/SubscriptionList";
import type { Mandate, Subscription } from "../src/types";

const subscriptions: Subscription[] = [
  {
    subscription_id: "sub_1",
    merchant_id: "m_video",
    merchant_name: "视频会员",
    amount_minor: 2500,
    period: "monthly",
    next_charge_at: "2026-11-01T00:00:00Z",
    status: "active",
    evidence: "contract",
    terms_version: "v2",
    mandate_id: "md_1",
    version: 2,
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    subscription_id: "sub_2",
    merchant_id: "m_cloud",
    merchant_name: "云存储",
    amount_minor: 900,
    period: "monthly",
    status: "cancelled",
    evidence: "inferred",
    inference_confidence_bps: 8200,
    is_contract_evidence: false,
    terms_version: "v1",
    mandate_id: "md_2",
    cancelled_at: "2026-10-31T00:00:00Z",
    last_charged_at: "2026-10-01T00:00:00Z",
    version: 4,
    created_at: "2026-02-01T00:00:00Z",
  },
  {
    subscription_id: "sub_3",
    merchant_id: "m_gym",
    merchant_name: "健身房",
    amount_minor: 19900,
    period: "yearly",
    status: "active",
    evidence: "contract",
    terms_version: "v1",
    mandate_id: "md_3",
    version: 1,
    created_at: "2026-03-01T00:00:00Z",
  },
];

const mandates: Mandate[] = [
  {
    mandate_id: "md_1",
    subscription_id: "sub_1",
    merchant_id: "m_video",
    status: "active",
    version: 1,
    created_at: "2026-01-01T00:00:00Z",
  },
  {
    mandate_id: "md_2",
    subscription_id: "sub_2",
    merchant_id: "m_cloud",
    status: "active",
    version: 2,
    created_at: "2026-02-01T00:00:00Z",
  },
  {
    mandate_id: "md_3",
    subscription_id: "sub_3",
    merchant_id: "m_gym",
    status: "revoked",
    revoked_at: "2026-10-20T08:00:00Z",
    version: 3,
    created_at: "2026-03-01T00:00:00Z",
  },
];

describe("SubscriptionList 订阅与代扣（契约 subscription.json / mandate.json）", () => {
  it("合同状态与代扣授权状态分别展示（两个对象不能混成一个标签）", () => {
    render(<SubscriptionList subscriptions={subscriptions} mandates={mandates} />);
    const contract = screen.getByTestId("subscription-contract-sub_1");
    const mandate = screen.getByTestId("subscription-mandate-sub_1");
    expect(contract).toHaveTextContent("合同生效中");
    expect(mandate).toHaveTextContent("代扣授权有效");
  });

  it("合同已取消但代扣仍有效：提示部分成功并要求单独撤销代扣", () => {
    render(<SubscriptionList subscriptions={subscriptions} mandates={mandates} />);
    const partial = screen.getByTestId("subscription-partial-sub_2");
    expect(partial).toHaveTextContent("部分成功");
    expect(partial).toHaveTextContent("代扣授权仍然有效");
    expect(screen.getByTestId("subscription-contract-sub_2")).toHaveTextContent("合同已取消");
    expect(screen.getByTestId("subscription-mandate-sub_2")).toHaveTextContent("代扣授权有效");
  });

  it("代扣已撤销但合同仍生效：同样提示部分成功（反之亦然）", () => {
    render(<SubscriptionList subscriptions={subscriptions} mandates={mandates} />);
    const partial = screen.getByTestId("subscription-partial-sub_3");
    expect(partial).toHaveTextContent("部分成功");
    expect(partial).toHaveTextContent("合同仍生效");
  });

  it("疑似周期（inferred）不得冒充合同，必须标注推断与置信度", () => {
    render(<SubscriptionList subscriptions={subscriptions} mandates={mandates} />);
    expect(screen.getByTestId("subscription-evidence-sub_1")).toHaveTextContent("合同证据");
    const inferred = screen.getByTestId("subscription-evidence-sub_2");
    expect(inferred).toHaveTextContent("疑似周期");
    expect(inferred).toHaveTextContent("置信度 82.00%");
  });

  it("金额、下次扣款、取消时间与代扣撤销时间如实展示", () => {
    render(<SubscriptionList subscriptions={subscriptions} mandates={mandates} />);
    expect(screen.getByText("¥25.00")).toBeInTheDocument();
    expect(screen.getAllByText(/\/ 每月/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("2026-11-01 08:00:00")).toBeInTheDocument();
    expect(screen.getByText("2026-10-31 08:00:00")).toBeInTheDocument();
    const gym = screen.getByTestId("subscription-mandate-sub_3").closest("li") as HTMLElement;
    expect(within(gym).getByText("2026-10-20 16:00:00")).toBeInTheDocument();
  });

  it("取消合同与撤销代扣是两个不同入口，回传对应订阅", async () => {
    const onCancelContract = vi.fn();
    const onRevokeMandate = vi.fn();
    render(
      <SubscriptionList
        subscriptions={[subscriptions[0] as Subscription]}
        mandates={mandates}
        onCancelContract={onCancelContract}
        onRevokeMandate={onRevokeMandate}
      />,
    );
    await screen.getByRole("button", { name: /取消合同/ }).click();
    await screen.getByRole("button", { name: /撤销代扣授权/ }).click();
    expect(onCancelContract.mock.calls[0]?.[0]).toMatchObject({ subscription_id: "sub_1" });
    expect(onRevokeMandate.mock.calls[0]?.[0]).toMatchObject({ subscription_id: "sub_1" });
  });

  it("没有关联代扣授权时显示'不适用'，未知状态显示原值", () => {
    render(
      <SubscriptionList
        subscriptions={[
          {
            subscription_id: "sub_x",
            merchant_id: "m_x",
            merchant_name: "未知商户",
            amount_minor: 100,
            period: "yearly",
            status: "frozen",
            evidence: "unknown_evidence",
            terms_version: "v1",
            version: 1,
            created_at: "2026-01-01T00:00:00Z",
          },
        ]}
      />,
    );
    expect(screen.getByTestId("subscription-contract-sub_x")).toHaveTextContent("未知状态（frozen）");
    expect(screen.getByTestId("subscription-mandate-sub_x")).toHaveTextContent("不适用（该合同没有关联的代扣授权）");
    expect(screen.getByTestId("subscription-evidence-sub_x")).toHaveTextContent("证据类型未登记（unknown_evidence）");
  });
});