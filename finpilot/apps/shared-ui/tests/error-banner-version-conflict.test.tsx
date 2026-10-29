import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ErrorBanner } from "../src/components/ErrorBanner";
import { VersionConflictNotice } from "../src/components/VersionConflictNotice";
import type { ApiFailure } from "../src/api/client";
import type { LocalConfirmedAction } from "../src/types";

const resultUnknown: ApiFailure = {
  code: "RESULT_UNKNOWN",
  message: "结果未知，正在用原操作编号核对，请勿重复提交。",
  hint: "请使用原操作编号查询结果，不要重复提交。",
  retryMode: "query_same_operation",
  knownCode: true,
  requiresQueryOfOriginalOperation: true,
  httpStatus: 503,
  status: "unknown",
  reasonCodes: ["provider_timeout"],
  details: null,
  requestId: "req_1",
  operationId: "op_9f2c41",
  envelope: null,
  transport: false,
};

describe("ErrorBanner 错误提示", () => {
  it("结果未知不写成失败，并给出查询原订单的动作", async () => {
    const onRetry = vi.fn();
    render(<ErrorBanner failure={resultUnknown} onRetry={onRetry} context="转账提交" />);
    expect(screen.getByTestId("error-banner")).toHaveTextContent("结果未知（未确认成功，也未确认失败）");
    expect(screen.getByTestId("error-banner")).toHaveTextContent("请勿重复提交");
    expect(screen.getByText("op_9f2c41")).toBeInTheDocument();
    const button = screen.getByRole("button", { name: "用原操作编号查询" });
    await button.click();
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("普通失败展示错误码、文案与重试提示", () => {
    render(<ErrorBanner code="INSUFFICIENT_FUNDS" />);
    expect(screen.getByTestId("error-banner")).toHaveTextContent("可用余额不足。");
    expect(screen.getByTestId("error-banner")).toHaveTextContent("INSUFFICIENT_FUNDS");
    expect(screen.getByTestId("error-banner")).toHaveTextContent("请修正输入或刷新后重试。");
  });

  it("传输层失败说明请求未到达业务层，不暗示业务结果", () => {
    render(
      <ErrorBanner
        failure={{
          ...resultUnknown,
          code: "NETWORK_ERROR",
          message: "无法连接到服务端，本次请求未获得业务结果；若为写操作请用原操作编号查询。",
          hint: "该情况不可重试，请不要重复提交。",
          retryMode: "never",
          status: null,
          transport: true,
        }}
      />,
    );
    expect(screen.getByTestId("error-banner")).toHaveTextContent("请求未到达业务层");
    expect(screen.getByTestId("error-banner")).toHaveTextContent("无法连接到服务端");
  });
});

const pending: LocalConfirmedAction[] = [
  {
    actionId: "act_1",
    kind: "confirm_plan",
    label: "已确认计划 r5 的授权（黄色等级）",
    at: "2026-10-27T08:00:05Z",
    expectedRevision: 5,
    operationId: "op_confirm_1",
  },
];

describe("VersionConflictNotice 版本冲突", () => {
  it("提示旧修订、服务端修订，并提供刷新按钮", async () => {
    const onRefresh = vi.fn();
    render(
      <VersionConflictNotice
        conflict={{
          code: "VERSION_CONFLICT",
          message: "对象已被其他操作更新，请刷新后重试。",
          expectedRevision: 5,
          serverRevision: 7,
          detectedAt: "2026-10-27T08:00:10Z",
        }}
        onRefresh={onRefresh}
        pendingActions={pending}
      />,
    );
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("你基于修订 r5");
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("服务端当前修订：r7");
    await screen.getByRole("button", { name: "刷新到最新修订" }).click();
    expect(onRefresh).toHaveBeenCalledTimes(1);
  });

  it("明确说明刷新不会丢弃已确认动作，并逐条列出被保留的动作", () => {
    render(
      <VersionConflictNotice
        conflict={{
          code: "VERSION_CONFLICT",
          message: "对象已被其他操作更新，请刷新后重试。",
          expectedRevision: 5,
          serverRevision: null,
          detectedAt: "2026-10-27T08:00:10Z",
        }}
        onRefresh={vi.fn()}
        pendingActions={pending}
      />,
    );
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("不会丢弃你已确认的动作");
    const list = screen.getByTestId("version-conflict-pending-actions");
    expect(list).toHaveTextContent("已确认计划 r5 的授权（黄色等级）");
    expect(list).toHaveTextContent("op_confirm_1");
    expect(screen.getByTestId("version-conflict-notice")).toHaveTextContent("具体版本以刷新结果为准");
  });

  it("刷新中禁用按钮并给出进行中文案", () => {
    render(
      <VersionConflictNotice
        conflict={{
          code: "VERSION_CONFLICT",
          message: "对象已被其他操作更新，请刷新后重试。",
          expectedRevision: 1,
          serverRevision: 2,
          detectedAt: "2026-10-27T08:00:10Z",
        }}
        onRefresh={vi.fn()}
        refreshing
      />,
    );
    const button = screen.getByRole("button", { name: "正在刷新…" });
    expect(button).toBeDisabled();
  });
});