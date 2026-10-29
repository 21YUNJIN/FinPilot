# S3 量化服务联调证据（自动生成）

- 引擎：`stockquant` 0.1.0，commit `5559e3f`，模块 `stockquant.backtest.metrics`
- 方法版本：`sq-wealth-risk/1.0.0`
- 数据：`synthetic_bank_catalog_v2`（合成净值，仅用于联调）
- 产品数：12，成功 12，可排序 11

## 口径检查

- [x] `source_hash_recomputed_client_side`
- [x] `cvar_withheld_exactly_when_below_252_returns`
- [x] `short_history_marked_not_usable`

## 排序偏好对照

- `stability`：首位 WM-CASH-001（A4 无量化首位 WM-CASH-001），名次变化 2 个产品
- `balanced`：首位 WM-CASH-001（A4 无量化首位 WM-CASH-001），名次变化 2 个产品
- `return`：首位 WM-BOND-102（A4 无量化首位 WM-CASH-001），名次变化 5 个产品

## 文件

| 文件 | 字节 | SHA-256 |
|---|---:|---|
| 01_meta.json | 2296 | `def283e9897bcef7…` |
| 02_risk_report.json | 71006 | `87c33345047df60a…` |
| 03_rank_balanced.json | 15488 | `e8b3dcf75beca51c…` |
| 03_rank_return.json | 14957 | `4acc7a8b1e695431…` |
| 03_rank_stability.json | 15127 | `b493d4febe0c1e74…` |
| 04_stress.json | 29015 | `b8a8fd8a99203a94…` |
| 05_policy_annotations.json | 2514 | `66cd6c9848ff2169…` |
| 06_h4_probe.json | 1055 | `dcdc06b693b61ad3…` |
| 07_invariants.json | 2564 | `adf91da2d8a5d38c…` |

本证据只证明“服务真的被调用、数字可独立复算、版本与来源可追”，不证明策略收益，也不替代 H4 在真实产品与真实用户上的评测。
