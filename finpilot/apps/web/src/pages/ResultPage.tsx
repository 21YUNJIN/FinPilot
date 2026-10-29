import { useCallback, useEffect, useState, type ReactElement } from "react";
import {
  ErrorBanner,
  OperationReceipt,
  operationApi,
  type ApiFailure,
  type OperationView,
} from "@finpilot/shared-ui";
import { useAppState } from "../state";

/**
 * 结果页：按操作编号查询权威状态（不消费"模型说成功"）。
 *
 * 规则：
 * - 只调用 GET /v1/operations/{operation_id}（归属隔离）；
 * - 结果口径由 OperationReceipt 从 OperationView 推导：没有 effect_confirmed 依据就不显示"已到账"；
 * - 未知状态提供"查询原订单"按钮（同一 operation_id，不重复提交）。
 */
export function ResultPage({ operationId }: { operationId: string }): ReactElement {
  const { reportMode } = useAppState();
  const [operation, setOperation] = useState<OperationView | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<ApiFailure | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    const result = await operationApi.get(operationId);
    setLoading(false);
    if (!result.ok) {
      setError(result.error);
      return;
    }
    setError(null);
    setOperation(result.data);
    reportMode(result.data.domain_result?.provenance?.mode ?? null);
  }, [operationId, reportMode]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="web-result">
      <header className="web-section-header">
        <h1 className="web-section-title">操作结果</h1>
        <p className="web-section-hint">
          结果只来自权威操作查询与回执；"已受理"不等于"已到账"，未知时会用原操作编号持续核对。
        </p>
      </header>

      {error ? <ErrorBanner failure={error} context="操作查询" onRetry={() => void load()} /> : null}
      {operation ? (
        <OperationReceipt
          operation={operation}
          querying={loading}
          onQueryOriginalOrder={() => {
            void load();
          }}
        />
      ) : (
        <section className="fp-card" aria-label="操作结果">
          <p className="web-hint" aria-busy={loading}>
            {loading ? "正在查询服务端操作状态…" : "尚未取到操作状态。"}
          </p>
        </section>
      )}
    </div>
  );
}