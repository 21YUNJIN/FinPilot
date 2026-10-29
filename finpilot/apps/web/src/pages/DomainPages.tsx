import { useCallback, useEffect, useState, type ReactElement } from "react";
import {
  BillReport,
  CardPanel,
  ErrorBanner,
  PrivacyPanel,
  RiskQuestionnaire,
  SubscriptionList,
  WealthCompare,
  cacheKeys,
  domainApi,
  experimentalApi,
  newOperationId,
  privacyApi,
  resourceCache,
  useResource,
  type ApiFailure,
  type BillReportData,
  type Card,
  type Mandate,
  type PrivacyDeleteView,
  type RetentionRule,
  type RiskQuestionnaireData,
  type Subscription,
  type WealthProduct,
} from "@finpilot/shared-ui";
import { useAppState } from "../state";

/**
 * 场景页（卡片 / 账单 / 理财 / 订阅 / 隐私）：全部走同一套只读接口 + 用户级缓存。
 *
 * 规则：
 * - 只读数据用 useResource（按用户隔离缓存，切换用户自动失效）；
 * - **本页不做写操作的假成功**：卡片挂失/补卡（RED）、理财购买（RED）、订阅取消与代扣撤销
 *   都需要服务端签发授权或挑战，因此只提供跳转到任务/确认页的真实入口；
 * - 隐私删除是真实写操作：带 operation_id 调用 /v1/privacy/delete，失败显示后端安全文案。
 */

// ---------------------------------------------------------------------------
// 卡片
// ---------------------------------------------------------------------------

export function CardsPage(): ReactElement {
  const cards = useResource<Card[]>({
    key: cacheKeys.cards(),
    loader: () => domainApi.cards(),
  });

  return (
    <div className="web-cards">
      {cards.error ? <ErrorBanner failure={cards.error} context="卡片查询" onRetry={() => void cards.refresh()} /> : null}
      <CardPanel
        cards={cards.data ?? []}
        onReportLost={() => {
          window.location.hash = "/chat";
        }}
        onReplace={() => {
          window.location.hash = "/chat";
        }}
      />
      <p className="web-hint">
        挂失、补卡、解挂为红色等级：必须完成强验证（TOTP）后由服务端提交；本页只做展示与入口跳转，
        不用按钮点击代替强验证。
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 账单
// ---------------------------------------------------------------------------

export function BillsPage(): ReactElement {
  const { reportMode } = useAppState();
  const [period, setPeriod] = useState<"month" | "year">("month");
  const report = useResource<BillReportData>({
    key: cacheKeys.billReport(period),
    loader: () => domainApi.billReport({ kind: period }),
  });
  const mode = report.data?.provenance?.mode ?? null;

  useEffect(() => {
    if (mode) reportMode(mode);
  }, [mode, reportMode]);

  return (
    <div className="web-bills">
      <div className="web-filters" role="group" aria-label="账期选择">
        <button
          type="button"
          className="fp-button fp-button--small"
          aria-pressed={period === "month"}
          onClick={() => setPeriod("month")}
        >
          本月
        </button>
        <button
          type="button"
          className="fp-button fp-button--small"
          aria-pressed={period === "year"}
          onClick={() => setPeriod("year")}
        >
          本年
        </button>
        <button type="button" className="fp-button fp-button--small" onClick={() => void report.refresh()}>
          刷新
        </button>
      </div>
      <BillReport
        report={report.data}
        loading={report.loading}
        error={report.error}
        onRetry={() => void report.refresh()}
        onExport={() => {
          window.location.hash = "/privacy";
        }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// 理财
// ---------------------------------------------------------------------------

export function WealthPage(): ReactElement {
  const [budget, setBudget] = useState(1000);
  const [horizon, setHorizon] = useState(365);
  const [preference, setPreference] = useState("risk");
  const products = useResource<WealthProduct[]>({
    key: `wealth:comparison:${budget}:${horizon}:${preference}`,
    loader: () => domainApi.wealthComparison({budget_minor: Math.round(budget * 100), horizon_days: horizon, preference}),
  });
  const profile = useResource({
    key: "wealth:risk-profile",
    loader: () => experimentalApi.riskProfile(),
  });

  const [questionnaire, setQuestionnaire] = useState<RiskQuestionnaireData | null>(null);
  const [assessmentBusy, setAssessmentBusy] = useState(false);
  const [assessmentError, setAssessmentError] = useState<string | null>(null);

  const openQuestionnaire = useCallback(async () => {
    setAssessmentError(null);
    const result = await experimentalApi.riskQuestionnaire();
    if (result.ok) {
      setQuestionnaire(result.data);
    } else {
      setAssessmentError(result.error.message);
    }
  }, []);

  const submitAssessment = useCallback(
    async (answers: Record<string, string>) => {
      setAssessmentBusy(true);
      setAssessmentError(null);
      const result = await experimentalApi.submitRiskAssessment(answers, questionnaire?.questionnaire_version);
      setAssessmentBusy(false);
      if (result.ok) {
        setQuestionnaire(null);
        await profile.refresh();
      } else {
        setAssessmentError(result.error.message);
      }
    },
    [questionnaire, profile],
  );

  if (questionnaire) {
    return (
      <div className="web-wealth">
        <RiskQuestionnaire
          questionnaire={questionnaire}
          onSubmit={submitAssessment}
          busy={assessmentBusy}
          error={assessmentError}
          onCancel={() => setQuestionnaire(null)}
        />
      </div>
    );
  }

  return (
    <div className="web-wealth">
      <section className="fp-card" aria-label="理财比较条件">
        <h2>理财比较条件</h2>
        <label>含费预算（元） <input aria-label="含费预算（元）" type="number" min="1" step="1" value={budget}
          onChange={(e) => setBudget(Math.max(1, Number(e.target.value) || 1))} /></label>{" "}
        <label>用款时间（天后） <input aria-label="用款时间（天后）" type="number" min="1" step="1" value={horizon}
          onChange={(e) => setHorizon(Math.max(1, Math.floor(Number(e.target.value) || 1)))} /></label>{" "}
        <label>优先比较 <select aria-label="优先比较" value={preference} onChange={(e) => setPreference(e.target.value)}>
          <option value="risk">产品风险等级</option><option value="fee">费用</option>
          <option value="liquidity">流动性</option><option value="yield">历史净值表现</option>
        </select></label>
        <p>先核对风险等级、期限和预算，再按所选偏好排序。当前使用合成产品净值，不代表真实产品收益。</p>
      </section>
      {products.error ? <ErrorBanner failure={products.error} context="理财查询" onRetry={() => void products.refresh()} /> : null}
      <WealthCompare
        products={products.data ?? []}
        riskProfile={profile.data}
        onSelect={() => {
          window.location.hash = "/chat";
        }}
        onStartAssessment={() => {
          void openQuestionnaire();
        }}
      />
      {assessmentError && !questionnaire ? (
        <p className="web-hint" role="alert">{assessmentError}</p>
      ) : null}
      <p className="web-hint">
        购买与赎回属于红色等级：必须完成强验证；风险等级、费率与净值缺失时显示"不适用"，界面不提供收益保证。
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 订阅与代扣
// ---------------------------------------------------------------------------

export function SubscriptionsPage(): ReactElement {
  const [queryOperation, setQueryOperation] = useState<string | null>(null);
  const subscriptions = useResource<Subscription[]>({
    key: queryOperation ? `subscriptions:confirmed:${queryOperation}` : null,
    enabled: queryOperation !== null,
    cache: false,
    loader: () => domainApi.confirmedSubscriptions(queryOperation as string),
  });

  // 代扣授权可能内联在合同对象里；没有内联时组件会显示"不适用（没有关联的代扣授权）"。
  const mandates: Mandate[] = (subscriptions.data ?? [])
    .map((item) => item.mandate)
    .filter((item): item is Mandate => !!item);

  return (
    <div className="web-subs">
      {!queryOperation ? <section className="fp-card" aria-label="订阅查询确认">
        <h2>确认查询订阅（黄色等级）</h2>
        <p>将读取你账户下的订阅合同、扣费金额、续费日期和代扣状态；不会取消合同或扣款。</p>
        <button type="button" className="fp-button fp-button--primary" onClick={() => setQueryOperation(newOperationId())}>确认查询我的订阅</button>
      </section> : null}
      {subscriptions.error ? (
        <ErrorBanner failure={subscriptions.error} context="订阅查询" onRetry={() => void subscriptions.refresh()} />
      ) : null}
      <SubscriptionList
        subscriptions={subscriptions.data ?? []}
        mandates={mandates}
        onCancelContract={() => {
          window.location.hash = "/chat";
        }}
        onRevokeMandate={() => {
          window.location.hash = "/chat";
        }}
      />
      <p className="web-hint">
        取消合同（黄色等级）与撤销代扣授权（红色等级）是两个独立动作，必须在任务页分别确认；
        本页展示两者各自的真实状态，未确认前不显示为已生效。
      </p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 隐私
// ---------------------------------------------------------------------------

const DEFAULT_RETENTION: RetentionRule[] = [
  { data_class: "交易与授权记录", retention: "按监管要求留存", note: "金融事实，不直接抹除" },
  { data_class: "对话与任务上下文", retention: "以服务端策略为准", note: "删除后不再用于展示与自动化" },
];

export function PrivacyPage(): ReactElement {
  const [deleteError, setDeleteError] = useState<ApiFailure | null>(null);
  const [deleteView, setDeleteView] = useState<PrivacyDeleteView | null>(null);
  const [busy, setBusy] = useState<"export" | "delete" | null>(null);

  const exportView = useResource({
    key: cacheKeys.privacyRetention(),
    loader: () => privacyApi.exportData(),
    cache: false,
  });

  const requestDelete = useCallback(
    async (input: { scope: string; reason: string }) => {
      setBusy("delete");
      const result = await privacyApi.requestDelete({
        scope: input.scope,
        reason: input.reason,
        operationId: newOperationId(),
      });
      setBusy(null);
      if (!result.ok) {
        setDeleteError(result.error);
        return;
      }
      setDeleteError(null);
      setDeleteView(result.data);
      // 删除请求会影响可导出的内容：清掉只读缓存，避免展示过期凭据。
      resourceCache.clear("privacy_delete_requested");
      await exportView.refresh();
    },
    [exportView],
  );

  return (
    <div className="web-privacy">
      <PrivacyPanel
        retention={exportView.data?.retention ?? DEFAULT_RETENTION}
        exportView={exportView.data}
        exportBusy={busy === "export" || exportView.loading}
        deleteBusy={busy === "delete"}
        exportError={exportView.error}
        deleteError={deleteError}
        onExport={() => {
          setBusy("export");
          void exportView.refresh().finally(() => setBusy(null));
        }}
        onDeleteRequest={(input) => {
          void requestDelete(input);
        }}
      />
      {deleteView ? (
        <section className="fp-card" aria-label="删除请求结果">
          <h2 className="fp-card__title">删除请求已提交</h2>
          <p className="web-hint">
            状态：{deleteView.status} · 范围：{deleteView.scope}
            {deleteView.retained_basis ? ` · 留存依据：${deleteView.retained_basis}` : ""}
          </p>
        </section>
      ) : null}
    </div>
  );
}