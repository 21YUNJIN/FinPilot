/**
 * 会话状态（token / 当前用户）与缓存清理入口。
 *
 * 需求（A01、SEC-04）：
 * - 身份只来自服务端会话（INTERFACES.md §1）：前端不解析 token、不推断 user_id。
 * - 切换用户必须清空上一个用户的缓存（另一个用户看不到上一个用户的数据）。
 * - 401 → clearSessionCache()；登出、换用户同理。
 * - token 只保存在内存 + 当前标签页的 sessionStorage；不写 localStorage、不进日志。
 */

import { resourceCache } from "./cache";
import { traceEvent } from "./trace";

export interface SessionRecord {
  token: string;
  userId: string;
  displayName?: string;
  roles?: string[];
  expiresAt?: string;
  channel: string;
}

const STORAGE_KEY = "finpilot.session.v1";

let current: SessionRecord | null = null;
let generation = 0;

const listeners = new Set<(session: SessionRecord | null, generation: number) => void>();

function notify(): void {
  for (const listener of listeners) listener(current, generation);
}

function storage(): Storage | null {
  try {
    if (typeof sessionStorage === "undefined") return null;
    return sessionStorage;
  } catch {
    return null;
  }
}

function persist(record: SessionRecord | null): void {
  const store = storage();
  if (!store) return;
  try {
    if (!record) {
      store.removeItem(STORAGE_KEY);
      return;
    }
    // 只持久化恢复会话所需的最小信息；不存密码、不存验证码。
    store.setItem(
      STORAGE_KEY,
      JSON.stringify({
        token: record.token,
        userId: record.userId,
        displayName: record.displayName ?? "",
        roles: record.roles ?? [],
        expiresAt: record.expiresAt ?? "",
        channel: record.channel,
      }),
    );
  } catch {
    /* 存储不可用时静默降级为纯内存会话 */
  }
}

export function getSession(): SessionRecord | null {
  return current;
}

export function getActiveUserId(): string | null {
  return current?.userId ?? null;
}

export function getAuthToken(): string | null {
  return current?.token ?? null;
}

export function isAuthenticated(): boolean {
  return current !== null;
}

export function sessionGeneration(): number {
  return generation;
}

/**
 * 建立会话。切换到不同用户时先清空缓存并递增 generation，
 * 保证旧用户的在途请求/视图不会渲染到新用户界面。
 */
export function setSession(record: SessionRecord, options: { keepCache?: boolean } = {}): void {
  const previousUser = current?.userId ?? null;
  const userChanged = previousUser !== null && previousUser !== record.userId;
  current = { ...record };
  if (userChanged && !options.keepCache) {
    resourceCache.clear(`user_switch:${previousUser}->${record.userId}`);
    traceEvent("session.user_switch", `->${record.userId}`);
  }
  generation += 1;
  persist(current);
  notify();
}

/** 更新 token（同一用户续期），不清理缓存。 */
export function refreshToken(token: string, expiresAt?: string): void {
  if (!current) return;
  current = { ...current, token, expiresAt: expiresAt ?? current.expiresAt };
  generation += 1;
  persist(current);
  notify();
}

/** 清除会话与全部会话级缓存。401、登出、换用户都走这里。 */
export function clearSessionCache(reason = "manual"): void {
  const hadSession = current !== null;
  current = null;
  generation += 1;
  const cleared = resourceCache.clear(reason);
  persist(null);
  traceEvent("session.clear", `${reason}; cache_cleared=${cleared}`);
  if (hadSession || cleared > 0) notify();
}

/** 从 sessionStorage 恢复会话（刷新页面后）；过期则直接清理。 */
export function restoreSession(now: number = Date.now()): SessionRecord | null {
  const store = storage();
  if (!store) return null;
  try {
    const raw = store.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<SessionRecord>;
    if (!parsed.token || !parsed.userId) {
      store.removeItem(STORAGE_KEY);
      return null;
    }
    if (parsed.expiresAt) {
      const expiresAt = new Date(parsed.expiresAt).getTime();
      if (Number.isFinite(expiresAt) && expiresAt <= now) {
        store.removeItem(STORAGE_KEY);
        return null;
      }
    }
    const record: SessionRecord = {
      token: parsed.token,
      userId: parsed.userId,
      displayName: parsed.displayName,
      roles: parsed.roles ?? [],
      expiresAt: parsed.expiresAt,
      channel: parsed.channel ?? "web",
    };
    current = record;
    generation += 1;
    notify();
    return record;
  } catch {
    store.removeItem(STORAGE_KEY);
    return null;
  }
}

export function onSessionChange(listener: (session: SessionRecord | null, generation: number) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 测试与"换用户"流程用：完全重置内存态（不动 sessionStorage 之外的东西）。 */
export function __resetSessionForTests(): void {
  current = null;
  generation = 0;
  listeners.clear();
  const store = storage();
  try {
    store?.removeItem(STORAGE_KEY);
  } catch {
    /* ignore */
  }
}