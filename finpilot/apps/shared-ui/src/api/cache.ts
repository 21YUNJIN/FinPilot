/**
 * 会话级只读缓存（含用户隔离）。
 *
 * 需求（A01 验收、SEC-04、NFR-03）：
 * - 每个缓存条目都记录归属用户；读取时若 owner 不匹配则视为未命中（另一个用户看不到上一个用户的数据）。
 * - 切换用户 / 收到 401 / 主动登出时清空缓存，避免串户与过期数据。
 * - 缓存只存只读视图（任务视图、卡片、账单等），不存资金结论，也不代替服务端查询。
 */

import { clearClientTrace } from "./trace";

export interface CacheEntry<T> {
  userId: string;
  key: string;
  value: T;
  storedAt: number;
}

export interface CacheClearEvent {
  reason: string;
  at: string;
  size: number;
}

export class UserScopedCache {
  private readonly store = new Map<string, CacheEntry<unknown>>();
  private readonly listeners = new Set<(event: CacheClearEvent) => void>();

  constructor(private readonly ttlMs: number = 5 * 60 * 1000) {}

  get<T>(key: string, userId: string | null | undefined): T | undefined {
    const entry = this.store.get(key);
    if (!entry) return undefined;
    if (!userId || entry.userId !== userId) return undefined;
    if (this.ttlMs > 0 && Date.now() - entry.storedAt > this.ttlMs) {
      this.store.delete(key);
      return undefined;
    }
    return entry.value as T;
  }

  set<T>(key: string, userId: string, value: T): void {
    this.store.set(key, { userId, key, value, storedAt: Date.now() });
  }

  has(key: string, userId: string | null | undefined): boolean {
    return this.get(key, userId) !== undefined;
  }

  /** 清空所有条目（切换用户、登出、401）。 */
  clear(reason: string): number {
    const size = this.store.size;
    this.store.clear();
    clearClientTrace();
    const event: CacheClearEvent = { reason, at: new Date().toISOString(), size };
    for (const listener of this.listeners) listener(event);
    return size;
  }

  /** 只清除某个用户的条目（例如该用户重新登录）。 */
  clearUser(userId: string, reason = "user_reset"): number {
    let removed = 0;
    for (const [key, entry] of this.store) {
      if (entry.userId === userId) {
        this.store.delete(key);
        removed += 1;
      }
    }
    if (removed > 0) {
      const event: CacheClearEvent = { reason, at: new Date().toISOString(), size: removed };
      for (const listener of this.listeners) listener(event);
    }
    return removed;
  }

  size(): number {
    return this.store.size;
  }

  keys(): string[] {
    return [...this.store.keys()];
  }

  owners(): string[] {
    return [...new Set([...this.store.values()].map((entry) => entry.userId))];
  }

  onClear(listener: (event: CacheClearEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }
}

/** 全局只读视图缓存：两个客户端共用同一进程内的实例（同标签页）。 */
export const resourceCache = new UserScopedCache();

export function cacheKey(...parts: Array<string | number | null | undefined>): string {
  return parts.filter((part) => part !== null && part !== undefined && part !== "").join(":");
}

export function clearAllCaches(reason: string): void {
  resourceCache.clear(reason);
}

export const cacheKeys = {
  taskView: (taskId: string) => cacheKey("task", taskId),
  taskList: () => cacheKey("tasks"),
  cards: () => cacheKey("cards"),
  billReport: (from?: string, to?: string) => cacheKey("bills", from, to),
  wealthProducts: (riskLevel?: string) => cacheKey("wealth", riskLevel),
  subscriptions: () => cacheKey("subscriptions"),
  operations: () => cacheKey("operations"),
  snapshot: (accountIds?: string[]) => cacheKey("snapshot", accountIds?.join("+")),
  privacyRetention: () => cacheKey("privacy", "retention"),
} as const;