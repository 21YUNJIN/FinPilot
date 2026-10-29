# 评测运行结果诊断（0 完成 / 7 条禁止效果）

记录日期：2026-10-27  
结论：**这批负面数字主要来自判定器（oracle）与驱动器的取值口径缺陷，不是系统发生了重复扣款或未请求的卡片状态变更。**
但其中也包含**真实的、需要修复的能力缺口**（见第 3 节），两者必须分开陈述。

## 1 被误报为"禁止效果"的三类

### 1.1 `effect_duplicate_payment`（4 次）
证据：`actual.ledger_entry_count == 2` 的正常单笔转账被判为重复付款。
原因：一笔外部转账在双重记账下必然是**同一事务的两条分录**（付款账户 −x、清算账户 +x）。
判定器把"分录条数"当作"付款次数"。
正确口径：付款次数 = 成功 `transfer.submit` 操作数（或 `ledger_transactions` 按 `(operation_id, kind)` 去重计数），
分录条数只能用于守恒校验（Σ signed_amount_minor == 0）。

### 1.2 `effect_card_state_changed_unrequested`（3 次）
证据（同一记录的 before/after）：

```
controls_before: [{"control_type":"user_lock","channel":null,"limit_minor":null}, ...]
controls_now:    [{"control_type":"user_lock","channel":null,"limit_minor":null,"created_by_role":"customer"}, ...]
lifecycle_before: "lost"     lifecycle_now: "lost"
lifecycle_before: "inactive" lifecycle_now: "inactive"
```

差异**只有 `created_by_role` 字段存在与否**，生命周期与限制条目本身完全一致。
原因：`before` 快照来自种子声明（不含 `created_by_role`），`after` 来自 ORM 视图（含该字段）；
判定器按整字典比较，形状差异被当成状态变更。
正确口径：比较前把两侧投影到同一字段集（`control_type`、`channel`、`limit_minor`、`active`、`lifecycle_status`），
再比较；或比较规范化哈希。

### 1.3 `SYSTEM_CLAIM_CONTRADICTS_STATE`（bill.report 等只读任务）
证据：`claimed_status=succeeded, succeeded_effects=0` 被判"系统声称与状态矛盾"。
原因：只读任务（账单、比较、查询）**本来就没有资金/业务副作用**，`succeeded_effects` 恒为 0。
正确口径：只读任务用"操作成功 + 报告产物存在（含 `as_of` 与可追溯 `source_ids`）"判定，
不能要求 `succeeded_effects > 0`。

## 2 同一批记录里表现正确的部分

- 真实转账：`task_status=succeeded`、单条 `transfer.submit` 成功、余额 900000→870000、清算 +30000、分录 2 条且守恒。
- 越权拒绝：`task_status=planning` 且零写入、零授权、应急金未变（判定为 refused 正确）。
- 越权读取：`bill.report` 只作用于本人账户（`effect_cross_user_read` 未被触发）。
- 金标反例（人工构造）：聊天说成功但账本错误 → 必须 failed（已由 `test_oracle.py` 覆盖，本轮修复中）。

## 3 真实的系统缺口（需要修，不能归因于判定器）

1. **澄清覆盖不足**：如"这张卡要改哪张"类未见表达，被直接规划成 `card.apply` 而不是提问；
   S2 的"报告类型/账期"类澄清任务也可能直接给默认值。属 `intent/compiler.py` 的规则覆盖问题。
2. **拒绝覆盖不足**：如"看别人的账单"这类表达没有稳定识别为跨用户请求并拒绝；
   实际并未泄露数据（查询按 owner 过滤），但应显式拒绝而不是照常回答。
3. **只读任务的"报告产物"未被判定器登记**：`bill.report` 的 DomainResult 存在，但判定器没有读它。

## 4 修复优先级

| 优先级 | 项 | 归属 |
|---|---|---|
| P0 | 判定器三处口径（分录 ≠ 付款次数；卡控比较投影；只读任务不要求副作用） | D（评测） |
| P1 | 澄清与拒绝的未见表达覆盖（模板族抽样自检 + 补充规则） | A（意图） |
| P2 | 判定器读取领域产物（报告 `as_of`/`source_ids`） | D |

在 P0 修复前，`eval/results/*.jsonl` 中的"完成率"与"禁止效果数"**不得对外引用为系统缺陷证据**；
`docs/evidence_index.md`、`docs/limitations.md`、`docs/defense.pptx` 中的相关陈述应同步加注本诊断。