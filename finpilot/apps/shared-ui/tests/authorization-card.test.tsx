import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { AuthorizationCard } from "../src/components/AuthorizationCard";
import type { ScopeSummary } from "../src/types";

const summary: ScopeSummary = {
  tool: "card.replace",
  amount_minor: 123456,
  fee_minor: 2000,
  irreversible_cost_minor: 500,
  currency: "CNY",
  source_account_id: "account_salary",
  object_label: "信用卡补卡",
  counterparty: { name: "招商银行信用卡中心", account_id: "acc_card_1" },
  time_window: { label: "2026-10-28 09:00—17:00（Asia/Shanghai）" },
  irreversible: true,
  irreversible_impacts: ["补卡工本费 20.00 元不可退", "旧卡立即失效"],
  cancel_conditions: ["未提交前可撤销；提交后不可撤回"],
  scope_hash: "sh_9f2c41ab77de",
  notes: ["补卡后旧卡按条款处理"],
};

describe("AuthorizationCard 授权卡片", () => {
  it("RED 未完成强验证时：不得显示成功/完成态，也不得提供提交按钮", () => {
    const onConfirm = vi.fn();
    const onVerify = vi.fn();
    const { container } = render(
      <AuthorizationCard
        level="RED"
        summary={summary}
        verified={false}
        resultStatus="succeeded"
        onConfirm={onConfirm}
        onVerify={onVerify}
      />,
    );

    // 关键断言：整张卡片不出现"成功"字样，也不出现完成态标签
    expect(container.textContent ?? "").not.toContain("成功");
    expect(screen.queryByText(/已完成/)).toBeNull();
    expect(screen.queryByTestId("authorization-confirm-button")).toBeNull();
    expect(screen.getByTestId("authorization-verify-button")).toBeInTheDocument();
    expect(screen.getByTestId("authorization-verify-required")).toHaveTextContent("必须完成强验证");
    // 调用方误传完成态时被显式拒绝展示
    expect(screen.getByTestId("authorization-red-guard")).toHaveTextContent("不得展示完成态");
    expect(screen.getByTestId("authorization-card")).toHaveAttribute("data-needs-verification", "true");
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("RED 完成强验证后才出现确认提交按钮（并说明受理不等于到账）", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(
      <AuthorizationCard
        level="RED"
        summary={summary}
        verified
        status="verifying"
        onConfirm={onConfirm}
        onVerify={vi.fn()}
      />,
    );

    expect(screen.queryByTestId("authorization-verify-button")).toBeNull();
    const confirm = screen.getByTestId("authorization-confirm-button");
    expect(confirm).toHaveTextContent("确认提交（红色等级，已验证）");
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("authorization-verified")).toHaveTextContent("受理不等于到账");
  });

  it("展示与 scope_hash 同源的金额、费用、不可退成本、对象、时间窗、不可逆影响", () => {
    render(<AuthorizationCard level="YELLOW" summary={summary} />);
    expect(screen.getByTestId("authorization-scope-hash")).toHaveTextContent("sh_9f2c41ab77de");
    expect(screen.getByTestId("authorization-amount")).toHaveTextContent("¥1,234.56");
    expect(screen.getByText("¥20.00")).toBeInTheDocument();
    expect(screen.getByText("¥5.00")).toBeInTheDocument();
    expect(screen.getByTestId("authorization-counterparty")).toHaveTextContent("招商银行信用卡中心");
    expect(screen.getByText(/2026-10-28 09:00—17:00/)).toBeInTheDocument();
    expect(screen.getByTestId("authorization-irreversible")).toHaveTextContent("不可撤销：是");
    expect(screen.getByText("补卡工本费 20.00 元不可退")).toBeInTheDocument();
    expect(screen.getByText(/未提交前可撤销/)).toBeInTheDocument();
  });

  it("YELLOW 需要用户明确确认；GREEN 不需要确认", () => {
    const onConfirm = vi.fn();
    const { unmount } = render(<AuthorizationCard level="YELLOW" summary={summary} onConfirm={onConfirm} />);
    expect(screen.getByTestId("authorization-confirm-button")).toHaveTextContent("确认并提交（黄色等级）");
    unmount();

    render(<AuthorizationCard level="GREEN" summary={summary} />);
    expect(screen.queryByTestId("authorization-confirm-button")).toBeNull();
    expect(screen.queryByTestId("authorization-verify-button")).toBeNull();
    expect(screen.getByText("绿色等级：无需你确认，服务端完成身份与归属检查后执行；如需停止请在任务页发起停止请求。")).toBeInTheDocument();
  });

  it("缺少 scope_hash 或金额时显示'不适用/未提供'，不猜测数值", () => {
    render(<AuthorizationCard level="YELLOW" summary={{ tool: "transfer.submit" }} />);
    expect(screen.getByTestId("authorization-amount")).toHaveTextContent("不适用");
    expect(screen.getByText(/未提供（无法核对范围，请先刷新）/)).toBeInTheDocument();
    expect(screen.getByTestId("authorization-counterparty")).toHaveTextContent("不适用");
  });

  it("未知等级不自动执行，并显示原值", () => {
    render(<AuthorizationCard level="PURPLE" summary={summary} />);
    expect(screen.getByTestId("authorization-level-chip")).toHaveTextContent("等级 PURPLE");
    expect(screen.getByText(/未知等级（PURPLE）/)).toBeInTheDocument();
  });
});