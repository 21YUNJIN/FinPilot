# StockQuant MVP

> **银行 AI 智能体方向（FinPilot Agent）**：原有量化系统保留不动，新增 `bankagent/` 产品层。
>
> **当前开发基线：** [`六场景 SRS v4.0`](docs/BANKAGENT_SRS_V4_SIX_SCENARIOS.md)；**执行契约：** [`六场景执行与接口契约`](docs/BANKAGENT_V4_EXECUTION_CONTRACTS.md)；**产品说明：** [`六场景一页说明`](docs/BANKAGENT_ONE_PAGE.md)；**团队执行：** [`六场景 P0 任务看板`](docs/BANKAGENT_P0_TASK_BOARD.md)、[`四人任务总索引`](docs/BANKAGENT_TEAM_TASKS.md)、[`AI 任务包模板`](docs/BANKAGENT_AI_TASK_PACKET_TEMPLATE.md)。
>
> v1/v2/v3 企业投研分析版保留为技术参考，不再作为银行六场景比赛主线。
>
> **完整系统介绍（给老师/评审/其他 AI）：** [`StockQuant 智能量化系统完整简介`](docs/STOCKQUANT_COMPLETE_INTRODUCTION_2026-10-21.md)，另有 `StockQuant_智能量化系统完整简介_2026-10-21.docx` 和同名 PDF。
>
> **技术说明与功能报告（2026-10-28）：** [`StockQuant 智能量化系统技术说明与功能报告`](docs/STOCKQUANT_TECHNICAL_AND_FUNCTIONAL_REPORT_2026-10-28.md)，另有 `StockQuant_智能量化系统技术说明与功能报告_2026-10-28.docx` 和同名 PDF。

一个可离线运行的股票量化研究系统 MVP，面向 A 股风格的日频/周频/月频多因子研究。

它不是“AI 选股神器”，而是一个把以下环节连起来的可复现系统：

```text
CSV / 后续数据源
→ 事件时间与数据校验
→ 横截面因子评分
→ 行业/权重约束
→ 下期开盘执行
→ 成本、T+1、涨跌停、停牌约束
→ Walk-forward
→ 风险指标和 HTML 报告
```

> ⚠️ **研究用途，不是投资建议，不荐股**：本项目是软件工程与研究练习，不提供任何买卖建议、
> 不承诺收益。它自己的文档就写明回测 Sharpe **未通过显著性检验**（DSR 0.777 < 0.95）、
> 诚实预期只有 4~8%/年（尾部回撤按 −20% 预算）。详见 [`DISCLAIMER.md`](DISCLAIMER.md)。
>
> 重要：默认演示数据是合成的。项目只用于研究、代码验证和产品原型，不构成投资建议，不保证收益。

## 现在的状态（2026-10-10 第 4 轮复盘后）

| 模块 | 状态 | 结论 |
|---|---|---|
| 股票多因子（沪深 300/500/1000） | **已冻结** | 诚实口径：固定权重、strict PIT、修好的市值加权基准下，全样本超额 +9.8pp/年、Sharpe 0.91；**但 DSR 0.529（94 次诚实试验，需 >0.95）、PBO 0.246**。样本外 walk-forward 显示**「每窗口重选权重」是负贡献**（OOS 超额 −1.35%），冻结权重则 +3.36%/年（5 窗口 4 个为正、DSR 0.144）→ 结论：**冻结权重、不要再选**；行业中性若换成 PIT 干净的市值分位分组，超额回落到 +7.1pp（详见 `docs/STOCK_LINE_REVIEW_2026-10-16.md`） |
| **可转债「双低」轮动** | **主线** | 保守规格回测 CAGR **15.94%** / Sharpe 1.04 / maxDD −12.61%（2019-01~2026-10；含双边成本；2026-10-16 修复「用未来成交价」后重跑）；DSR 0.78 / **PBO 0.714**（均未过）；**诚实预期 4~8%/年**；⚠️ 按**你自己的本金口径**（1.8 万 / 5 元最低佣金）现实 CAGR 只有 **+4.47%**，15.94% 是「假设 5bps」的理论口径（`docs/CONVERTIBLE_BOND.md` §四之四、`docs/AUDIT_ROUND7_FIXES_2026-10-16.md`） |
| **ETF 执行验证（试点）** | **进行中** | 2 万本金 / 5 袖套（300+500+红利+黄金+国债）/ 月度 / 风险平价+趋势过滤 / 换手 0.29×/年：回测 CAGR 7.74%、波动 9.39%、maxDD −12.14%；**目的是验证执行层（滑点<0.3%、执行率>90%），不是赚超额**；511010 一手 1.41 万 > 2 万本金的一半 → 会自动剔除并告警（`docs/ETF_EXECUTION_VALIDATION.md`） |
| 本机看板（Flask, 127.0.0.1:8765） | 可用 | 默认落地页「✅ 本周操作」：清单 / 持仓盯盘 / paper 对账 / 强赎 / 打新 / 风控 |

必读文档：
- **`docs/CRITIQUE_2026-10-15.md`** —— 客观评估：系统现有的问题清单、按 ROI 排序的待办、以及**不建议做**的清单（含 DSR 0.78 / PBO 0.714 的解读）；
- **`docs/ANALYST_REVISION_SIGNAL.md`** —— C 线（分析师评级/覆盖修正）预注册与首轮检验：**五条判据全部不通过 → 不采纳**；EPS 修正版本在免费数据上不可测；同批日期下现有 10 因子复合分 IC 仍是 +0.021（t 1.37）；第二源与下次复验路径写在文末；
- **`docs/STOCK_LINE_REVIEW_2026-10-16.md`** —— 股票线首次体检：DSR 0.529 / PBO 0.246、12 个权重变体的同口径对比、样本外 walk-forward（OOS 超额 **−1.35%/年**），以及关键分解：全样本 +9.8pp 里 6.2pp 来自 2018-2020 行情、5.0pp 被"按训练期 Sharpe 选权重"这一步吃掉；
- `docs/CONVERTIBLE_BOND.md` —— 转债策略完整规格、结果、阈值稳健性、诚实局限；
- **`docs/ETF_EXECUTION_VALIDATION.md`** —— ETF 执行验证试点：策略规格、预注册判据（滑点中位 <0.3%、执行率 >90%、账实一致）、不通过时的处置路径；命令：`scripts/fetch_etf_data.py` → `scripts/etf_plan.py` → `scripts/etf_execution_report.py`；
- `docs/UNKNOWNS.md` —— **还不确定 / 不知道的清单**（数据、模型、执行、界面、元问题）；
- **`docs/AUDIT_ROUND7_FIXES_2026-10-16.md`** —— 第 7 轮修复：PIT 可得时间真正生效、历史成分不再向前回填、零成交/停牌 fail-closed、可转债不再用未来成交价，以及每处改动对历史数字的实测影响；
- 新增开关：`--pit-policy {off,session,strict}`、`--optimizer-candidate-multiplier`、`--kelly-fraction`；存量数据用 `scripts/backfill_fundamental_availability.py` 补戳（默认 dry-run）；
- `docs/AUDIT_ROUND4.md` —— 第 4 轮复盘发现的问题与修复（含未来函数修复）；
- `docs/REVIEW_BRIEF_4.md` —— 给外部评审（ChatGPT/Kimi）的完整材料与待审问题；
- **`docs/RESEARCH_LOG.md`** —— 2026-10-10 晚的六项实测（归因/赛马/参数对冲/DSR-PBO/失效规则/打新）。

## 已实现能力

- 透明多因子评分：价值、质量、动量、低波动、短期反转、流动性。
- 横截面 winsorize + z-score。
- 行业中性化。
- 组合约束：最大持仓数、单票权重、行业权重、现金缓冲。
- 目标波动率缩放。
- 月度/周度/日度调仓。
- 次日开盘执行，避免用当日收盘信号直接成交。
- A 股交易约束：
  - T+1 卖出保护
  - 100 股整数手
  - 停牌和涨跌停检查
  - 佣金、印花税、滑点
- 等权全市场基准。
- 绩效指标：CAGR、波动率、Sharpe、Sortino、最大回撤、Calmar、IR、Alpha、Beta、换手率。
- 滚动 Walk-forward：只用训练窗口选择因子权重，再在下一个测试窗口评估。
- HTML、JSON、CSV 报告输出。
- 无第三方依赖；Python 标准库即可运行。


## 本地交互界面

启动控制台：

```bash
./run_dashboard.sh
```

默认地址：`http://127.0.0.1:8765/`。

界面支持数据集选择、调仓频率、因子组合、主动权重、目标波动率、持仓与行业约束、交易成本、趋势过滤、一键回测、净值/基准图、目标组合、因子 IC 和历史实验。

macOS 部署使用 LaunchAgent：`~/Library/LaunchAgents/com.local.stockquant.dashboard.plist`，运行目录为 `~/Library/Application Support/StockQuant/app`。

## 快速开始

在当前项目根目录执行：

```bash
PYTHONPATH=. python3 -m stockquant demo \
  --output-dir demo_output \
  --walkforward
```

输出：

- `demo_market.csv`
- `backtest_report.html`
- `metrics.json`
- `trades.csv`
- `walkforward_report.html`
- `walkforward.json`
- `experiments.jsonl`

如果你要生成到 Codex 的交付目录：

```bash
PYTHONPATH=. python3 -m stockquant demo \
  --output-dir ../../outputs/stockquant_demo \
  --walkforward
```

## 使用自己的 CSV

```bash
PYTHONPATH=. python3 -m stockquant inspect --data /path/to/market.csv

PYTHONPATH=. python3 -m stockquant backtest \
  --data /path/to/market.csv \
  --output-dir ../../outputs/my_backtest \
  --rebalance monthly

PYTHONPATH=. python3 -m stockquant walkforward \
  --data /path/to/market.csv \
  --output-dir ../../outputs/my_walkforward \
  --train-days 252 --test-days 63 --step-days 63
```

CSV 必须是长表，一行表示“某日期、某股票”的行情和基础字段。字段定义见 [docs/DATA_CONTRACT.md](docs/DATA_CONTRACT.md)。


## 真实数据接入

安装可选研究环境：

```bash
python3 -m venv .venv
.venv/bin/pip install pandas akshare tushare
```

AKShare 免费日线：

```bash
PYTHONPATH=. .venv/bin/python -m stockquant fetch-akshare \
  --index 000300 --limit 30 \
  --start 2021-01-01 --end 2025-12-31 \
  --adjust hfq --workers 1 \
  --output data/real/csi300_30_hfq.csv
```

Tushare PIT 数据（需要 token）：

```bash
export TUSHARE_TOKEN=...
PYTHONPATH=. .venv/bin/python -m stockquant fetch-tushare \
  --index 399300.SZ --limit 50 \
  --start 2020-01-01 --end 2025-12-31 \
  --output data/real/tushare_csi300.csv
```

AKShare 当前成分接口仍存在存活者偏差；真正的历史成分和公告日财务数据优先使用 Tushare 或授权数据源。

## 配置

常用参数：

```text
--rebalance monthly|weekly|daily
--max-positions 10
--max-weight 0.12
--sector-max-weight 0.35
--cash-buffer 0.03
--target-volatility 0.18
--commission-bps 3
--stamp-duty-bps 5
--slippage-bps 5
--no-target-vol
--no-sector-neutral
```

## 测试

```bash
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

测试覆盖：

- 因子分布和动量方向
- 权重与行业上限
- 信号后下一交易日执行
- 绩效指标
- CSV 读写

## 代码结构

```text
stockquant/
  domain.py                  # Bar、MarketData、Trade、Position
  cli.py                     # demo/backtest/walkforward/inspect
  walkforward.py             # 滚动训练和测试
  report.py                  # HTML/JSON/CSV 报告
  data/
    csv_loader.py
    demo.py
    validation.py
  factors/
    basic.py
    engine.py
  portfolio/
    constructor.py
    risk.py
  backtest/
    engine.py
    metrics.py
```

## 为什么第一版不是 LLM Agent

当前版本刻意不使用 LLM 做投资信号：

1. LLM 无法替代 PIT 数据、财务修订、退市和停牌处理。
2. 先建立可回测的因子和组合层，再讨论自然语言解释。
3. 任何“智能体投票”都必须通过样本外、成本和风险验证，否则只是规则包装。
4. AutoWealth AI 值得参考的是产品结构和多智能体解释层，不是直接复用它的预测和回测核心。

## 与优秀项目的定位关系

- **Qlib**：后续接入作为 AI 因子研究和模型注册中心。
- **LEAN / NautilusTrader**：后续用于独立回测复核和实盘工程。
- **AutoWealth AI**：参考 Agent、FastAPI、Streamlit/Flutter 产品层。
- **Tushare / AKShare / yfinance**：当前通过 CSV 导入，后续写统一 provider adapter。
- **本文 MVP**：先实现一个不依赖框架、可看懂、可测试、无未来函数的底座。

## OKX 加密模拟盘

OKX Demo Trading 接入、IP 白名单、限价单/撤单、小资金市价闭环和
`crypto-live-run` 的命令见 [docs/OKX_DEMO_TESTING.md](docs/OKX_DEMO_TESTING.md)。

真实盘默认锁定；演示盘 API Key 不得开启提现权限。

## 当前限制

- 默认数据为合成数据，不代表真实收益。
- 还没有接入真实 PIT 财务数据库。
- 没有行业历史成分、指数历史成分和退市股票全集。
- 没有做冲击成本模型和成交量参与率约束。
- 组合优化目前是透明的权重约束，不是均值方差或 Black-Litterman。
- Walk-forward 是因子权重选择，不是完整机器学习训练流水线。
- 没有实盘下单接口。

## 下一步路线

### Phase 1：真实数据

- 接入 Tushare 或 AKShare A 股日线、财务和行业数据。
- 增加 `available_at` 字段和财报修订版本。
- 建立历史指数成分、退市股票和停牌数据库。

### Phase 2：模型

- 用严格时间切分训练 Ridge / LightGBM。
- 目标改为未来 20/60 日超额收益，而不是预测价格。
- 增加因子暴露、风格中性和风险模型。

### Phase 3：产品

- FastAPI 分析服务。
- Streamlit/Next.js 报告界面。
- 模型版本、数据版本和预测快照审计。
- 模拟盘和监控告警。

详见 [docs/ROADMAP.md](docs/ROADMAP.md)、[docs/RESEARCH_NOTES.md](docs/RESEARCH_NOTES.md) 和 [docs/SOURCES.md](docs/SOURCES.md)。
