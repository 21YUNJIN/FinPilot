import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { resetClientConfig, resourceCache, __resetSessionForTests } from "@finpilot/shared-ui";

beforeEach(() => {
  __resetSessionForTests();
  resetClientConfig();
  resourceCache.clear("web-test-reset");
  window.location.hash = "";
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals?.();
});