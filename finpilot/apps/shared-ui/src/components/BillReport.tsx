import type { ReactElement } from "react";
import type { ApiFailure } from "../api/client";
import type { BillReport as BillReportData } from "../types";
import { DOMAIN_SOURCE_LABELS } from "../contract/labels";
import { formatDate, formatDateTime } from "../format/datetime";
import { formatPercent } from "../format/money";
import { MoneyText } from "./MoneyText";
import { ModeBadge } from "./ModeBadge";
import { ErrorBanner } from "./ErrorBanner";
import { cx } from "../lib/cx";

/**
 * 账单报表（bill_report.json / S2）。
 *
 * 规则：
 * - 报告必须注明数据截至时间（data_cutoff_at）；内部转账不计消费、费用单列、退款口径明确；
 * - 占比用整数基点（share_bps）展示，缺省时按总额换算；零分母不造值（显示"不适用"）；
 * - 异常提示只展示可核查依据，不表述为欺诈定论；
 * - `untrusted_text`（商户自由文本）单独标注为不可信数据，不与结构化金额混排；
 * - 报告缺失/失败时显示真实原因与重试入口，不伪造一份"演示账单"。
 */
export interface BillReportProps {
  report?: BillReportData | null;
  title?: string;
  loading?: boolean;
  error?: ApiFailure | null;
  onExport?: () => void;
  onRetry?: () => void;
  emptyHint?: string;
  className?: string;
  "data-testid"?: string;
}

function periodText(report: BillReportData): string {
  const period = report.period;
  if (period.kind === "month" && period.year && period.month) return `${period.year} 年 ${period.month} 月`;
  if (period.kind === "year" && period.year) return `${period.year} 年`;
  if (period.start_date && period.end_date) return `${period.start_date} 至 ${period.end_date}`;
  return "不适用（服务端未给出账期）";
}

export function BillReport({
  report,
  title = "账单分析",
  loading = false,
  error,
  onExport,
  onRetry,
  emptyHint = "尚未获取账单数据。",
  className,
  "data-testid": dataTestId = "bill-report",
}: BillReportProps): ReactElement {
  if (error) {
    return (
      <section className={cx("fp-card", "fp-bills", className)} aria-label={title} data-testid={dataTestId}>
        <header className="fp-card__header">
          <h2 className="fp-card__title">{title}</h2>
        </header>
        <ErrorBanner failure={error} context="账单查询" onRetry={onRetry} />
      </section>
    );
  }

  if (!report) {
    return (
      <section className={cx("fp-card", "fp-bills", className)} aria-label={title} data-testid={dataTestId}>
        <header className="fp-card__header">
          <h2 className="fp-card__title">{title}</h2>
        </header>
        <p className="fp-bills__empty" aria-busy={loading}>
          {loading ? "正在查询服务端账单数据…" : emptyHint}
        </p>
      </section>
    );
  }

  const zeroDenominator = (report.zero_denominator_flags ?? []).length > 0;

  return (
    <section className={cx("fp-card", "fp-bills", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
        {onExport ? (
          <button type="button" className="fp-button fp-button--small" onClick={onExport}>
            导出本报告
          </button>
        ) : null}
      </header>

      <p className="fp-bills__period" data-testid="bill-period">
        账期：{periodText(report)}
      </p>
      <p className="fp-bills__cutoff">数据截至：{formatDateTime(report.data_cutoff_at, "未提供")}</p>

      <dl className="fp-bills__totals">
        <div>
          <dt>消费性支出</dt>
          <dd>
            <MoneyText amountMinor={report.total_outflow_minor} currency={report.currency} data-testid="bill-total-spend" />
          </dd>
        </div>
        <div>
          <dt>入账合计</dt>
          <dd>
            <MoneyText amountMinor={report.total_inflow_minor} currency={report.currency} />
          </dd>
        </div>
        <div>
          <dt>净额</dt>
          <dd>
            <MoneyText amountMinor={report.net_minor} currency={report.currency} />
          </dd>
        </div>
        <div>
          <dt>内部转账（不计消费）</dt>
          <dd>
            <MoneyText amountMinor={report.internal_transfer_minor ?? null} currency={report.currency} fallback="不适用" />
          </dd>
        </div>
        <div>
          <dt>费用（单列）</dt>
          <dd>
            <MoneyText amountMinor={report.fee_minor ?? null} currency={report.currency} fallback="不适用" />
          </dd>
        </div>
        <div>
          <dt>退款调整</dt>
          <dd>
            <MoneyText amountMinor={report.refund_minor ?? null} currency={report.currency} fallback="不适用" />
          </dd>
        </div>
      </dl>

      <div className="fp-bills__categories">
        <h3 className="fp-bills__subtitle">分类明细</h3>
        {report.categories.length === 0 ? (
          <p className="fp-bills__empty">本次账期没有可分类的支出。</p>
        ) : (
          <table className="fp-table">
            <caption className="fp-table__caption">金额为整数分换算；占比按支出总额计算，零分母时显示"不适用"</caption>
            <thead>
              <tr>
                <th scope="col">分类</th>
                <th scope="col">金额</th>
                <th scope="col">笔数</th>
                <th scope="col">占比</th>
              </tr>
            </thead>
            <tbody>
              {report.categories.map((category) => (
                <tr key={category.category}>
                  <th scope="row">{category.category}</th>
                  <td>
                    <MoneyText amountMinor={category.amount_minor} currency={report.currency} />
                  </td>
                  <td>{typeof category.count === "number" ? category.count : "不适用"}</td>
                  <td>
                    {typeof category.share_bps === "number"
                      ? formatPercent(category.share_bps / 10000)
                      : report.total_outflow_minor > 0
                        ? formatPercent(category.amount_minor / report.total_outflow_minor)
                        : "不适用"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {typeof report.uncategorized_minor === "number" ? (
          <p className="fp-bills__note">
            未分类金额：<MoneyText amountMinor={report.uncategorized_minor} currency={report.currency} />
          </p>
        ) : null}
        {zeroDenominator ? (
          <p className="fp-bills__note" role="status">
            零分母项：{(report.zero_denominator_flags ?? []).join("、")} —— 相关比例显示为"不适用"，不造值。
          </p>
        ) : null}
      </div>

      {report.anomalies && report.anomalies.length > 0 ? (
        <div className="fp-bills__anomalies" data-testid="bill-anomalies">
          <h3 className="fp-bills__subtitle">异常提示（依据可核查）</h3>
          <ul>
            {report.anomalies.map((item) => (
              <li key={`${item.label}-${item.basis}`}>
                {item.label}：{item.basis}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      {(report.warnings ?? []).length > 0 ? (
        <div className="fp-bills__warnings" role="status" data-testid="bill-warnings">
          <h3 className="fp-bills__subtitle">报告警告</h3>
          <ul>
            {(report.warnings ?? []).map((warning) => (
              <li key={warning}>{warning}</li>
            ))}
          </ul>
        </div>
      ) : null}

      {report.untrusted_text ? (
        <p className="fp-bills__untrusted" data-testid="bill-untrusted-text">
          商户自由文本（不可信，仅原样展示，不代表金额或结论）：{report.untrusted_text}
        </p>
      ) : null}

      <footer className="fp-bills__footer">
        <span>
          数据来源：{DOMAIN_SOURCE_LABELS[String(report.source)] ?? `来源未登记（${String(report.source)}）`}
        </span>
        <ModeBadge
          mode={report.provenance?.mode ?? null}
          adapter={report.provenance?.adapter ?? null}
          observedAt={report.provenance?.observed_at ? formatDateTime(report.provenance.observed_at) : null}
          size="sm"
          source="账单接口 provenance"
        />
        <span>生成时间：{formatDate(report.generated_at, "未提供")}</span>
      </footer>
    </section>
  );
}