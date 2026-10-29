/// <reference types="vitest/globals" />

/**
 * 环境变量声明。
 *
 * 契约：`VITE_API_BASE_URL` 是唯一的后端地址来源；未设置时回落到
 * `http://127.0.0.1:58000`（见 src/api/client.ts 的 DEFAULT_API_BASE_URL）。
 * 这里刻意不引入 `vite/client` 类型，避免 shared-ui 反向依赖具体打包器。
 */
interface ImportMetaEnv {
  readonly VITE_API_BASE_URL?: string;
  readonly VITE_APP_CHANNEL?: string;
  readonly MODE?: string;
  readonly DEV?: boolean;
  readonly PROD?: boolean;
}

interface ImportMeta {
  readonly env?: ImportMetaEnv;
}

declare module "*.css";