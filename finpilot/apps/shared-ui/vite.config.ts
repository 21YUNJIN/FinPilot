import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

// shared-ui 自身不做应用打包，这里只承载 vitest 配置（jsdom + Testing Library）。
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}", "src/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    clearMocks: true,
  },
});