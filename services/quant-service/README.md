# StockQuant 银行版：S3 理财风险服务

把 StockQuant 从"股票研究系统"改造成 FinPilot 六场景中**第三模块 S3 理财**的只读风险内核。

- 输入：银行理财产品的**净值序列**与条款字段（不是股票代码、不是 A 股行情）；
- 输出：波动、回撤、CVaR、Sharpe、压力情景、可复现的偏好排序；
- 边界：不申购、不赎回、不下单、不推断费率/期限/到账、不改用户风险等级；
- 形态：独立 HTTP 服务 + FinPilot 侧只读客户端，两个 Python 环境不混用。

决策与理由见 [docs/DECISION_S3_QUANT.md](docs/DECISION_S3_QUANT.md)，集成步骤见 [docs/INTEGRATION_C03.md](docs/INTEGRATION_C03.md)，口径见 [docs/METHOD_CARD.md](docs/METHOD_CARD.md)。

## 快速开始

服务必须跑在 StockQuant 的 Python 环境里（需要 `flask`、`requests`、`numpy`；本机实测为 `stock-quant-mvp/.venv` 的 Python 3.9.6）。下面的 `python` 均指该解释器。

```bash
cd s3-quant-service
export STOCKQUANT_ROOT=/path/to/StockQuant_Backup_2026-10-28   # 需含 stockquant/ 包
export PYTHONPATH="$STOCKQUANT_ROOT:."

python scripts/generate_synthetic_catalog.py     # 已生成时可跳过
python -m pytest                                 # 60 passed
SQ_BANK_ENGINE_COMMIT=5559e3f python scripts/run_evidence.py

export SQ_BANK_TOKEN="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
bash scripts/serve.sh
# 另一个终端
curl -s http://127.0.0.1:8770/v1/meta | python -m json.tool | head -30
```

`scripts/serve.sh` 要求设置 `SQ_BANK_TOKEN`；只有显式 `SQ_BANK_DEV_OPEN=1` 才允许本机无令牌演示，且 `/v1/meta` 会标 `auth: dev_open`。

## 目录

```text
stockquant_bank/          服务实现（指标、NAV 校验、报告、排序、压力、HTTP、审计）
finpilot_adapter/         放进 FinPilot 的两个文件：只读客户端 + 降级策略
contracts/openapi.yaml    端点与 Schema
data/                     合成产品目录与净值（固定 seed，可重建）
scripts/                  生成数据、启动服务、生成证据
tests/                    60 项：公式、口径、契约、端到端
docs/                     决策、方法卡、集成手册、证据
evidence/                 本次实际调用输出与 SHA-256 索引
```

## 关键设计

1. **复用真实内核**：指标直接 import `stockquant.backtest.metrics`（`daily_returns`、`max_drawdown`、`sharpe_statistics`），CVaR 尾部分位与 `stockquant.portfolio.optimizer.portfolio_cvar` 做数值一致性测试。没有"看起来像量化"的本地替代公式。
2. **口径守卫**：≥60 收益才算年化波动，<252 收益不报正式 95% CVaR，数据不足返回 `null` + 原因；最大回撤明确是观察窗口统计，不是损失上限。
3. **硬约束先于排序**：风险等级、期限、流动性、起购金额先过滤，分数不能把不合格产品拉回来。
4. **A4 消融内置**：同一响应同时给 `ranked` 与 `baseline_ranked`，H4 不需要跑两套系统。
5. **可追溯**：每个产品净值 SHA-256，客户端独立复算；方法版本钉住；审计日志含 request_id、响应哈希、延迟。
6. **失败有语义**：单产品坏数据不会让整页失败；服务不可用分"标 NA 继续"与"硬门槛阻断"两种策略，绝不把缺数据当低风险。

## 已知限制

- 数据是合成的（`synthetic_bank_catalog_v2`），只验证管道，不构成市场结论；H4 尚未在真实产品/用户上跑。
- 净值频率假定日频；周频/月频产品需调用方换算或标不可用。
- 单进程 Flask + 内存限流，适合比赛联调，不是生产部署；多实例需共享缓存与审计。
- StockQuant 的因子、选股、回测收益、DSR/PBO 与交易执行**没有**进入本服务。

## 许可

StockQuant 为 MIT；本目录新增代码沿用 MIT，保留原版权声明。合成产品数据仅用于本项目演示。