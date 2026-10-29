import { describe, expect, it } from "vitest";
import {
  CLIENT_ERROR_MESSAGES,
  ERROR_MESSAGES,
  ERROR_MIRROR,
  errorMessageFor,
  isKnownErrorCode,
  resolveUserMessage,
  retryModeHint,
} from "../src/api/errorMessages";

describe("错误码 → 用户可读文案", () => {
  it("已登记错误码返回 errors.py 的安全文案（逐字）", () => {
    expect(errorMessageFor("RESULT_UNKNOWN")).toBe("结果未知，正在用原操作编号核对，请勿重复提交。");
    expect(errorMessageFor("VERSION_CONFLICT")).toBe("对象已被其他操作更新，请刷新后重试。");
    expect(errorMessageFor("PROVIDER_TIMEOUT")).toBe("请求结果尚未确认，正在查询原订单。");
    expect(errorMessageFor("CHALLENGE_LOCKED")).toBe("验证尝试次数过多，请稍后再试。");
    expect(errorMessageFor("INSUFFICIENT_FUNDS")).toBe("可用余额不足。");
    expect(isKnownErrorCode("RESULT_UNKNOWN")).toBe(true);
  });

  it("未登记错误码不猜测成功，提示以服务端为准并保留原码", () => {
    const message = errorMessageFor("SOMETHING_NEW");
    expect(message).toContain("SOMETHING_NEW");
    expect(message).not.toContain("成功");
    expect(isKnownErrorCode("SOMETHING_NEW")).toBe(false);
  });

  it("结果未知类错误要求查询原订单而不是重复提交", () => {
    const resolved = resolveUserMessage({ code: "RESULT_UNKNOWN" });
    expect(resolved.requiresQueryOfOriginalOperation).toBe(true);
    expect(resolved.hint).toContain("原操作编号");
    expect(resolved.retryMode).toBe("query_same_operation");

    const timeout = resolveUserMessage({ code: "REQUEST_TIMEOUT" });
    expect(timeout.requiresQueryOfOriginalOperation).toBe(true);
    expect(timeout.message).toContain("结果未知");
  });

  it("后端 safe_message 优先于前端镜像", () => {
    const resolved = resolveUserMessage({
      code: "PARAM_INVALID",
      serverMessage: "收款账号格式不正确（服务端口径）。",
      retryMode: "after_input_fix",
    });
    expect(resolved.message).toBe("收款账号格式不正确（服务端口径）。");
    expect(resolved.knownCode).toBe(true);
  });

  it("retry_mode 映射为可执行的下一步提示，未知值不伪造文案", () => {
    expect(retryModeHint("never")).toContain("不可重试");
    expect(retryModeHint("query_same_operation")).toContain("原操作编号");
    expect(retryModeHint("after_reauthorization")).toContain("重新登录");
    expect(retryModeHint("whatever")).toContain("未知的重试方式");
  });

  it("镜像表与客户端错误码分离，客户端码不与后端契约混淆", () => {
    expect(Object.keys(ERROR_MESSAGES)).toHaveLength(Object.keys(ERROR_MIRROR).length);
    expect(ERROR_MIRROR.NETWORK_ERROR).toBeUndefined();
    expect(CLIENT_ERROR_MESSAGES.NETWORK_ERROR).toContain("原操作编号");
    expect(CLIENT_ERROR_MESSAGES.CLIENT_MISSING_EXPECTED_REVISION).toContain("expected_revision");
  });
});