import type { ReactElement } from "react";
import type { Plan, PlanStep } from "../types";
import {
  AUTH_LEVEL_SHORT_LABELS,
  isStepInFlight,
  normalizePlanStepStatus,
  PLAN_STEP_STATUS_LABELS,
  planStepStatusLabel,
} from "../contract/labels";
import { formatDateTime } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, toneForStatus } from "./StatusChip";
import { cx } from "../lib/cx";

/**
 * 计划步骤列表（plan.json / plan_step.json；SRS 18.2 / A03）。
 *
 * 规则：
 * - 每步显示**真实状态**：契约状态（pending/running/awaiting_authorization/succeeded/failed/
 *   skipped/unknown/cancelled）归一为 未开始／待确认／验证／执行中／已提交／等待结果／已完成／失败／已停止／结果未知；
 * - 不渲染任何"假进度动画"：只有按真实状态统计的文字与计划级金额合计；
 * - 未知（unknown）步骤必须提示用原操作编号核对，且不得显示成功或失败；
 * - RED 等级步骤在未完成强验证前只显示"待确认"，并提示需要验证；
 * - 步骤参数不做原文展示（typed_args 可能含敏感字段），只显示 write_set / expected_effects 的摘要。
 */
export interface PlanStepsProps {
  plan?: Plan | null;
  /** 兼容直接传步骤（例如只拿到 plan.steps 的场景）。 */
  steps?: PlanStep[];
  headline?: string;
  emptyHint?: string;
  activeStepId?: string | null;
  onConfirmStep?: (step: PlanStep) => void;
  onStopStep?: (step: PlanStep) => void;
  onViewOperation?: (operationId: string) => void;
  className?: string;
  "data-testid"?: string;
}

function stepLevelLabel(step: PlanStep): string | null {
  const level = step.required_level;
  if (!level) return null;
  const key = String(level).toUpperCase();
  return AUTH_LEVEL_SHORT_LABELS[key] ?? `等级 ${level}`;
}

function effectSummary(step: PlanStep): string | null {
  const writeSet = step.write_set ?? [];
  const effects = step.expected_effects ? Object.keys(step.expected_effects) : [];
  const parts = [...writeSet, ...effects].filter((item) => item.trim() !== "");
  return parts.length > 0 ? parts.join("、") : null;
}

export function PlanSteps({
  plan,
  steps,
  headline = "执行步骤",
  emptyHint = "计划尚未生成（服务端返回计划后才会显示步骤）。",
  activeStepId,
  onConfirmStep,
  onStopStep,
  onViewOperation,
  className,
  "data-testid": dataTestId = "plan-steps",
}: PlanStepsProps): ReactElement {
  const items: PlanStep[] = steps ?? plan?.steps ?? [];
  const normalized = items.map((step) => normalizePlanStepStatus(step.status));
  const completed = normalized.filter((status) => status === "succeeded").length;
  const failed = normalized.filter((status) => status === "failed").length;
  const stopped = normalized.filter((status) => status === "stopped").length;
  const inFlight = items.filter((step) => isStepInFlight(step.status)).length;

  return (
    <section className={cx("fp-card", "fp-plan", className)} aria-label={headline} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{headline}</h2>
        {plan ? (
          <>
            <StatusChip
              label={plan.status ? `计划状态：${plan.status}` : "计划状态未提供"}
              tone={toneForStatus(plan.status)}
              srPrefix="计划状态"
              rawValue={plan.status}
            />
            <span className="fp-plan__level">
              计划等级：{AUTH_LEVEL_SHORT_LABELS[String(plan.required_level).toUpperCase()] ?? String(plan.required_level)}
            </span>
            <span className="fp-plan__revision">修订 r{plan.revision}</span>
          </>
        ) : null}
      </header>

      {plan ? (
        <dl className="fp-plan__totals" data-testid="plan-totals">
          <div>
            <dt>预计总额</dt>
            <dd>
              <MoneyText amountMinor={plan.total_estimated_minor} />
            </dd>
          </div>
          <div>
            <dt>预计手续费</dt>
            <dd>
              <MoneyText amountMinor={plan.total_fee_minor} />
            </dd>
          </div>
          <div>
            <dt>可行性</dt>
            <dd data-testid="plan-feasible">
              {plan.feasible ? "在现有约束下找到可行方案" : "不可行（服务端给出原因）"}
              {plan.feasible === false && plan.infeasible_reasons && plan.infeasible_reasons.length > 0
                ? `：${plan.infeasible_reasons.join("；")}`
                : ""}
            </dd>
          </div>
        </dl>
      ) : null}

      {items.length === 0 ? (
        <p className="fp-plan__empty">{emptyHint}</p>
      ) : (
        <>
          <p className="fp-plan__summary" data-testid="plan-steps-summary">
            按真实状态统计：共 {items.length} 步，已完成 {completed} 步
            {failed > 0 ? `，失败 ${failed} 步` : ""}
            {stopped > 0 ? `，已停止 ${stopped} 步` : ""}
            {inFlight > 0 ? `，在途（可能已产生外部效果）${inFlight} 步` : ""}。
          </p>
          <ol className="fp-plan__list">
            {items.map((step, index) => {
              const status = normalizePlanStepStatus(step.status);
              const isActive = activeStepId === step.step_id;
              const levelLabel = stepLevelLabel(step);
              const isRedPending =
                status === "awaiting_confirmation" && /RED/i.test(String(step.required_level ?? ""));
              const summary = effectSummary(step);
              return (
                <li
                  key={step.step_id}
                  className={cx("fp-plan__item", isActive && "fp-plan__item--active")}
                  data-step-id={step.step_id}
                  data-status={step.status}
                  aria-current={isActive ? "step" : undefined}
                >
                  <div className="fp-plan__item-head">
                    <span className="fp-plan__index" aria-hidden="true">
                      {typeof step.seq === "number" ? step.seq + 1 : index + 1}
                    </span>
                    <span className="fp-plan__title">
                      <code>{step.tool}</code>
                      {step.node_id ? <span className="fp-plan__node">（{step.node_id}）</span> : null}
                    </span>
                    <StatusChip
                      label={planStepStatusLabel(step.status)}
                      tone={toneForStatus(status ?? step.status)}
                      srPrefix="步骤状态"
                      rawValue={String(step.status)}
                      data-testid={`plan-step-status-${step.step_id}`}
                    />
                  </div>

                  <div className="fp-plan__meta">
                    {levelLabel ? <span className="fp-plan__level-tag">{levelLabel}</span> : null}
                    {step.dependencies && step.dependencies.length > 0 ? (
                      <span className="fp-plan__deps">依赖：{step.dependencies.join("、")}</span>
                    ) : null}
                    {typeof step.attempt === "number" && step.attempt > 0 ? (
                      <span className="fp-plan__attempt">尝试次数：{step.attempt}</span>
                    ) : null}
                  </div>

                  {summary ? <p className="fp-plan__text">预期效果：{summary}</p> : null}
                  {step.preconditions && step.preconditions.length > 0 ? (
                    <p className="fp-plan__text">前置条件：{step.preconditions.join("；")}</p>
                  ) : null}

                  {step.status === "awaiting_authorization" ? (
                    <p className="fp-plan__waiting" role="status" aria-live="polite">
                      等待授权：该步骤尚未提交，需你确认{isRedPending ? "并完成强验证（TOTP）" : ""}。
                    </p>
                  ) : null}
                  {status === "running" ? (
                    <p className="fp-plan__waiting" role="status" aria-live="polite">
                      执行中：尚未收到外部结果，请不要重复提交。
                    </p>
                  ) : null}
                  {isRedPending ? (
                    <p className="fp-plan__notice">该步骤为红色等级：必须完成强验证后才会提交，当前未提交。</p>
                  ) : null}
                  {status === "submitted" || status === "awaiting_result" ? (
                    <p className="fp-plan__notice">
                      已提交但尚未确认结果：可能已经产生外部效果，请不要重复提交；可用操作编号查询权威状态。
                    </p>
                  ) : null}
                  {status === "result_unknown" ? (
                    <p className="fp-plan__notice fp-plan__notice--unknown">
                      结果未知：系统正在用原操作编号核对，请不要重复提交；核对完成前不显示成功或失败。
                    </p>
                  ) : null}
                  {step.last_error ? <p className="fp-plan__reason">失败原因：{step.last_error}</p> : null}
                  {step.compensation_policy?.mode && step.compensation_policy.mode !== "none" ? (
                    <p className="fp-plan__text">
                      补偿策略：{step.compensation_policy.mode}
                      {step.compensation_policy.reason ? ` · ${step.compensation_policy.reason}` : ""}
                      {step.compensation_policy.requires_reauthorization ? " · 需重新授权" : ""}
                    </p>
                  ) : null}

                  <div className="fp-plan__actions">
                    {step.operation_id ? (
                      <>
                        <span className="fp-plan__operation">
                          操作编号：<code>{step.operation_id}</code>
                        </span>
                        {onViewOperation ? (
                          <button
                            type="button"
                            className="fp-button fp-button--small"
                            onClick={() => onViewOperation(step.operation_id as string)}
                          >
                            查询该操作
                          </button>
                        ) : null}
                      </>
                    ) : null}
                    {status === "awaiting_confirmation" && onConfirmStep ? (
                      <button
                        type="button"
                        className="fp-button fp-button--small fp-button--primary"
                        onClick={() => onConfirmStep(step)}
                      >
                        确认该步骤
                      </button>
                    ) : null}
                    {onStopStep && status !== "succeeded" && status !== "failed" && status !== "stopped" ? (
                      <button type="button" className="fp-button fp-button--small" onClick={() => onStopStep(step)}>
                        停止该步骤（未提交部分）
                      </button>
                    ) : null}
                  </div>
                </li>
              );
            })}
          </ol>
        </>
      )}

      {plan?.scope_hash ? (
        <p className="fp-plan__scope-hash">
          授权范围摘要 scope_hash：<code data-testid="plan-scope-hash">{plan.scope_hash}</code>
        </p>
      ) : null}
      {plan?.confirmed_at ? (
        <p className="fp-plan__confirmed">确认时间：{formatDateTime(plan.confirmed_at)}</p>
      ) : null}
      <p className="fp-plan__legend">状态说明：{Object.values(PLAN_STEP_STATUS_LABELS).join(" / ")}</p>
    </section>
  );
}