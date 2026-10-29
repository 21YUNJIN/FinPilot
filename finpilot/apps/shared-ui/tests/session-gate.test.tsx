import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { SessionGate } from "../src/components/SessionGate";

describe("SessionGate 登录闸门", () => {
  it("未认证时显示登录表单，提交后回传账号密码", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn().mockResolvedValue(null);
    render(
      <SessionGate authenticated={false} channel="web" onLogin={onLogin}>
        <p>已登录内容</p>
      </SessionGate>,
    );

    await user.type(screen.getByTestId("login-user-id"), "user_demo_01");
    await user.type(screen.getByTestId("login-password"), "pw-123456");
    await user.click(screen.getByTestId("login-submit"));

    expect(onLogin).toHaveBeenCalledWith({ user_id: "user_demo_01", password: "pw-123456" });
    expect(screen.queryByText("已登录内容")).toBeNull();
    expect(screen.getByText(/身份只来自服务端会话/)).toBeInTheDocument();
  });

  it("登录失败展示后端安全文案（不显示泛泛的'登录失败'）", async () => {
    const user = userEvent.setup();
    const onLogin = vi.fn().mockResolvedValue({
      code: "UNAUTHENTICATED",
      message: "登录状态无效，请重新登录。",
      hint: "请重新登录或重新完成验证。",
      retryMode: "after_reauthorization",
      knownCode: true,
      requiresQueryOfOriginalOperation: false,
      httpStatus: 401,
      status: "failed",
      reasonCodes: [],
      details: null,
      requestId: null,
      operationId: null,
      envelope: null,
      transport: false,
    });
    render(
      <SessionGate authenticated={false} channel="im" onLogin={onLogin}>
        <p>已登录内容</p>
      </SessionGate>,
    );

    await user.type(screen.getByTestId("login-user-id"), "user_demo_01");
    await user.type(screen.getByTestId("login-password"), "wrong");
    await user.click(screen.getByTestId("login-submit"));

    expect(screen.getByTestId("error-banner")).toHaveTextContent("登录状态无效，请重新登录。");
    expect(screen.getByTestId("error-banner")).toHaveTextContent("UNAUTHENTICATED");
  });

  it("已认证时渲染业务内容，并在登录前如实标注运行模式", () => {
    const { unmount } = render(
      <SessionGate authenticated channel="web" onLogin={vi.fn()}>
        <p>已登录内容</p>
      </SessionGate>,
    );
    expect(screen.getByText("已登录内容")).toBeInTheDocument();
    unmount();

    render(
      <SessionGate authenticated={false} channel="web" mode={null} onLogin={vi.fn()}>
        <p>已登录内容</p>
      </SessionGate>,
    );
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("运行模式未标注");
    expect(screen.getByTestId("mode-badge")).toHaveTextContent("不得当作实时运行");
  });
});