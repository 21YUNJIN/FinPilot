import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach, beforeEach } from "vitest";
import { resetClientConfig } from "../src/api/client";
import { resourceCache } from "../src/api/cache";
import { __resetSessionForTests } from "../src/api/session";

beforeEach(() => {
  __resetSessionForTests();
  resetClientConfig();
  resourceCache.clear("test-reset");
});

afterEach(() => {
  cleanup();
});