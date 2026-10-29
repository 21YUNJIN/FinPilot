import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { WealthCompare } from "../src/components/WealthCompare";
import type { WealthProduct } from "../src/types";

const products: WealthProduct[] = [
  {
    product_id: "wp_1",
    name: "稳健 90 天",
    issuer: "示例银行",
    product_type: "fixed_income",
    terms_version: "v3",
    issuer_risk_level: "R2",
    min_minor: 100000,
    fee_bps: 30,
    fee_fixed_minor: 0,
    term_days: 90,
    liquidity_terms: "到期赎回",
    settlement_days: 1,
    nav_e6: 1023456,
    nav_as_of_date: "2026-10-26",
    is_active: true,
  },
  {
    product_id: "wp_2",
    name: "量化增强",
    issuer: "示例基金",
    product_type: "equity",
    terms_version: "v1",
    issuer_risk_level: "R4",
    min_minor: 1000000,
    fee_bps: 150,
    fee_fixed_minor: 500,
    is_active: true,
    assessment_required: true,
    nav_is_na: true,
    nav_e6: null,
  },
];

describe("WealthCompare 理财比较（契约 wealth_product.json）", () => {
  it("展示风险/费用/期限/流动性/出处，风险以文字标签呈现", () => {
    render(<WealthCompare products={products} onSelect={vi.fn()} />);
    expect(screen.getByTestId("wealth-risk-wp_1")).toHaveTextContent("中低风险 R2");
    expect(screen.getByTestId("wealth-risk-wp_2")).toHaveTextContent("中高风险 R4");
    expect(screen.getByText("30 bps（0.30%）")).toBeInTheDocument();
    expect(screen.getByText("90 天（结算 1 天）")).toBeInTheDocument();
    expect(screen.getByText("到期赎回")).toBeInTheDocument();
    expect(screen.getByText(/示例银行 · 条款 v3/)).toBeInTheDocument();
    expect(screen.getByText(/1\.023456/)).toBeInTheDocument();
  });

  it("费率 + 固定费用都展示；净值数据不足时显示'不适用'并说明未编造", () => {
    render(<WealthCompare products={[products[1] as WealthProduct]} onSelect={vi.fn()} />);
    const row = screen.getByText("量化增强").closest("tr") as HTMLElement;
    expect(within(row).getByText(/150 bps（1.50%）/)).toBeInTheDocument();
    expect(within(row).getByText("¥5.00")).toBeInTheDocument();
    expect(within(row).getByText(/不适用（净值数据不足，未编造）/)).toBeInTheDocument();
  });

  it("缺失字段显示'不适用'（NA），不显示 0 或 NaN", () => {
    const sparse: WealthProduct = {
      product_id: "wp_3",
      name: "极简产品",
      issuer: "",
      product_type: "money_market",
      terms_version: "",
      issuer_risk_level: "R1",
      min_minor: 0,
      fee_bps: 10,
      fee_fixed_minor: 0,
      is_active: true,
    };
    const { container } = render(<WealthCompare products={[sparse]} onSelect={vi.fn()} />);
    const row = screen.getByText("极简产品").closest("tr") as HTMLElement;
    const na = within(row).getAllByText(/^不适用/);
    // 期限、流动性、净值、出处
    expect(na.length).toBeGreaterThanOrEqual(4);
    expect(container.textContent ?? "").not.toContain("NaN");
  });

  it("需要测评且未测评时给出提示入口，不直接进入购买", () => {
    render(<WealthCompare products={products} onSelect={vi.fn()} />);
    expect(screen.getByTestId("wealth-assessment-missing")).toHaveTextContent("不适用");
    expect(screen.getByTestId("wealth-assessment-required")).toHaveTextContent("未测评前只能查看信息");
  });

  it("未登记的风险等级显示原值而不是猜测档位", () => {
    render(
      <WealthCompare
        products={[{ ...(products[0] as WealthProduct), product_id: "wp_x", issuer_risk_level: "R6+" }]}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("wealth-risk-wp_x")).toHaveTextContent("未知风险等级（R6+）");
  });
});