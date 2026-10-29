# FinPilot C03 集成手册：quant.risk_report / quant.rank / quant.stress

对应 V5：§11（S3）、§15（StockQuant 边界）、§16（契约）、§19.2 A4 消融、§19.3 指标。  
对应分工：C03「理财规则与 StockQuant」。

## 1 落点

| 本目录 | FinPilot 仓库目标路径 | 说明 |
|---|---|---|
| `finpilot_adapter/risk_report.py` | `services/api/src/finpilot/adapters/stockquant/risk_report.py` | 只读 HTTP 客户端；不 import StockQuant |
| `finpilot_adapter/policy.py` | `services/api/src/finpilot/adapters/stockquant/policy.py` | 服务不可用时的降级/阻断策略 |
| 本服务（独立进程） | 仓库外或 `services/quant-service/` | 用 StockQuant 的 Python 环境运行 |
| `contracts/openapi.yaml` | `contracts/openapi.yaml`（作为 quant 段） | 端点、Schema、错误码 |

FinPilot 的 `wealth_rules/suitability.py` 只消费 `user.risk_level` 与产品官方等级；`adapters/stockquant/` 不写银行余额、不签发授权、不改订单。

## 2 启动

```bash
export STOCKQUANT_ROOT=/path/to/stock-quant-mvp     # 或备份目录
export SQ_BANK_TOKEN='<32+ 随机字符>'
export SQ_BANK_PORT=8770
bash scripts/serve.sh
```

FinPilot 侧：

```bash
export STOCKQUANT_SERVICE_URL=http://127.0.0.1:8770
export STOCKQUANT_SERVICE_TOKEN='<同上>'
```

服务启动即校验：`SQ_BANK_TOKEN` 未设置且未显式 `SQ_BANK_DEV_OPEN=1` 时拒绝启动；不提供"默认无鉴权"的正式模式。

## 3 调用示例

```python
from finpilot.adapters.stockquant.risk_report import StockQuantRiskClient, QuantUnavailable

client = StockQuantRiskClient.from_env()          # 超时 3s，钉住方法版本
products = [{
    "product_id": "WM-BOND-102",
    "official_risk_level": "R2",
    "total_fee_bps": 30,
    "term_days": 0,
    "settlement_days": 2,
    "min_amount_minor": 10000,
    "nav": [{"date": "2026-10-22", "value": 1.0831}, ...],
}]

call = client.risk_report(products, as_of="2026-10-23")
report = call.data["products"][0]

vol = report["metrics"]["annual_volatility"]      # value 可能为 None
if vol["value"] is None:
    ...                                            # 显示 na_reason，不填 0
```

客户端对每个产品复算 `nav_source_hash(products[i].nav)` 并与响应的 `provenance.source_hash` 比对；不一致直接 `QuantContractError`。这条检查专门回应评审 §5.4「StockQuant 真实接入未证明」。

## 4 端点

| 方法 | 路径 | 用途 |
|---|---|---|
| GET | `/healthz` | 存活与引擎名 |
| GET | `/v1/meta` | 能力、引擎版本、口径门槛、排序权重、限制 |
| POST | `/v1/quant/risk-report` | 每个产品的指标与来源 |
| POST | `/v1/quant/rank` | 硬过滤 + 偏好排序 + A4 基线 |
| POST | `/v1/quant/stress` | 压力情景 |

响应统一信封（V5 §16.2）：`schema_version / request_id / status / data / error / provenance`。HTTP 200 不等于业务成功，必须看 `status`。

错误码：`INVALID_REQUEST`(400)、`UNAUTHORIZED`(403)、`PAYLOAD_TOO_LARGE`(413)、`UNPROCESSABLE_DATA`(422)、`RATE_LIMITED`(429)、`ENGINE_UNAVAILABLE`(503)、`INTERNAL`(500)。

`/v1/quant/risk-report` 对单个产品失败采用**逐产品隔离**：其余产品照常返回，失败项进 `failures[]`，不因一个坏序列让整页空白。

## 5 字段消费约定

- 指标是数据，不是权限：`observed_market_risk_band` 不能改写用户风险等级，也不能替代官方 R1—R5。
- `metrics.*.value` 为 `null` 时，UI 显示 `NA` + `na_reason` 的中文解释；禁止用 0、禁止隐藏。
- `data_window.end` 必须与页面"数据截至"一致；`stale_lag_days > 7` 时提示"净值未更新"。
- `warnings` 原样展示（含官方等级与统计档不一致的提示）。
- `ranked[].score` 是相对比较分，不是收益率、不是推荐强度、不能排序宣传语。
- `baseline_ranked` 与 `rank_changes` 只用于 H4/A4 评测与"为什么这样排序"的解释，不进入用户主界面。

## 6 服务不可用

| 场景 | 策略 |
|---|---|
| 指标只是比较页的说明列 | `RiskGate(require_metrics_for_ranking=False)`：标 NA，继续按条款/费用/流动性排序 |
| 指标是申购硬条件（银行策略要求） | `RiskGate(require_metrics_for_ranking=True)`：阻断该产品申购比较，提示稍后重试 |
| 超时 | 3s 超时，最多 1 次重试；仍失败按上表处理，不返回缓存旧值冒充当前值 |
| 版本不匹配 | 拒绝结果（`QuantContractError`），不自动降级到本地公式 |

量化不可用绝不能变成"风险低"。

## 7 验收与证据

| V5 测试族 | 本服务可提供的证据 |
|---|---|
| T46 | 同一净值快照重复请求，指标与 `source_hash` 一致；换风险偏好后排序变化；缺数据产品返回 NA |
| T50 | 不足 60/252 收益的边界样例；净值缺口、重复日期、未来数据的拒绝日志 |
| T51 | `/v1/meta` 引擎版本、commit、方法版本；请求/响应哈希；审计日志 |
| T53 | `rank_changes` 与 A4 基线，可直接进 H4 表 |

提交时应附：`evidence/RUN.md`、`evidence/SHA256SUMS.json`、`evidence/audit.jsonl`，以及一份**真实调用**记录（request_id + 客户端复算哈希 + 服务端审计行三者对齐）。

## 8 H4 实验怎么用本服务

1. 固定产品集合、固定 `as_of`、固定用户风险等级；
2. 只改 `user.preference_mode`（stability ↔ return），记录 `ranked` 顺序与 `rank_changes`；
3. 切到 A4：`include_quant=false` 或直接读同响应的 `baseline_ranked`；
4. 指标：排序依据正确率（人工判定是否与偏好一致）、风险约束满足率、不可计算比例、额外延迟（响应头/日志 `latency_ms`）。

现有 `evidence/06_h4_probe.json` 只是在合成数据上验证管道：`return` 模式首位从现金管理变为纯债基金，与 A4 首位不同。**这不是比赛成绩**，真实产品与真实偏好下必须重跑。

## 9 不做的事

- 不申购、不赎回、不生成交易指令、不读券商凭证；
- 不推断产品费率、期限、到账时间；
- 不输出"预期收益""稳赚""最大亏损"；
- 不把 StockQuant 的回测 CAGR、DSR、PBO 带进银行产品页。