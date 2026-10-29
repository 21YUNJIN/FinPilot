import { useCallback, useEffect, useState } from "react";
import type { ApiFailure } from "../api/client";
import { authApi } from "../api/endpoints";
import {
  clearSessionCache,
  getActiveUserId,
  getSession,
  onSessionChange,
  setSession,
  type SessionRecord,
} from "../api/session";
import type { LoginCredentials } from "../types";

/**
 * 会话 Hook（两个客户端共用）：登录 / 登出 / 会话变化订阅。
 *
 * 规则：
 * - 身份只来自服务端返回的会话；前端不解析 token 内容、不推断 user_id；
 * - 切换用户时 setSession 内部会清空上一个用户的缓存（另一个用户看不到上一个用户的数据）；
 * - 登录失败返回 ApiFailure，由界面显示后端文案（而不是"登录失败"这种无信息提示）。
 */
export interface UseSessionResult {
  session: SessionRecord | null;
  userId: string | null;
  token: string | null;
  isAuthenticated: boolean;
  generation: number;
  channel: string;
  login: (credentials: Omit<LoginCredentials, "channel"> & { channel?: string }) => Promise<ApiFailure | null>;
  logout: () => Promise<void>;
  clear: (reason?: string) => void;
}

export function useSession(defaultChannel = "web"): UseSessionResult {
  const [session, setSessionState] = useState<SessionRecord | null>(() => getSession());
  const [generation, setGeneration] = useState(0);

  useEffect(() => {
    return onSessionChange((next, nextGeneration) => {
      setSessionState(next);
      setGeneration(nextGeneration);
    });
  }, []);

  const login = useCallback<UseSessionResult["login"]>(
    async (credentials) => {
      const channel = credentials.channel ?? defaultChannel;
      const result = await authApi.login({
        user_id: credentials.user_id,
        password: credentials.password,
        channel,
      });
      if (!result.ok) return result.error;
      const data = result.data;
      if (!data || !data.token || !data.user || !data.user.user_id) {
        // 信封 ok 但缺少必要字段：按失败处理，不凭空建立会话。
        return {
          code: "CLIENT_MALFORMED_RESPONSE",
          message: "登录响应缺少 token 或用户信息，未建立会话。",
          hint: "请重试或联系管理员核对网关版本。",
          retryMode: "never",
          knownCode: false,
          requiresQueryOfOriginalOperation: false,
          httpStatus: result.meta.httpStatus,
          status: result.meta.status,
          reasonCodes: [],
          details: null,
          requestId: result.meta.requestId,
          operationId: null,
          envelope: null,
          transport: false,
        } satisfies ApiFailure;
      }
      setSession({
        token: data.token,
        userId: data.user.user_id,
        displayName: data.user.display_name,
        roles: data.user.roles ?? [],
        expiresAt: data.expires_at,
        channel,
      });
      return null;
    },
    [defaultChannel],
  );

  const logout = useCallback(async (): Promise<void> => {
    // OpenAPI 未登记登出接口：前端清除本机会话与用户级缓存即可，
    // 服务端会话由 expires_at 与 401 处理负责失效（不伪造一个不存在的接口调用）。
    clearSessionCache("logout");
  }, []);

  const clear = useCallback((reason = "manual") => clearSessionCache(reason), []);

  return {
    session,
    userId: getActiveUserId(),
    token: session?.token ?? null,
    isAuthenticated: session !== null,
    generation,
    channel: session?.channel ?? defaultChannel,
    login,
    logout,
    clear,
  };
}