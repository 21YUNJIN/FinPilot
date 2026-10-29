/**
 * 前端只读镜像与后端源码的一致性校验（不是伪造的"文案测试"）。
 *
 * 直接读取 Python 源码：
 * - services/api/src/finpilot/errors.py 的 ERROR_SPECS（码、HTTP 语义、retry_mode、safe_message）
 * - services/api/src/finpilot/models.py 顶部的状态常量元组
 *
 * 任何一处漂移都会让测试失败，从而迫使前端镜像与后端同步（INTERFACES.md §1 的唯一来源原则）。
 * 如果仓库中没有 Python 侧代码（例如只检出前端），测试会跳过并打印原因，而不是假装通过。
 */

import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { ERROR_MIRROR } from "../src/api/errorMessages";
import {
  CARD_APPLICATION_LABELS,
  CARD_LIFECYCLE_LABELS,
  GRANT_STATUS_LABELS,
  JOB_STATE_LABELS,
  MANDATE_STATUS_LABELS,
  OPERATION_STATUS_LABELS,
  SUBSCRIPTION_STATUS_LABELS,
  TASK_STATUS_LABELS,
  WEALTH_ORDER_STATUS_LABELS,
} from "../src/contract/labels";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "../../..");
const errorsPath = resolve(repoRoot, "services/api/src/finpilot/errors.py");
const modelsPath = resolve(repoRoot, "services/api/src/finpilot/models.py");

const hasPythonSources = existsSync(errorsPath) && existsSync(modelsPath);

interface ParsedErrorSpec {
  code: string;
  httpStatus: number;
  retryMode: string;
  safeMessage: string;
}

function parseRetryConstants(source: string): Map<string, string> {
  const map = new Map<string, string>();
  // 行尾可能带注释（例如 "# 必须用原 operation_id 查询"），所以不锚定行尾。
  const regex = /^(RETRY_[A-Z_]+) = "([a-z_]+)"/gm;
  let match: RegExpExecArray | null;
  while ((match = regex.exec(source)) !== null) {
    map.set(match[1] as string, match[2] as string);
  }
  return map;
}

function parseErrorSpecs(source: string): ParsedErrorSpec[] {
  const retryConstants = parseRetryConstants(source);
  const blockMatch = source.match(/ERROR_SPECS: dict\[str, ErrorSpec\] = \{([\s\S]*?)\n\}/);
  if (!blockMatch) throw new Error("无法在 errors.py 中定位 ERROR_SPECS 块");
  const block = blockMatch[1] as string;
  const specs: ParsedErrorSpec[] = [];
  const lineRegex = /"([A-Z_]+)":\s*ErrorSpec\(\s*(\d+)\s*,\s*(RETRY_[A-Z_]+)\s*,\s*"([^"]*)"\s*\)/g;
  let match: RegExpExecArray | null;
  while ((match = lineRegex.exec(block)) !== null) {
    const [, code, httpStatus, retryConstant, safeMessage] = match;
    const retryMode = retryConstants.get(retryConstant as string);
    if (!retryMode) throw new Error(`未知的 retry 常量 ${retryConstant}`);
    specs.push({
      code: code as string,
      httpStatus: Number(httpStatus),
      retryMode,
      safeMessage: safeMessage as string,
    });
  }
  return specs;
}

function parseStatusTuple(source: string, name: string): string[] {
  const match = source.match(new RegExp(`${name} = \\(([\\s\\S]*?)\\)`));
  if (!match) throw new Error(`无法在 models.py 中定位 ${name}`);
  const values = [...(match[1] as string).matchAll(/"([a-z_]+)"/g)].map((item) => item[1] as string);
  if (values.length === 0) throw new Error(`${name} 解析为空`);
  return values;
}

describe.skipIf(!hasPythonSources)("前端镜像 vs 后端源码", () => {
  it("错误码映射与 errors.py::ERROR_SPECS 完全一致（码、HTTP、retry_mode、文案）", () => {
    const source = readFileSync(errorsPath, "utf8");
    const specs = parseErrorSpecs(source);
    expect(specs.length).toBeGreaterThan(30);

    const backendCodes = specs.map((spec) => spec.code).sort();
    const frontendCodes = Object.keys(ERROR_MIRROR).sort();
    expect(frontendCodes).toEqual(backendCodes);

    for (const spec of specs) {
      const mirrored = ERROR_MIRROR[spec.code];
      expect(mirrored, `缺少错误码 ${spec.code}`).toBeTruthy();
      expect(mirrored?.http_status, `${spec.code} 的 HTTP 语义不一致`).toBe(spec.httpStatus);
      expect(mirrored?.retry_mode, `${spec.code} 的 retry_mode 不一致`).toBe(spec.retryMode);
      expect(mirrored?.safe_message, `${spec.code} 的安全文案不一致`).toBe(spec.safeMessage);
    }
  });

  it("状态标签覆盖 models.py 的全部状态常量，且不多不少", () => {
    const source = readFileSync(modelsPath, "utf8");
    const cases: Array<[string, Readonly<Record<string, string>>]> = [
      ["TASK_STATUSES", TASK_STATUS_LABELS],
      ["OPERATION_STATUSES", OPERATION_STATUS_LABELS],
      ["GRANT_STATUSES", GRANT_STATUS_LABELS],
      ["JOB_STATES", JOB_STATE_LABELS],
      ["CARD_LIFECYCLE", CARD_LIFECYCLE_LABELS],
      ["CARD_APPLICATION_STATUS", CARD_APPLICATION_LABELS],
      ["SUBSCRIPTION_STATUS", SUBSCRIPTION_STATUS_LABELS],
      ["MANDATE_STATUS", MANDATE_STATUS_LABELS],
      ["WEALTH_ORDER_STATUS", WEALTH_ORDER_STATUS_LABELS],
    ];

    for (const [tupleName, labels] of cases) {
      const constants = parseStatusTuple(source, tupleName).sort();
      expect(Object.keys(labels).sort(), `${tupleName} 的标签集合与后端常量不一致`).toEqual(constants);
    }
  });
});

if (!hasPythonSources) {
  // 明确告警而不是静默跳过：前端单独检出时无法校验镜像。
  console.warn(
    `[parity] 未找到 Python 源码（${errorsPath}），跳过错误码/状态镜像一致性校验；本次运行不构成镜像一致的证据。`,
  );
}