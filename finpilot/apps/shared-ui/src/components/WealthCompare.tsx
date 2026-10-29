import type { ReactElement } from "react";
import type { RiskProfile, WealthProduct } from "../types";
import { PRODUCT_TYPE_LABELS, riskLevelLabel } from "../contract/labels";
import { formatDate } from "../format/datetime";
import { MoneyText } from "./MoneyText";
import { StatusChip, type StatusTone } from "./StatusChip";
import { cx } from "../lib/cx";
import { WealthRiskCell } from "./WealthRiskCell";

/**
 * 理财产品比较（S3）：风险 / 费用 / 期限 / 流动性 / 出处。
 *
 * 规则（wealth_product.json、S3-02/S3-05）：
 * - 缺失或数据不足一律显示"不适用"（NA）：`nav_is_na=true`、缺流动性条款、缺期限、缺出处；
 * - 风险等级必须文字化（"中高风险 R4"），颜色只作辅助；
 * - 费率用整数基点展示（1bp=0.01%），固定费用用整数分，两者都显示；
 * - 出处展示 issuer + terms_version + history_ref，缺失即"不适用（未标注出处）"；
 * - 需要风险测评而测评缺失/过期时不允许直接进入购买确认。
 */
export interface WealthCompareProps {
  products: WealthProduct[];
  riskProfile?: RiskProfile | null;
  highlightProductId?: string | null;
  busyProductId?: string | null;
  onSelect?: (product: WealthProduct) => void;
  onStartAssessment?: () => void;
  emptyHint?: string;
  title?: string;
  className?: string;
  "data-testid"?: string;
}

const NA_TEXT = "不适用";

function riskTone(level: string | null | undefined): StatusTone {
  const key = String(level ?? "").trim().toUpperCase();
  if (key === "R1" || key === "R2") return "success";
  if (key === "R3") return "warning";
  if (key === "R4" || key === "R5") return "danger";
  const text = String(level ?? "");
  if (/高/.test(text)) return "danger";
  if (/中/.test(text)) return "warning";
  return "info";
}

function feeText(product: WealthProduct): ReactElement {
  const hasBps = typeof product.fee_bps === "number";
  // 0 元固定费用不展示，避免干扰阅读；只展示真实存在的费用项。
  const hasFixed = typeof product.fee_fixed_minor === "number" && product.fee_fixed_minor > 0;
  if (!hasBps && !hasFixed) return <span className="fp-na">{NA_TEXT}（提供方未给出费率）</span>;
  return (
    <span>
      {hasBps ? `${product.fee_bps} bps（${(product.fee_bps / 100).toFixed(2)}%）` : NA_TEXT}
      {hasFixed ? (
        <>
          {" + "}
          <MoneyText amountMinor={product.fee_fixed_minor} />
        </>
      ) : null}
    </span>
  );
}

function termText(product: WealthProduct): ReactElement {
  if (typeof product.term_days === "number") {
    return (
      <span>
        {product.term_days} 天
        {typeof product.settlement_days === "number" ? `（结算 ${product.settlement_days} 天）` : ""}
      </span>
    );
  }
  return <span className="fp-na">{NA_TEXT}（未标注期限）</span>;
}

function liquidityText(product: WealthProduct): ReactElement {
  if (product.liquidity_terms && product.liquidity_terms.trim() !== "") {
    return <span>{product.liquidity_terms}</span>;
  }
  return <span className="fp-na">{NA_TEXT}（未标注流动性条款）</span>;
}

function sourceText(product: WealthProduct): ReactElement {
  const parts = [product.issuer, product.source, product.terms_version ? `条款 ${product.terms_version}` : null, product.history_ref]
    .filter((part): part is string => typeof part === "string" && part.trim() !== "");
  if (parts.length === 0) return <span className="fp-na">{NA_TEXT}（未标注出处）</span>;
  return <span>{parts.join(" · ")}</span>;
}

function navText(product: WealthProduct): ReactElement {
  if (product.nav_is_na === true || product.nav_e6 === null || product.nav_e6 === undefined) {
    return <span className="fp-na">{NA_TEXT}（净值数据不足，未编造）</span>;
  }
  const nav = (product.nav_e6 / 1_000_000).toFixed(6);
  return (
    <span>
      {nav}
      {product.nav_as_of_date ? `（${formatDate(product.nav_as_of_date, "")}）` : ""}
    </span>
  );
}

export function WealthCompare({
  products,
  riskProfile,
  highlightProductId,
  busyProductId,
  onSelect,
  onStartAssessment,
  emptyHint = "没有可比较的产品。",
  title = "理财产品比较",
  className,
  "data-testid": dataTestId = "wealth-compare",
}: WealthCompareProps): ReactElement {
  const profileValid =
    !!riskProfile && (!riskProfile.valid_until || new Date(riskProfile.valid_until).getTime() > Date.now());
  const needsAssessment = products.some((product) => product.assessment_required || product.is_active === false);

  return (
    <section className={cx("fp-card", "fp-wealth", className)} aria-label={title} data-testid={dataTestId}>
      <header className="fp-card__header">
        <h2 className="fp-card__title">{title}</h2>
        {riskProfile ? (
          <span className="fp-wealth__assessment" data-testid="wealth-assessment">
            风险测评：{riskLevelLabel(riskProfile.level)}
            {riskProfile.valid_until ? ` · 有效期至 ${formatDate(riskProfile.valid_until, "")}` : ""}
            {profileValid ? "" : "（已过期，需要重新测评）"}
          </span>
        ) : (
          <span className="fp-wealth__assessment fp-na" data-testid="wealth-assessment-missing">
            风险测评：不适用（尚未完成测评）
          </span>
        )}
      </header>

      {needsAssessment && !profileValid ? (
        <div className="fp-wealth__notice" role="status" data-testid="wealth-assessment-required">
          <p>部分产品需要有效的风险测评才能购买；未测评前只能查看信息，不能进入购买确认。</p>
          {onStartAssessment ? (
            <button type="button" className="fp-button fp-button--small fp-button--primary" onClick={onStartAssessment}>
              开始风险测评（黄色等级）
            </button>
          ) : null}
        </div>
      ) : null}

      {products.length === 0 ? (
        <p className="fp-wealth__empty">{emptyHint}</p>
      ) : (
        <div className="fp-wealth__table-wrap">
          <table className="fp-table fp-wealth__table">
            <caption className="fp-table__caption">
              缺失字段显示"不适用"；费率以基点（1bp=0.01%）与固定费用分别展示，购买前会再次展示实际费用。
            </caption>
            <thead>
              <tr>
                <th scope="col">产品</th>
                <th scope="col">风险等级</th>
                <th scope="col">费用</th>
                <th scope="col">期限</th>
                <th scope="col">流动性</th>
                <th scope="col">净值</th>
                <th scope="col">历史风险统计</th>
                <th scope="col">出处</th>
                <th scope="col">操作</th>
              </tr>
            </thead>
            <tbody>
              {products.map((product) => (
                <tr
                  key={product.product_id}
                  data-product-id={product.product_id}
                  className={cx(highlightProductId === product.product_id && "fp-wealth__row--highlight")}
                  aria-current={highlightProductId === product.product_id ? "true" : undefined}
                >
                  <th scope="row">
                    {product.name}
                    {product.product_type ? (
                      <span className="fp-wealth__kind">
                        （{PRODUCT_TYPE_LABELS[String(product.product_type)] ?? String(product.product_type)}）
                      </span>
                    ) : null}
                    <span className="fp-wealth__min">
                      起购：<MoneyText amountMinor={product.min_minor} fallback="不适用" />
                    </span>
                    {product.is_active === false ? <span className="fp-wealth__na">（已停售）</span> : null}
                  </th>
                  <td>
                    <StatusChip
                      label={riskLevelLabel(product.issuer_risk_level)}
                      tone={riskTone(product.issuer_risk_level)}
                      srPrefix="风险等级"
                      rawValue={product.issuer_risk_level}
                      data-testid={`wealth-risk-${product.product_id}`}
                    />
                  </td>
                  <td>{feeText(product)}</td>
                  <td>{termText(product)}</td>
                  <td>{liquidityText(product)}</td>
                  <td>{navText(product)}</td>
                  <td><WealthRiskCell report={product.quant_risk} /></td>
                  <td>{sourceText(product)}</td>
                  <td>
                    {onSelect ? (
                      <button
                        type="button"
                        className="fp-button fp-button--small"
                        onClick={() => onSelect(product)}
                        disabled={busyProductId === product.product_id || product.eligible === false || product.is_active === false || (!!product.assessment_required && !profileValid)}
                      >
                        查看并准备购买确认
                      </button>
                    ) : (
                      <span className="fp-na">不适用</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <p className="fp-wealth__footer">
        风险等级为文字标签（颜色仅作辅助）；净值缺失时保持 NA；历史业绩不代表未来表现，页面不提供收益保证。
      </p>
    </section>
  );
}