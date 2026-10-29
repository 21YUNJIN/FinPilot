import { render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { BillReport } from "../src/components/BillReport";
import type { BillReport as BillReportData } from "../src/types";

const report: BillReportData = {
  report_id: "bill_2026_10",
  period: { kind: "month", year: 2026, month: 10, timezone: "Asia/Shanghai" },
  generated_at: "2026-10-31T16:05:00Z",
  data_cutoff_at: "2026-10-31T16:00:00Z",
  currency: "CNY",
  total_inflow_minor: 2000000,
  total_outflow_minor: 1234567,
  net_minor: 765433,
  internal_transfer_minor: 500000,
  internal_transfers_excluded_from_consumption: true,
  fee_minor: 2000,
  refund_minor: 15000,
  uncategorized_minor: 23456,
  categories: [
    { category: "餐饮", amount_minor: 400000, count: 22, share_bps: 3240 },
    { category: "订阅", amount_minor: 834567, count: 3 },
  ],
  anomalies: [{ label: "订阅支出上升", basis: "较上月增加 12%，依据 3 笔固定周期扣款" }],
  warnings: ["有 2 笔交易尚未完全入账"],
  zero_denominator_flags: ["categories"],
  untrusted_text: "某商户备注：双击领取优惠",
  source: "local_ledger",
  provenance: {
    adapter: "finpilot-api",
    observed_at: "2026-10-31T16:05:00Z",
    schema_version: "1.0",
    mode: "offline_replay",
  },
};

describe("BillReport 账单报表（契约 bill_report.json）", () => {
  it("展示账期、数据截至时间、总额与分类金额（整数分换算）", () => {
    render(<BillReport report={report} />);
    expect(screen.getByTestId("bill-period")).toHaveTextContent("2026 年 10 月");
    expect(screen.getByText(/数据截至：2026-11-01 00:00:00/)).toBeInTheDocument();
    expect(screen.getByTestId("bill-total-spend")).toHaveTextContent("¥12,345.67");
    expect(screen.getByText("¥20,000.00")).toBeInTheDocument();
    expect(screen.getByText("¥7,654.33")).toBeInTheDocument();
    expect(screen.getByText("¥5,000.00")).toBeInTheDocument();
    expect(screen.getByText("¥20.00")).toBeInTheDocument();
    expect(screen.getByText("¥150.00")).toBeInTheDocument();
  });

  it("内部转账不计消费：单独展示且有明确说明", () => {
    render(<BillReport report={report} />);
    expect(screen.getByText(/内部转账（不计消费）/)).toBeInTheDocument();
  });

  it("分类占比优先用 share_bps（整数基点），缺失时按总额换算", () => {
    render(<BillReport report={report} />);
    expect(screen.getByText("餐饮")).toBeInTheDocument();
    expect(screen.getByText("32.40%")).toBeInTheDocument();
    expect(screen.getByText("67.60%")).toBeInTheDocument();
    expect(screen.getByText("¥4,000.00")).toBeInTheDocument();
  });

  it("零分母不造值：显示'不适用'并列出零分母项", () => {
    render(<BillReport report={{ ...report, total_outflow_minor: 0, categories: [{ category: "餐饮", amount_minor: 0, count: 0 }] }} />);
    expect(screen.getAllByText("不适用").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText(/零分母项：categories/)).toBeInTheDocument();
  });

  it("异常提示带可核查依据，商户自由文本标注为不可信", () => {
    render(<BillReport report={report} />);
    expect(screen.getByTestId("bill-anomalies")).toHaveTextContent("较上月增加 12%");
    expect(screen.getByTestId("bill-untrusted-text")).toHaveTextContent("不可信");
    expect(screen.getByTestId("bill-warnings")).toHaveTextContent("有 2 笔交易尚未完全入账");
  });

  it("出处与运行模式一并展示（离线重放必须标注）", () => {
    render(<BillReport report={report} />);
    expect(screen.getByText("数据来源：本地账本")).toBeInTheDocument();
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("离线重放");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("非实时");
  });

  it("加载中与失败分别显示真实说明，不伪造账单", () => {
    const { unmount } = render(<BillReport report={null} loading />);
    expect(screen.getByText("正在查询服务端账单数据…")).toBeInTheDocument();
    unmount();

    render(
      <BillReport
        report={null}
        error={{
          code: "DEPENDENCY_FAILED",
          message: "依赖的离线或缓存能力失败，未伪造结果。",
          hint: "请稍后再试。",
          retryMode: "retry_after_backoff",
          knownCode: true,
          requiresQueryOfOriginalOperation: false,
          httpStatus: 503,
          status: "failed",
          reasonCodes: [],
          details: null,
          requestId: null,
          operationId: null,
          envelope: null,
          transport: false,
        }}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.getByTestId("error-banner")).toHaveTextContent("未伪造结果");
    expect(within(screen.getByTestId("error-banner")).getByText("DEPENDENCY_FAILED")).toBeInTheDocument();
  });
});