import { describe, expect, it } from "vitest";
import { cacheKey, cacheKeys, resourceCache } from "../src/api/cache";
import {
  clearSessionCache,
  getActiveUserId,
  getAuthToken,
  restoreSession,
  setSession,
  sessionGeneration,
  __resetSessionForTests,
} from "../src/api/session";

describe("会话缓存隔离（换用户必须清缓存）", () => {
  it("缓存按用户归属校验：另一个用户读不到上一个用户的数据", () => {
    resourceCache.set(cacheKeys.cards(), "user_demo_01", { cards: ["card_a"] });
    expect(resourceCache.get(cacheKeys.cards(), "user_demo_01")).toEqual({ cards: ["card_a"] });
    expect(resourceCache.get(cacheKeys.cards(), "user_demo_02")).toBeUndefined();
    expect(resourceCache.has(cacheKeys.cards(), "user_demo_02")).toBe(false);
  });

  it("切换用户会清空缓存并递增 generation（避免旧请求渲染到新用户）", () => {
    setSession({ token: "tok_1", userId: "user_demo_01", channel: "web" });
    resourceCache.set(cacheKeys.taskView("task_1"), "user_demo_01", { task_id: "task_1", revision: 3 });
    const firstGeneration = sessionGeneration();

    setSession({ token: "tok_2", userId: "user_demo_02", channel: "web" });

    expect(getActiveUserId()).toBe("user_demo_02");
    expect(sessionGeneration()).toBeGreaterThan(firstGeneration);
    expect(resourceCache.size()).toBe(0);
    expect(resourceCache.get(cacheKeys.taskView("task_1"), "user_demo_01")).toBeUndefined();
  });

  it("同一用户重新登录（token 续期）不清空自己的缓存", () => {
    setSession({ token: "tok_1", userId: "user_demo_01", channel: "web" });
    resourceCache.set(cacheKeys.taskView("task_1"), "user_demo_01", { task_id: "task_1" });
    setSession({ token: "tok_1b", userId: "user_demo_01", channel: "web" });
    expect(resourceCache.get(cacheKeys.taskView("task_1"), "user_demo_01")).toEqual({ task_id: "task_1" });
  });

  it("clearSessionCache 清空 token 与全部缓存（401/登出路径）", () => {
    setSession({ token: "tok_1", userId: "user_demo_01", channel: "web" });
    resourceCache.set(cacheKeys.subscriptions(), "user_demo_01", { subscriptions: [] });
    clearSessionCache("http_401");
    expect(getAuthToken()).toBeNull();
    expect(getActiveUserId()).toBeNull();
    expect(resourceCache.size()).toBe(0);
  });

  it("会话可从 sessionStorage 恢复；已过期的会话不会恢复", () => {
    setSession({
      token: "tok_persist",
      userId: "user_demo_01",
      channel: "im",
      expiresAt: "2026-10-27T09:00:00Z",
    });
    const raw = sessionStorage.getItem("finpilot.session.v1");
    expect(raw).toBeTruthy();

    // 模拟"页面刷新"：内存态清空，但 sessionStorage 仍保留（这里手动还原被测试清理掉的键）
    __resetSessionForTests();
    sessionStorage.setItem("finpilot.session.v1", String(raw));

    const restored = restoreSession(Date.parse("2026-10-27T08:00:00Z"));
    expect(restored?.userId).toBe("user_demo_01");
    expect(restored?.channel).toBe("im");

    // 超过 expires_at 后不再恢复
    __resetSessionForTests();
    sessionStorage.setItem("finpilot.session.v1", String(raw));
    expect(restoreSession(Date.parse("2026-10-27T10:00:00Z"))).toBeNull();
  });

  it("cacheKey 过滤空值，键稳定可复现", () => {
    expect(cacheKey("task", "task_1", null, undefined, "")).toBe("task:task_1");
    expect(cacheKeys.billReport("2026-10-01", "2026-10-31")).toBe("bills:2026-10-01:2026-10-31");
  });
});