import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ChallengeDialog } from "../src/components/ChallengeDialog";

afterEach(() => {
  vi.useRealTimers();
});

describe("ChallengeDialog 强验证对话框", () => {
  it("验证码独立提交：回调收到验证码，提交后不残留在页面文本中", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn().mockResolvedValue(undefined);
    const { container } = render(
      <ChallengeDialog
        challengeId="chal_1"
        expiresAt={new Date(Date.now() + 120_000).toISOString()}
        onSubmit={onSubmit}
        purpose="补卡"
      />,
    );

    await user.type(screen.getByTestId("challenge-code-input"), "123456");
    await user.click(screen.getByTestId("challenge-submit"));

    expect(onSubmit).toHaveBeenCalledWith("123456");
    expect(screen.getByTestId("challenge-code-input")).toHaveValue("");
    // 验证码不进入对话/日志/页面展示（DOM 中也不应残留）
    expect(container.textContent ?? "").not.toContain("123456");
    expect(screen.getByTestId("challenge-privacy-note")).toHaveTextContent("不会进入对话消息、模型上下文或前端日志");
  });

  it("非 6 位数字不会提交，并提示格式要求", async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn();
    render(<ChallengeDialog challengeId="chal_2" onSubmit={onSubmit} />);

    await user.type(screen.getByTestId("challenge-code-input"), "12ab");
    await user.click(screen.getByTestId("challenge-submit"));

    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.getByRole("alert")).toHaveTextContent("请输入 6 位数字动态验证码。");
  });

  it("已过期时禁止提交并说明需重新发起验证", () => {
    const onSubmit = vi.fn();
    render(
      <ChallengeDialog
        challengeId="chal_expired"
        expiresAt={new Date(Date.now() - 1000).toISOString()}
        onSubmit={onSubmit}
        onReissue={vi.fn()}
      />,
    );

    expect(screen.getByTestId("challenge-expired-message")).toHaveTextContent("验证已过期");
    expect(screen.getByTestId("challenge-code-input")).toBeDisabled();
    expect(screen.getByTestId("challenge-submit")).toBeDisabled();
    expect(screen.getByRole("button", { name: "重新发起验证" })).toBeEnabled();
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("剩余有效期按真实时间倒计时（不伪造进度）", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-10-27T08:00:00Z"));
    render(<ChallengeDialog challengeId="chal_timer" expiresAt="2026-10-27T08:02:00Z" onSubmit={vi.fn()} />);

    expect(screen.getByTestId("challenge-expiry")).toHaveTextContent("剩余 02:00");
    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByTestId("challenge-expiry")).toHaveTextContent("剩余 01:00");
    // 组件不渲染进度条，只有真实剩余时间文字
    expect(screen.queryByRole("progressbar")).toBeNull();
  });

  it("尝试次数用尽时显示锁定提示并禁止提交", () => {
    render(
      <ChallengeDialog
        challengeId="chal_locked"
        attemptsRemaining={0}
        lockedUntil={new Date(Date.now() + 900_000).toISOString()}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByTestId("challenge-locked")).toHaveTextContent("尝试次数过多，已锁定至");
    expect(screen.getByTestId("challenge-attempts")).toHaveTextContent("剩余 0 / 5 次");
    expect(screen.getByTestId("challenge-submit")).toBeDisabled();
  });

  it("服务端返回锁定/失败原因时展示后端文案并保留取消入口", async () => {
    const user = userEvent.setup();
    const onCancel = vi.fn();
    render(
      <ChallengeDialog
        challengeId="chal_err"
        serverError="验证尝试次数过多，请稍后再试。"
        serverErrorCode="CHALLENGE_LOCKED"
        onSubmit={vi.fn()}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByTestId("error-banner")).toHaveTextContent("验证尝试次数过多，请稍后再试。");
    await user.click(screen.getByRole("button", { name: "取消（不提交任何操作）" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});