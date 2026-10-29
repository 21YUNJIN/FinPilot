# 方法卡：sq-wealth-risk/1.0.0

本文件是 V5 §15.1 要求的"方法卡"。任何指标被展示或被下游使用前，必须能与本卡逐条对上。

## 1 输入

| 字段 | 说明 | 缺失时 |
|---|---|---|
| `products[].nav[]` | `{date: YYYY-MM-DD, value: >0}`，按调用方提供的日频净值 | 422 `UNPROCESSABLE_DATA` |
| `products[].official_risk_level` | 银行产品官方 R1—R5 | 不参与可申购比较（`PRODUCT_RISK_LEVEL_UNKNOWN`） |
| `total_fee_bps` / `term_days` / `settlement_days` / `min_amount_minor` | 条款事实，由 FinPilot 提供 | 排序中按 0 处理并保留字段为空，**不由净值推断** |
| `user.risk_level` | 风险测评结果 | 400 `INVALID_REQUEST` |
| `user.horizon_days` / `liquidity_need_days` / `budget_minor` | 用户约束 | 不传则不做对应硬过滤（并在响应中保持字段为 null） |
| `as_of` | 业务日 | 400 |

净值序列按日期升序归一化；同一天出现两条直接拒绝，不猜哪条正确。

## 2 统计口径

| 指标 | 公式 | 最少样本 | 单位 |
|---|---|---|---|
| `cumulative_return` | `nav_last / nav_first - 1` | 2 点 | ratio |
| `cagr` | `(nav_last/nav_first)^(252/收益数) - 1` | 2 点 + 60 收益 | ratio/年 |
| `annual_volatility` | `pstdev(日收益) * sqrt(252)` | **60 收益** | ratio/年 |
| `downside_deviation` | `sqrt(mean(min(0,r)^2)) * sqrt(252)` | 60 收益 | ratio/年 |
| `max_drawdown` | `min(nav/峰值 - 1)`，覆盖**整个观察窗口** | 2 点 | ratio |
| `historical_cvar_95` | 最差 `ceil(5% × n)` 个日收益的均值（负=损失） | **252 收益** | 日收益 |
| `sharpe` | `mean(r - rf/252)/pstdev(r) * sqrt(252)`，`rf` 默认 1.5% | 60 收益 | ratio/年 |
| `sharpe_t_stat` / `sharpe_se_annual` | Lo (2002) 自相关调整（Bartlett 核），复用 StockQuant `sharpe_statistics` | 60 收益 | — |
| `calmar` | `cagr / abs(max_drawdown)` | 60 收益且回撤≠0 | ratio |

- 年度交易日因子固定 **252**，不随资产频率变化。
- 不足门槛时 `value=null` 且给出 `na_reason`，**不用 0 代替**。
- 样本 <252 时仍报累计收益与回撤，但界面必须写明"样本不足，CVaR 不展示"。
- `cagr` 在 60—251 收益时照常输出，但属于短样本年化，展示时必须带窗口起止。

## 3 统计风险档（仅诊断）

`observed_market_risk_band` 由年化波动分档：<1.5% R1、<3% R2、<6% R3、<12% R4、≥12% R5；若最大回撤 < -15% 再上调一档。

**它不是官方风险等级，也不能替代适当性判断。** 与 `official_risk_level` 不一致时，报告写入 warning，提示核对产品映射或数据，而不是自动改客户等级。

## 4 压力情景

| 情景 | 方法 | 假设 |
|---|---|---|
| `nav_shock` | 净值 ×(1+shock)，shock ∈ [-1,0] | 一次性等比例下跌；不建模费用、暂停赎回、分红 |
| `historical_worst_window` | 样本内最差 N 个交易日累计跌幅，一次性重放 | 已发生情形，不是预测；窗口 2—250 |
| `parametric_vol_shock` | `z(1-置信度) × 年化波动 × sqrt(horizon/252) × 倍数` | 正态假设；厚尾与相关性上升会低估极端损失 |

每条结果都带 `assumptions` 与 `not_a_loss_limit`。不得把情景结果写成"最大亏损"或"风险预算"。

## 5 排序

1. 硬过滤（顺序固定）：产品风险等级 > 用户等级、期限 > 用款时间、赎回到账 > 可等待天数、起购 > 预算、官方等级缺失。
2. 剩余产品按用户偏好打分，权重公开在 `weights_used`：

| 模式 | 权重 |
|---|---|
| `stability` | 波动 .40、回撤 .30、CVaR .20、费用 .10 |
| `balanced` | 波动 .25、回撤 .20、CVaR .15、费用 .20、流动性 .10、期限 .10 |
| `return` | 历史 Sharpe .60、历史 CAGR .25、费用 .15 |
| `cost` | 费用 .50、流动性 .30、期限 .20 |

3. 打分尺度是**绝对参考刻度**（见 `scoring_scales`），不随本次请求的产品集合变化；否则 A4 消融会因"这次发了哪些产品"而漂移。
4. `baseline_ranked` 使用 A4 口径（只用费用、流动性、期限），与量化排序共享同一批通过硬过滤的产品。
5. 同分依次按总费率、`product_id` 排序，保证可复现。
6. 未表达偏好时用 `balanced` 并在界面披露；模型不能自行新建风险阈值。

## 6 版本与变更

- 方法版本：`sq-wealth-risk/1.0.0`。任何公式、门槛、权重变化都必须升版本，客户端默认钉住版本，不匹配即拒绝使用。
- 引擎：StockQuant `stockquant.backtest.metrics`（`__version__` 0.1.0，备份 commit `5559e3f`）。
- 已知偏差：
  - 净值频率假定为日频；周频/月频产品直接传入会低估年化波动（252 因子不适用），当前版本不自动识别频率，需调用方换算或标记不可用。
  - 分红再投资口径取决于净值序列本身（累计净值 vs 单位净值），服务不推断；调用方必须在 `nav_source` 说明。
  - 合成数据只验证管道，不构成任何市场结论。