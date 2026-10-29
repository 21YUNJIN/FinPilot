import type { ReactElement } from "react";
import type { QuantRiskReport } from "../types";

export function WealthRiskCell({ report }: { report?: QuantRiskReport | null }): ReactElement {
  if (!report || report.status !== "ok") {
    return <span className="fp-na">NA（{report?.message ?? "风险统计尚未接通"}）</span>;
  }
  const rows = [
    ["annual_volatility", "年化波动"],
    ["max_drawdown", "观察期最大回撤"],
    ["historical_cvar_95", "最差 5% 日收益均值"],
  ];
  return <div>
    {rows.map(([key, label]) => {
      const metric = report.metrics[key as string];
      return <div key={key}>{label}：{metric?.value == null
        ? "NA（有效历史样本不足）" : `${(metric.value * 100).toFixed(2)}%`}</div>;
    })}
    <div>数据截至 {report.data_window?.end ?? "NA"}</div>
    <details><summary>计算来源与说明</summary>
      <p>{report.source_verified ? "净值来源校验通过" : "来源未核验"} · {report.provenance?.method_version ?? "NA"}</p>
      <p>请求编号：{report.request_id ?? "NA"}</p>
      {(report.warnings ?? []).map((warning, i) => <p key={i}>{warning}</p>)}
      <p>历史统计不代表未来表现；历史回撤不是最大亏损上限。</p>
    </details>
  </div>;
}
