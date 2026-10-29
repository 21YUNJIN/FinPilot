# contracts —— 契约包（V5 API-01 / SYS-05 / GOV-01）

本目录是 FinPilot 并行开发的**唯一契约来源**：Schema、HTTP、工具目录、模块注册表与正反例。
任何实现必须与本文一致；需要变更时先改 `docs/INTERFACES.md` 与 `contracts/`，再改实现与消费者。

- 契约版本：`1.0`；策略版本：`policy-2026.10`。
- 主维护：D（汇总）；`openapi.yaml` 由 A 发布、端点行为由提供者确认；Schema 分领域归属（见 §5）。
- 规格来源：`docs/BANKAGENT_SRS_V5_COMPETITION.md`（V5 第 4、6、7、16 节与附录 A）与
  `docs/BANKAGENT_TEAM4_PLAN.md`（§4 契约表、附录 92 项需求映射）。

## 1 目录

| 路径 | 内容 | 校验 |
|---|---|---|
| `schemas/*.json` | JSON Schema 2020-12，`$id = https://finpilot.local/schemas/<name>.json` | `test_schemas.py` |
| `openapi.yaml` | OpenAPI 3.1，路径集合 = `INTERFACES.md` 第 4 节 | `test_schemas.py` |
| `tool-catalog.yaml` | 工具目录，名称集合 ⊇ `INTERFACES.md` 第 5 节 | `test_schemas.py` |
| `module-manifest.yaml` | 模块注册表（owner / path / provides / depends_on / status） | `test_schemas.py` |
| `examples/` | 每个 Schema 一正一反，反例必须真的被拒绝 | `test_schemas.py` |
| `../docs/requirement-trace.md` | 92 项需求 → 主责 → 工作包 → 测试族 → 实现状态 | `test_requirement_trace.py` |

## 2 输入面与输出面

同一份领域对象在契约层分成两面，避免把服务端字段暴露成可写字段：

| 输入面（`x-client-writable: true`） | 权威输出面 |
|---|---|
| `task.json`（`POST /v1/tasks` 请求体） | `task_view.json` |
| `operation_draft.json`（业务写操作登记请求） | `operation_view.json` |
| `grant_request.json`（`POST /v1/plans/{plan_id}/confirm` 请求体） | `grant.json`（服务端签发，客户端只能引用 `grant_id`） |
| `tool_request.json`（模型可见工具的调用信封） | 各领域 Schema + `error_envelope.json` |

输入面一律 `additionalProperties: false`，并用内部 `not` 显式禁止 `user_id`、`owner_user_id`、`grant_id`、`status`、
`required_level`、`posted_balance_minor` 等字段。拒绝原因写在 Schema 的 `description` 里：身份、归属、授权、
状态与余额事实只能由可信服务端产生，客户端或模型覆盖它们等于伪造授权或账本事实；命中时返回
`EXTRA_DANGEROUS_FIELD`（`errors.py`，HTTP 400）。`x-forbidden-client-fields` 列出被禁止字段，便于 SDK 生成器与测试读取。

## 3 全局口径（与 `docs/INTERFACES.md` §1 一致）

| 主题 | 契约表达 |
|---|---|
| 金额 | 一律 `type: integer`，单位分（`*_minor`）；禁止 `type: number`，`test_schemas.py` 全目录扫描断言 |
| 份额 | `shares_e8`：`type: integer`，1e-8 定点（单位 = 份 × 1e8） |
| 净值 | `nav_e6`：`type: integer`，1e-6 定点 |
| 比例 | 十进制定点**字符串**（如 `daily_return_bps: "0.0123"`），不使用 JSON number |
| 时间 | 时间戳 `type: string, format: date-time`（必须带时区）；业务日 `format: date` |
| 币种 | `const: "CNY"` |
| 枚举 | 与 `services/api/src/finpilot/models.py` 顶部常量**逐字一致**，由测试用 AST 解析后逐项比对 |
| 错误 | `error_envelope.error.code` 的枚举 = `errors.ERROR_SPECS` 键集合；`retry_mode` = `errors.RETRY_*` |

命名约定（由测试机械校验）：时间戳字段以 `_at` 结尾；日期字段以 `_date` 结尾；金额字段以 `_minor` 结尾；
份额字段以 `_e8` 结尾；净值以 `_e6` 结尾。

## 4 跨字段约束（`allOf` / `if-then`）

| Schema | 约束 |
|---|---|
| `grant` | `required_level=RED` ⇒ 必须有 `challenge_id` 且 `verification_level=TOTP`；`scope_kind=scoped` ⇒ 必须有 `max_uses` 与 `max_total_minor`；`scope_kind=exact` ⇒ `max_uses` 恒为 1 且 `lineage` 为空；`status=revoked` ⇒ 必须有 `revoked_at` |
| `plan` | `feasible=false` ⇒ `infeasible_reasons` 至少一条；`status=confirmed` ⇒ 必须有 `confirmed_at` |
| `operation_view` | `status=unknown` ⇒ `unknown_since`；`status=succeeded` ⇒ `effect_confirmed=true` 且 `settled_at`；`status=failed` ⇒ `error_code` |
| `policy_decision` | `RED` ⇒ `challenge_required=true`；`GREEN/YELLOW` ⇒ `challenge_required=false`；`allow=false` ⇒ 至少一个 `reason_code` |
| `task_view` | `status=blocked` ⇒ `blocked_reason`；`awaiting_authorization` ⇒ `waiting_reason`；终态 ⇒ `result_summary` |
| `wealth_product` / `wealth_order` | `nav_is_na=true` ⇒ 不得给出净值与估算／确认份额（未知净值不编造份额） |
| `aa_request` / `aa_share` | 应收不可花（`received_minor_is_spendable=false`）；`auto_debit_allowed` 只能为 false（发起人不能代扣） |
| `provider_event` | 验签失败或重复／过期／冲突 ⇒ `state_applied=false`；回调整体禁止携带 `owner_user_id`／`user_id` |
| `auth_challenge` / `audit_event` | 禁止出现 `factor_response`、`totp_code`、`otp`、`secret`、`chain_of_thought`、`password`、`full_pan` |
| `subscription` | `evidence=inferred` ⇒ 必须带置信度且 `is_contract_evidence=false`（疑似周期不冒充合同） |
| `card_control` | `risk_freeze`／`risk_limit` ⇒ `created_by_role=risk`；`active=false` ⇒ 必须有解除人与时间 |
| `operation_draft` / `tool_request` | 写工具 ⇒ 必须带 `idempotency_key`、`expected_revision`（及 `plan_revision`）；`transfer.*` ⇒ 必须带 `source_account_id` 与 `amount_minor` |

## 5 Schema 归属（分领域主责，来自 TEAM4 §3）

| 领域 | Schema | 主责 | 复核 |
|---|---|---|---|
| 身份／资金／授权／操作 | `auth_context`、`money`、`snapshot`、`snapshot_account`、`operation_draft`、`operation_view`、`grant`、`grant_request`、`grant_use`、`auth_challenge` | B | D；涉及前端展示时 A |
| 任务／目标／结果 | `task`、`task_view`、`goal`、`clarification`、`result_view` | A | D；财务字段由 B/C 核对 |
| 计划／策略／工具 | `plan`、`plan_step`、`policy_decision`、`tool_catalog_entry` | D | B；领域参数由 A/C 核对 |
| 金融业务 | `bill_report`、`wealth_product`、`risk_profile`、`wealth_order`、`subscription`、`mandate`、`reminder`、`merchant_quote`、`merchant_order`、`aa_request`、`aa_share`、`domain_result` | C（`wealth_order` 由 B 交付订单） | B；消费端 A/D |
| 卡片 | `card`、`card_application`、`card_control` | A | D；迁移由 B 检查 |
| 平台 | `error_envelope`、`provider_event`、`receipt`、`audit_event` | B/D | 交叉复核 |

## 6 HTTP 契约（`openapi.yaml`）

- 路径集合与 `docs/INTERFACES.md` 第 4 节**逐条相等**（含 `/internal/provider-events/{provider}` 与 `/operator/*`），由测试解析 markdown 表格比对。
- 统一错误响应 `$ref: schemas/error_envelope.json`；成功响应也引用对应领域 Schema。
- 写操作要求 `operation_id` 与 `expected_revision`：标注 `x-requires-operation-id: true`；不适用者必须给出
  `x-idempotency-exempt`（说明其幂等边界，如一次性验证码、签名回调去重）。
- `x-agent-exposed: false`：人类专用（确认、强验证、隐私）与运营／回调专用端点，模型工具目录不得暴露。
- `x-required-level`：`GREEN`／`YELLOW`／`RED`／`OPERATOR`；`NONE` 仅用于认证入口与提供方回调。
- 所有 `operationId` 为 snake_case 且唯一。

## 7 工具目录（`tool-catalog.yaml`）

共 36 项：33 个业务工具 + 3 个运营工具。

- 业务工具：`account.balance`、`payee.resolve`、`transfer.prepare`、`transfer.submit`、`transfer.status`、`schedule.create`、`schedule.cancel`、`schedule.status`、`aa.create`、`aa.status`、`bill.report`、`risk.assess`、`wealth.compare`、`wealth.purchase`、`wealth.redeem`、`wealth.status`、`card.apply`、`card.set_limit`、`card.set_control`、`card.report_lost`、`card.replace`、`card.status`、`subscription.list`、`subscription.cancel`、`mandate.revoke`、`mandate.status`、`reminder.create`、`reminder.cancel`、`merchant.quote`、`merchant.book`、`merchant.cancel`、`merchant.status`、`quant.risk_report`。
- 运营工具（`agent_exposed: false`）：`operator.card_decision`、`operator.risk_unfreeze`、`operator.case_action`。

每项含：`name`、`category`、`read_write`、`default_level`、`level_rule`、`agent_exposed`、`idempotent`、
`timeout_seconds`、`input_schema`、`output_schema`、`preconditions`、`effects`、`compensation`、
`sensitive_fields`、`error_codes`、`implemented_by`。

硬约束：

- **未分级的写操作不允许出现在目录中**：每个写工具都有 `default_level`，且 `level_rule` 说明升级条件。
- `transfer.prepare` **不产生外部支付**：只登记操作草稿与参数哈希，不消费授权、不改余额、不创建预留。
- `transfer.submit` **只接受已登记并获授权的 operation 引用**，不接受模型任意新拼装的转账参数。
- `transfer.submit` 的等级为 `YELLOW/RED`：当日累计本金（含待执行占用）恰好 100000 分为 YELLOW，超过为 RED；
  手续费不计入本金累计，并发分拆不能绕过升级。
- `subscription.list` 即使只读也是 `YELLOW`（官方黄色例外，不能用“一切只读均绿色”覆盖）。
- `OPERATOR` 工具 `agent_exposed` 必须为 `false`，不能由 Agent 或客户直接批准。

## 8 模块注册表（`module-manifest.yaml`）

`id`、`owner`（A/B/C/D）、`co_owners`、`path`、`provides`、`depends_on`、`status`（`planned|in_progress|integrated`）、
`work_packages`（对应 TEAM4 附录 24 个工作包）。`status: in_progress` 的模块 `path` 必须真实存在（测试断言）。
24 个工作包必须至少被一个模块认领。

## 9 变更协议（TEAM4 §4.4、GOV-01）

1. 新增可选字段也要先加契约与正反例，并通过 `pytest tests/contracts`。
2. 删除／改名、状态含义改变、金额口径改变、权限等级变化：必须列出全部消费者，先做兼容提供方，再迁移消费者，
   最后删除旧字段；未迁移完成不得合入 main。
3. 契约版本、Schema、样例、测试与文档在同一组 PR 中更新，并记录交叉复核人。

## 10 校验

```powershell
& ".venv\Scripts\python.exe" -m pytest tests/contracts -q
```

`tests/contracts` 是纯静态契约测试，不需要 PostgreSQL（在测试模块内覆盖了 `tests/conftest.py` 的自动 `clean_db` 夹具）。
