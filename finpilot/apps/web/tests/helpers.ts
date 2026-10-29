import { setSession } from "@finpilot/shared-ui";

/**
 * 测试辅助：直接建立会话（模拟刷新页面后从 sessionStorage 恢复的状态）。
 * 只用于测试；生产代码通过 SessionGate → POST /v1/auth/sessions 建立会话。
 */
export function __setSessionForTests(userId = "user_demo_01"): void {
  setSession({
    token: "tok_test",
    userId,
    displayName: "示例用户",
    roles: ["customer"],
    expiresAt: "2099-01-01T00:00:00Z",
    channel: "app",
  });
}