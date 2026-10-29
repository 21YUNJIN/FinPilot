import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ApiFailure, ApiResult } from "../api/client";
import { resourceCache } from "../api/cache";
import { getActiveUserId, onSessionChange } from "../api/session";

/**
 * 只读资源 Hook（卡片 / 账单 / 理财 / 订阅 / 隐私留存等共用）。
 *
 * 规则：
 * - 缓存按用户隔离：切换用户后读不到上一个用户的缓存（`resourceCache` 校验 owner）；
 * - 用户变化时丢弃在途响应并清空本地数据；
 * - `cache: false` 用于隐私等不应缓存的数据。
 */
export interface UseResourceOptions<T> {
  key: string | null;
  loader: (signal: AbortSignal) => Promise<ApiResult<T>>;
  enabled?: boolean;
  cache?: boolean;
}

export interface UseResourceResult<T> {
  data: T | null;
  loading: boolean;
  error: ApiFailure | null;
  refresh: () => Promise<void>;
}

export function useResource<T>(options: UseResourceOptions<T>): UseResourceResult<T> {
  const { key, loader, enabled = true, cache = true } = options;
  const [userId, setUserId] = useState<string | null>(() => getActiveUserId());
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);
  const loaderRef = useRef(loader);
  loaderRef.current = loader;
  const generationRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => onSessionChange((session) => setUserId(session?.userId ?? null)), []);

  const load = useCallback(async (): Promise<void> => {
    if (!key || !enabled) return;
    const generation = generationRef.current;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    setLoading(true);
    const result = await loaderRef.current(controller.signal);
    if (generation !== generationRef.current) return;
    setLoading(false);
    if (!result.ok) {
      if (result.error.code === "REQUEST_ABORTED") return;
      setError(result.error);
      return;
    }
    setError(null);
    setData(result.data);
    const cacheUser = getActiveUserId();
    if (cache && cacheUser) resourceCache.set(key, cacheUser, result.data);
  }, [cache, enabled, key]);

  useEffect(() => {
    generationRef.current += 1;
    abortRef.current?.abort();
    setData(null);
    setError(null);
    if (!key || !enabled) {
      setLoading(false);
      return undefined;
    }
    const cacheUser = getActiveUserId();
    const cached = cache ? resourceCache.get<T>(key, cacheUser) : undefined;
    if (cached !== undefined) {
      setData(cached);
      setLoading(false);
      return undefined;
    }
    void load();
    return () => abortRef.current?.abort();
  }, [key, enabled, cache, load, userId]);

  const refresh = useCallback(async (): Promise<void> => {
    await load();
  }, [load]);

  return useMemo(() => ({ data, loading, error, refresh }), [data, loading, error, refresh]);
}