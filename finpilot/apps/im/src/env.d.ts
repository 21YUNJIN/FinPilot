/// <reference types="vitest/globals" />

/**
 * 环境变量声明（与 shared-ui/src/vite-env.d.ts 一致）。
 * base URL 唯一来源：`import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:58000"`。
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