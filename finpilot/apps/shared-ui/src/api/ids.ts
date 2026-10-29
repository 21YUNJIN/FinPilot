/**
 * 客户端生成的稳定标识。
 *
 * 契约（INTERFACES.md §1、V5 8.3）：对外稳定 ID 形如 `op_xxx`、`task_xxx`；同一逻辑操作的
 * `operation_id` 在网络重试、调度重领、人工重查中必须保持不变——客户端生成的幂等键就是它。
 */

const HEX = "0123456789abcdef";

function randomHex(length: number): string {
  const bytes = new Uint8Array(Math.ceil(length / 2));
  const cryptoObj = globalThis.crypto;
  if (cryptoObj && typeof cryptoObj.getRandomValues === "function") {
    cryptoObj.getRandomValues(bytes);
  } else {
    for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  }
  let out = "";
  for (const byte of bytes) out += byte.toString(16).padStart(2, "0");
  return out.slice(0, length);
}

function timePart(): string {
  // 时间戳只用 base36 缩短长度，不参与唯一性保证。
  return Date.now().toString(36);
}

/** 生成逻辑操作 ID（客户端幂等键），形如 `op_<时间><随机>`，长度 ≤ 48。 */
export function newOperationId(): string {
  return `op_${timePart()}${randomHex(12)}`;
}

/** 生成客户端本地动作 ID（不提交给后端，用于冲突提示里标记"已确认动作"）。 */
export function newClientActionId(): string {
  return `act_${timePart()}${randomHex(8)}`;
}

/** 生成请求追踪 ID：仅用于前端本地日志定位；不伪造服务端 request_id。 */
export function newClientTraceId(): string {
  return `cli_${timePart()}${randomHex(8)}`;
}

/** 校验是否为后端风格的稳定 ID。 */
export function isStableId(value: string | null | undefined, prefix?: string): boolean {
  if (!value) return false;
  const pattern = prefix ? new RegExp(`^${prefix}_[a-z0-9]+$`) : /^[a-z]+_[a-z0-9]+$/;
  return pattern.test(value);
}

export const ID_HEX_ALPHABET = HEX;