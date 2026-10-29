/**
 * 前端本地追踪（仅内存）。
 *
 * 安全约束（A03、SEC-04）：
 * - 验证码、密码、token 一律不写入这里，也不写入任何日志或会话存储。
 * - 这里只记录脱敏后的事件名与标识（operation_id / task_id），用于用户界面上的"最近动作"，
 *   便于版本冲突时说明"你已确认过什么"。
 */

export interface TraceEvent {
  at: string;
  name: string;
  detail?: string;
}

const MAX_EVENTS = 50;
const events: TraceEvent[] = [];

export function traceEvent(name: string, detail?: string): void {
  events.push({ at: new Date().toISOString(), name, detail });
  if (events.length > MAX_EVENTS) events.splice(0, events.length - MAX_EVENTS);
}

export function traceEvents(): readonly TraceEvent[] {
  return events;
}

export function clearClientTrace(): void {
  events.length = 0;
}