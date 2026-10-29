import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ModeBadge } from "../src/components/ModeBadge";
import { StatusChip } from "../src/components/StatusChip";

describe("ModeBadge 运行模式标识（NFR-04）", () => {
  it("live / offline_replay / mock 分别有明确文字标识", () => {
    const { unmount } = render(<ModeBadge mode="live" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("实时运行（live）");
    expect(screen.getByTestId("mode-badge")).toHaveAttribute("data-mode", "live");
    unmount();

    render(<ModeBadge mode="offline_replay" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("离线重放");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("非实时");
  });

  it("mock 模式必须写明非真实资金", () => {
    render(<ModeBadge mode="mock" adapter="bank-sandbox" observedAt="2026-10-27T08:00:00Z" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("模拟环境");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("非真实资金");
    expect(screen.getByTestId("mode-badge").getAttribute("title")).toContain("bank-sandbox");
  });

  it("模式缺失或非法值一律不得当作实时运行", () => {
    const { unmount } = render(<ModeBadge mode={null} />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("运行模式未标注");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("不得当作实时运行");
    expect(screen.getByTestId("mode-badge")).toHaveAttribute("data-mode", "unlabeled");
    unmount();

    render(<ModeBadge mode="staging" />);
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("未知运行模式（staging）");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("不得当作实时运行");
  });
});

describe("StatusChip 状态标签（不得只用颜色）", () => {
  it("渲染可见文字标签与读屏名称，颜色只作辅助", () => {
    render(<StatusChip label="部分成功" tone="warning" srPrefix="任务状态" rawValue="partially_succeeded" />);
    const chip = screen.getByLabelText("任务状态：部分成功");
    expect(chip).toBeInTheDocument();
    expect(chip).toHaveTextContent("部分成功");
    expect(chip).toHaveAttribute("data-tone", "warning");
    expect(chip).toHaveAttribute("data-raw-status", "partially_succeeded");
    expect(chip).toHaveAttribute("title", "后端原始状态：partially_succeeded");
  });

  it("风险状态即使使用危险色也必须带文字，空标签不会被渲染成空白", () => {
    const { unmount } = render(<StatusChip label="高风险 R5" tone="danger" srPrefix="风险等级" />);
    expect(screen.getByText("高风险 R5")).toBeInTheDocument();
    expect(screen.getByLabelText("风险等级：高风险 R5")).toBeInTheDocument();
    unmount();

    render(<StatusChip label="   " />);
    expect(screen.getByText("状态未标注")).toBeInTheDocument();
  });
});