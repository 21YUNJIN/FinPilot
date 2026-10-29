# 证据：本机实际运行记录（2026-10-28）

## 1 环境

| 项 | 值 |
|---|---|
| 机器 | macOS，本机 |
| Python | 3.9.6（StockQuant 的 venv：`stock-quant-mvp/.venv`） |
| Flask / requests / numpy | 3.1.3 / 2.32.5 / 2.0.2 |
| StockQuant 来源 | `StockQuant_Backup_2026-10-28`（源目录 `stock-quant-mvp`，HEAD `5559e3f`，分支 `codex/finpilot-agent`） |
| StockQuant 行情数据 | **未包含在备份中**（`data/real` 约 5.5 GB 被排除），本服务不依赖它 |
| 方法版本 | `sq-wealth-risk/1.0.0` |
| 产品数据 | `synthetic_bank_catalog_v2`，12 个产品，994 个交易日/个（1 个新发产品 45 点） |

## 2 测试

```bash
export STOCKQUANT_ROOT=/path/to/StockQuant_Backup_2026-10-28
export PYTHONPATH="$STOCKQUANT_ROOT:."
python -m pytest
# 60 passed
```

覆盖：

- 指标公式与 StockQuant `portfolio_cvar` 的**数值一致性**（尾部分位定义）；
- 60 / 252 样本门槛的边界（59 vs 60、251 vs 252 收益）；
- 净值重复日期、0/负值、未来数据、超长序列的拒绝；
- 官方风险等级与统计档不一致时的 warning；
- 硬过滤（风险、期限、流动性、起购）不能被排序分数推翻；
- 稳定偏好与收益偏好的排序差异、A4 基线、同输入可复现；
- HTTP 令牌、信封、错误码、逐产品隔离、审计日志不含令牌；
- 真实 HTTP 往返 + 客户端独立复算 `source_hash` + 方法版本钉住 + 服务不可达。

## 3 端到端调用（真实 loopback HTTP）

`evidence/RUN.md` 为自动生成，含 12 个产品调用 `/v1/quant/risk-report`、三种偏好的 `/v1/quant/rank`、`/v1/quant/stress`，以及 9 个 JSON 证据文件和 SHA-256 索引。

关键结果：

- 12/12 产品计算成功，11/11 可参与排序；新发产品（45 点）被标为 `INSUFFICIENT_NAV_HISTORY`，不参与量化排序，但在 A4 基线中保留，保证两组可比；
- 12 个产品的客户端复算哈希与服务端 `source_hash` **全部一致**；
- 全部产品均满足「不足 252 收益不报 CVaR」口径：唯一不足 252 收益的新发产品（45 点）其 `historical_cvar_95` 为 `null` 且带 `na_reason`，其余 11 个产品信度为 993 个收益；60 收益门槛的边界由单元测试覆盖；
- 同一批产品、同一 `as_of`，只改偏好：`stability` 首位为现金管理，`return` 首位变为纯债基金，A4 基线首位仍为现金管理。

延迟（审计日志，单进程 Flask dev server，12 产品 × 994 点）：239—1187 ms；同一快照重复调用走指标缓存后约 108 ms。正式联调建议按 `(product_id, source_hash, method_version)` 做共享缓存，并在 UI 显示数据截至日而不是服务器时间。

## 4 这份证据不能证明什么

- **不能证明真实产品风险或收益**：净值是合成的，只用于验证计算、契约、消融管道。
- **不能证明 H4 已达标**：偏好导致的排序变化是在合成目录上得到的；真实产品、真实用户偏好、冻结任务下的 H4 仍需 C/D 执行。
- **不能证明生产可靠**：单进程 Flask、内存限流、单机审计；没有压测、没有多实例一致性、没有 KMS/密钥轮换。
- **不能替代许可与数据授权**：StockQuant 为 MIT，但真实产品净值的使用需要另行授权；当前合成数据无此问题。

## 5 复现步骤

```bash
cd s3-quant-service
export STOCKQUANT_ROOT=/path/to/StockQuant_Backup_2026-10-28
export PYTHONPATH="$STOCKQUANT_ROOT:."
python scripts/generate_synthetic_catalog.py     # 重新生成合成目录（固定 seed）
python -m pytest                                 # 60 passed
SQ_BANK_ENGINE_COMMIT=5559e3f python scripts/run_evidence.py
```

输出写入 `evidence/`；`RUN.md` 与 `SHA256SUMS.json` 每次运行覆盖，`audit.jsonl` 追加。