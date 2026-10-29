# FinPilot 冻结数据与种子（DATA-03 / EVAL-01 / C01）

本目录是 **C 角色（金融分析与业务负责人）** 交付的冻结数据集、应用种子与来源清单，
对应 V5 需求 DATA-03、EVAL-01、GOV-02 与任务书工作包 **C01**。

> **合成数据声明：** 本目录全部金融数据（用户、账户、流水、持仓、卡片、订阅、商户、
> 报价、AA）均为**合成数据**，由 `data/synthetic/generate.py` 以固定种子 `20261022`
> 确定性生成，**不含任何真实个人金融信息**，也不代表任何真实银行的授权、认证或审批。
> 除注明外，数据可用于仓库内测试、演示与团队内部评测。

---

## 1 生成方式与重放

```powershell
# 在仓库根目录执行（PowerShell）
& ".\.venv\Scripts\python.exe" data\synthetic\generate.py --out-dir data
```

* 固定随机种子 `20261022`；`generated_at` 固定为 `2026-10-22T10:00:00+08:00`
  （与 `tests/conftest.py` 的 `REFERENCE_TIME` 一致），因此**两次运行输出字节一致**。
* 生成器只使用 Python 标准库；无网络、无数据库、无当前时间依赖。
* 全过程**不使用浮点数**：金额为整数分，份额为 1e-8 定点整数，净值为 1e-6 定点整数，
  日收益为整数基点；生成器在写出前用 `assert_no_floats` 校验。
* 生成器在写出前校验冻结 schema（键名与类型）与 AA 分摊合计，不符即失败退出（不产出脏数据）。

`--out-dir` 指定 `data` 目录；`--quiet` 只输出汇总行；`--repo-root` 可覆盖仓库根
（评测数据固定写到 `<repo-root>/eval/data`，便于在临时目录做双跑哈希比对）。

## 2 产物清单（seed-2026.10 / tasks-2026.10）

| 文件 | 内容 | 条数 | 字节 |
|---|---|---|---|
| `data/synthetic/seed_app.json` | 应用种子（后端 seeder 直接消费） | 2737 行（15 个集合） | 917,239 |
| `data/synthetic/tasks.json` | 600 个基础任务 | 600 | 598,766 |
| `data/metadata/source_manifest.json` | 来源、许可、合成比例与哈希 | 13 条 | 7,585 |
| `eval/data/attack_catalog.json` | 200 个攻击基础案例 | 200 | 141,066 |
| `eval/data/fault_catalog.json` | 58 个故障变体 | 58 | 27,424 |
| `eval/data/task_index.json` | 索引 + 拆分 + 数据集哈希 + 词汇表 | 1 | 28,889 |
| `eval/data/dev/tasks.json`、`dev/attacks.json` | 开发集 300 任务 / 100 攻击 | 300 / 100 | 303,337 / 70,346 |
| `eval/data/val/tasks.json` | 验证集 120 任务 | 120 | 115,190 |
| `eval/data/hidden/tasks.json`、`hidden/attacks.json` | 隐藏集 180 任务 / 100 攻击 | 180 / 100 | 180,245 / 70,723 |

合计 2,460,810 字节（11 个 JSON 产物）。体量以
`python data/synthetic/generate.py --out-dir data` 的实际输出为准；
`eval/data/task_index.json` 的 `artifacts` 字段记录每个产物的 `sha256`、`bytes` 与 `records`，
`dataset_hash` 覆盖除索引自身以外的全部产物。

## 3 种子数据（`seed_app.json`）

### 3.1 键名与字段（冻结 schema）

键名严格为：`dataset_version, generated_at, seed, users, accounts, protection_policies,
payees, billing_transactions, products, product_navs, holdings, cards, card_controls,
card_applications, subscriptions, mandates, merchant_offers, aa_requests`。
每个集合的字段键名与顺序固定，`tests/unit/domains/test_data_provenance.py` 逐字段断言。

### 3.2 与 `services/api/src/finpilot/models.py` 的映射

| 种子集合 | 目标表 | 备注 |
|---|---|---|
| `users` | `users` | `password` → `password_hash`（由 seeder 哈希）；`totp_secret_base32` → `totp_secret_enc`（由 seeder 加密）；`roles` 为 JSON 数组 |
| `accounts` | `accounts` | `posted_balance_minor` 为整数分；`account_type ∈ {salary, savings}` |
| `protection_policies` | `protection_policies` | `account_scope` 为账户 ID 数组 |
| `payees` | `payees` | `aliases` 为 JSON 数组，`destination_account_ref` 已脱敏 |
| `billing_transactions` | `billing_transactions` | 分析用流水，**与账本余额不联动**（余额由 `accounts.posted_balance_minor` 单点给定） |
| `products` / `product_navs` | `wealth_products` / `product_navs` | `nav_e6` 为 1e-6 定点整数；`daily_return_bps` 为整数基点（模型列声明为 Float，整数值合法且避免浮点漂移） |
| `holdings` | `holdings` | `*_shares_e8` 为 1e-8 定点整数 |
| `cards` / `card_controls` / `card_applications` | `cards` / `card_controls` / `card_applications` | `lifecycle_status ∈ {active, lost, inactive}`；`application_status` 与卡状态分离 |
| `subscriptions` / `mandates` | `subscriptions` / `mandates` | `evidence ∈ {contract, inferred}`；合同状态与代扣状态互相独立 |
| `merchant_offers` | 提供方沙箱商户目录（`services/provider-sandbox`） | `untrusted_text` 为不可信输入，用于注入类攻击评测 |
| `aa_requests[].shares` | `aa_requests` + `aa_shares` | `participant_ref` 使用真实 `user_id`，便于参与人各自授权付款 |

### 3.3 必须包含的业务事实

* 用户：`user_demo_01`（主）、`user_demo_02`（AA 参与人）、`user_demo_03`（AA 参与人）；
  口令仅用于本地演示（`Seed#2026a/b/c`），TOTP 为固定测试密钥，**不得用于任何真实系统**。
* 账户：`account_salary` 已记账余额 **900000 分（9000 元）**、`account_savings` 1500000 分；
  应急金 **800000 分（8000 元）** 作用于 `account_salary` → 可支配 1000 元。
* 同名联系人：两个 **“张三”**（`payee_zhangsan_work` / `payee_zhangsan_family`），
  脱敏目的账号分别为 `6222 **** **** 1001` 与 `6217 **** **** 2002`，别名不同。
* 账单流水：**24 个自然月**（2024-11 → 2026-10）共 **1257 条**（`user_demo_01` 1221 条，
  落在 1200—1800 的要求区间内），包含：
  * 内部互转（`kind=internal_transfer`，96 条，成对 `linked_bt_id` 关联）；
  * 理财申购/赎回（`kind=wealth_purchase|wealth_redeem`，32 条，`category=asset_allocation`，不冒充消费）；
  * 手续费单列（`kind=fee`，28 条，含对应原交易的 `linked_bt_id`）；
  * **跨期退款**（22 条：本期退款 + 上期/上上期原消费，`linked_bt_id` 指向原交易）；
  * 无法关联原交易的退款 2 条（`anomaly_label=unmatched_refund`，标记待核实）；
  * 重复同额扣费 2 组（`duplicate_same_amount`）、相同商户短时重复 2 组（`merchant_burst_repeat`，
    4 分钟内 3 笔）、显著偏离个人历史区间 2 笔（`outlier_vs_history`）；
  * 3 条账单备注注入样本（注入文本放在 `untrusted_text` 列，`memo` 保持正常）。
* 理财产品：7 只，风险等级覆盖 **R1—R5**；其中 `prod_tech_newissue_r3` **历史数据不足**
  （`history_ref` 为空、`product_navs` 无任何行 → 只能显示 NA）；`prod_equity_growth_r4`
  **不可撤单/无冷静期**；`prod_balance_mix_r3`（2 天）与 `prod_target_2030_r3`（3 天）**有冷静期**。
* 卡片：3 张（`card_salary_01` active 且同时有**用户锁 + 风控冻结 + 境外渠道锁**；
  `card_savings_02` lost；`card_virtual_03` inactive）。
* 订阅：月付 2 个、年付 2 个（均为 `evidence=contract`），另 2 个
  **仅有流水证据的疑似周期支出**（`evidence=inferred`，其中 `merchant_fitness` 的代扣已解除，
  用于“合同与代扣状态独立”场景）。
* 商户报价：鲜花 3、蛋糕 3、餐饮 4，含**缺货**（`inventory=0`）、**不可退**（`refundable=false`）、
  **取消费 > 0**（8 条）与含注入文本的商户描述。
* AA 历史请求：`aa_dinner_20260915`，实际支出 100 元三人 → **33.34 / 33.33 / 33.33 元**
  （最大余数法，按稳定参与人顺序），`linked_orders` 指向真实流水 `bt_aa_dinner_20260915`。
* 归属口径：600 个基础任务的 `user_id` 均为 `user_demo_01`（主用户，其账户/收款人/卡片/订阅
  才是这些任务的合法对象）。`user_demo_02`、`user_demo_03` 作为 **AA 参与人**与
  **跨用户隔离对象**存在；跨用户读写、代他人操作等覆盖由
  `cross_user_object_access` 攻击类（17 条）与 S1/S2/S4/S5 的应拒绝族承担，
  避免出现“对象不属于该用户”的不可执行任务。

## 4 时间与口径

* 时间：ISO 8601 带 `+08:00` 偏移（Asia/Shanghai 展示口径）；持久化由 seeder 转为 UTC。
* 业务日：`business_day` 为 `YYYY-MM-DD`，与 `posted_at` 的上海本地日期一致。
* 金额：整数分；**任何字段都不出现浮点数**（含清单中的 `synthetic_ratio`，
  本清单所有条目均为 `0`（无合成成分）或 `1`（全部合成），因此以整数表示，保证 JSON 可字节复现）。
* 版本：`seed-2026.10`（种子）、`tasks-2026.10`（任务）、`attacks-2026.10`、`faults-2026.10`；
  任何变更必须升版本号并重算 `eval/data/task_index.json` 的 `dataset_hash`。

## 5 来源清单（`data/metadata/source_manifest.json`）

每条含 `name, type(original|synthetic|public), license, synthetic_ratio, available_at,
sha256, notes, retention`。清单头部 `declaration` 明确：

* `all_financial_data_is_synthetic: true`、`no_real_personal_data: true`、
  `no_real_bank_authorization: true`；
* **`stockquant_license_verified: false`** —— StockQuant 仅登记为第三方代码且
  **许可证“待核验”**，本工作包未获取许可证文件、未核验、未复制其代码，**不声称已核验**；
* 模型/API 依赖的服务条款同样登记为待核验项。

## 6 拆分与隐藏集

拆分规则、泄漏检查、保管约定与重放校验命令见
[`eval/data/split_protocol.md`](../eval/data/split_protocol.md)。

要点：600 基础任务按 **模板族（template_family）+ 表达方式（phrasing_group）+ 实体池**
分组拆分，开发 300 / 验证 120 / 隐藏 180（每场景 50/20/30，类型比例保持 60%/20%/20%）；
**同一 `template_family` 与 `phrasing_group` 绝不跨拆分**（生成器与测试双重断言）。
隐藏集与开发集在**同一仓库**内受控维护，属于**团队内部评测**，不冒充第三方盲测。

## 7 自检命令

```powershell
# 1) 重新生成（字节一致）
& ".\.venv\Scripts\python.exe" data\synthetic\generate.py --out-dir data

# 2) 数据来源与拆分不变量测试（不需要 PostgreSQL）
& ".\.venv\Scripts\python.exe" -m pytest tests/unit/domains/test_data_provenance.py -q
```

`test_data_provenance.py` 覆盖：数量与比例、拆分与泄漏、金额整数性、种子 schema 键名与
类型、`models.py` 列映射、两次生成哈希一致、来源清单字段齐全、攻击/故障覆盖、
AA 分摊合计与最大余数、跨期退款关联等。

## 8 已知限制

* 本数据集是**合成评测集**：异常标签只存在于合成数据中；真实来源数据没有标签时
  不得据此虚报准确率（V5 第 10 节 S2-03）。
* 商户、商品与机构名称为虚构；报价、库存与条款用于业务规则验证，不代表任何真实商品。
* 隐藏集仅为团队内部隔离，**不等于独立第三方盲测**；揭盲修复后应转为已知回归集，
  新的泛化主张需要新的冻结集（V5 第 19.4 节）。