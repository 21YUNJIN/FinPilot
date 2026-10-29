import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

// shared-ui 通过别名直接引用源码，dev/build 都不需要先构建共享包。
const sharedUi = fileURLToPath(new URL("../shared-ui/src/index.ts", import.meta.url));
const sharedUiStyles = fileURLToPath(new URL("../shared-ui/src/styles.css", import.meta.url));

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  resolve: {
    alias: {
      "@finpilot/shared-ui/styles.css": sharedUiStyles,
      "@finpilot/shared-ui": sharedUi,
    },
  },
  server: {
    port: 5173,
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./tests/setup.ts"],
    include: ["tests/**/*.test.{ts,tsx}"],
    css: false,
    restoreMocks: true,
    clearMocks: true,
  },
});
