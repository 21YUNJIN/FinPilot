import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import { fileURLToPath } from "node:url";

const sharedUi = fileURLToPath(new URL("../shared-ui/src/index.ts", import.meta.url));
const sharedUiStyles = fileURLToPath(new URL("../shared-ui/src/styles.css", import.meta.url));

export default defineConfig({
  base: "/im/",
  plugins: [react()],
  resolve: {
    alias: {
      "@finpilot/shared-ui/styles.css": sharedUiStyles,
      "@finpilot/shared-ui": sharedUi,
    },
  },
  server: {
    port: 5174,
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
