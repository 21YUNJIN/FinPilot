import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { CardPanel } from "../src/components/CardPanel";
import type { Card } from "../src/types";

const cards: Card[] = [
  {
    card_id: "card_1",
    account_id: "account_salary",
    masked_pan: "**** **** **** 1234",
    brand: "标准白金卡",
    lifecycle_status: "active",
    credit_limit_minor: 5000000,
    version: 3,
    application_status: "under_review",
    controls: [
      {
        control_id: "ctl_1",
        card_id: "card_1",
        control_type: "user_limit",
        limit_minor: 100000,
        created_by: "user_demo_01",
        created_by_role: "customer",
        active: true,
        created_at: "2026-10-20T08:00:00Z",
      },
      {
        control_id: "ctl_2",
        card_id: "card_1",
        control_type: "risk_freeze",
        reason: "检测到异常交易模式",
        created_by: "risk_operator_07",
        created_by_role: "risk",
        active: true,
        created_at: "2026-10-21T08:00:00Z",
      },
      {
        control_id: "ctl_3",
        card_id: "card_1",
        control_type: "channel_lock",
        channel: "online",
        created_by: "user_demo_01",
        created_by_role: "customer",
        active: false,
        released_by: "user_demo_01",
        released_at: "2026-10-22T08:00:00Z",
        created_at: "2026-10-19T08:00:00Z",
      },
    ],
  },
  {
    card_id: "card_2",
    account_id: "account_salary",
    masked_pan: "**** **** **** 5678",
    lifecycle_status: "lost",
    credit_limit_minor: 0,
    version: 5,
  },
];

describe("CardPanel 卡片面板（契约 card.json / card_control.json）", () => {
  it("卡生命周期与申请状态分开展示，只显示脱敏卡号", () => {
    render(<CardPanel cards={cards} />);
    expect(screen.getByTestId("card-lifecycle-card_1")).toHaveTextContent("正常");
    expect(screen.getByTestId("card-application-card_1")).toHaveTextContent("申请审核中");
    expect(screen.getByTestId("card-lifecycle-card_2")).toHaveTextContent("已挂失");
    expect(screen.getByText(/不适用（任务视图中未返回申请对象）/)).toBeInTheDocument();
    expect(screen.getByText("标准白金卡 **** **** **** 1234")).toBeInTheDocument();
    expect(screen.getByText(/完整卡号、CVV 与有效期不会出现/)).toBeInTheDocument();
  });

  it("仅展示生效中的卡控，标明类型/来源角色/可解除角色", () => {
    render(<CardPanel cards={[cards[0] as Card]} />);
    const controls = screen.getByTestId("card-controls-card_1");
    expect(within(controls).getByText("用户自设限额")).toBeInTheDocument();
    expect(within(controls).getByText("风控冻结")).toBeInTheDocument();
    // 已解除的渠道锁（active=false）不再出现在生效列表里
    expect(within(controls).queryByText("渠道锁定")).toBeNull();
    expect(within(controls).getByText(/可解除角色：customer（你自己）/)).toBeInTheDocument();
    expect(within(controls).getByText(/仅风控角色可解除（客户与 Agent 均无权）/)).toBeInTheDocument();
    expect(within(controls).getByText(/检测到异常交易模式/)).toBeInTheDocument();
    expect(screen.getByText(/解除用户自设锁定不会同时解除风控冻结/)).toBeInTheDocument();
  });

  it("RED 操作明确标注需强验证，YELLOW 操作标注需确认", async () => {
    const onReportLost = vi.fn();
    render(
      <CardPanel
        cards={[cards[1] as Card]}
        onReportLost={onReportLost}
        onReplace={vi.fn()}
        onSetLimit={vi.fn()}
        onSetControl={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: /挂失（红色等级：需强验证）/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /补卡（红色等级：需强验证）/ })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /调整额度（黄色等级）/ })).toBeInTheDocument();
    await screen.getByRole("button", { name: /挂失/ }).click();
    expect(onReportLost.mock.calls[0]?.[0]).toMatchObject({ card_id: "card_2" });
  });

  it("空列表显示空提示；无卡控时显示'不适用'而不是空白", () => {
    const { unmount } = render(<CardPanel cards={[]} />);
    expect(screen.getByText("当前没有卡片记录。")).toBeInTheDocument();
    unmount();

    render(<CardPanel cards={[cards[1] as Card]} />);
    expect(screen.getByText(/叠加限制：不适用/)).toBeInTheDocument();
  });
});