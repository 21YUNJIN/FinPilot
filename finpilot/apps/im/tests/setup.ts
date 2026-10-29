import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { resetClientConfig, resourceCache, setSession, __resetSessionForTests } from "@finpilot/shared-ui";

beforeEach(() => {
  __resetSessionForTests();
  resetClientConfig();
  resourceCache.clear("im-test-reset");
});

afterEach(() => {
  cleanup();
});

/** 测试辅助：模拟已登录会话（生产路径是 SessionGate → POST /v1/auth/sessions）。 */
export function __setSessionForTests(userId = "user_demo_01"): void {
  setSession({
    token: "tok_im",
    userId,
    displayName: "示例用户",
    roles: ["customer"],
    channel: "im",
    expiresAt: "2099-01-01T00:00:00Z",
  });
}