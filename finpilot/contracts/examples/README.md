# contracts/examples —— Schema 正反例

每个 `contracts/schemas/<name>.json` 都配有一对样例文件：

- `<name>.positive.json`：必须通过该 Schema 校验；
- `<name>.negative.json`：必须被该 Schema 拒绝。

反例文件内部**不写** `__why_invalid`、`$comment` 之类会污染载荷的说明字段——说明文字放在本文件下方表格，
所以反例是“干净”的真实请求体：把它直接发给服务端，也会被服务端以同样的理由拒绝。
`tests/contracts/test_schemas.py` 对每个反例断言 `list(validator.iter_errors(x)) != []`，并在失败时报出反例文件名。

共 39 个 Schema、80 个样例（每个 Schema 至少一对，另附若干补充反例）。生成源与校验命令见 `contracts/README.md`。

## 正例覆盖的共享样例

正例统一采用 `docs/BANKAGENT_TEAM4_PLAN.md` §4.3 的共享样例：`user_demo_01` / `account_salary`、
余额 900000 分、应急金 800000 分、币种 CNY、时区 Asia/Shanghai；转账样例 10000 分、生日任务预算 80000 分。

## 反例清单与违反的约束

| 反例文件 | 对应 Schema | 违反的约束（必须被拒绝的理由） |
|---|---|---|
| `aa_request.negative.json` | `schemas/aa_request.json` | AA 应收被标记为可花（received_minor_is_spendable=true）；未到账的分摊款不是可用资金。 |
| `aa_share.negative.json` | `schemas/aa_share.json` | auto_debit_allowed=true；AA 只发请求，不得代扣任何参与人，付款须各自独立授权。 |
| `audit_event.negative.json` | `schemas/audit_event.json` | 审计事件写入模型思维链与 TOTP 验证码；审计不得保存思维链、验证码或任何秘密。 |
| `auth_challenge.negative.json` | `schemas/auth_challenge.json` | 挑战对象里回传了明文 factor_response 验证码；验证码不得进入对象、日志或模型上下文。 |
| `auth_context.negative.json` | `schemas/auth_context.json` | roles 出现未定义角色 root（枚举外）。身份与角色只能由服务端会话产生，客户端不能自封超级角色。 |
| `bill_report.negative.json` | `schemas/bill_report.json` | period.kind=custom 但缺少 start_date／end_date；自选账期必须给出明确边界。 |
| `card.negative.json` | `schemas/card.json` | 卡对象出现完整卡号 pan、CVV，且 masked_pan 未脱敏；完整卡号与 CVV 不得出现在接口、日志或模型上下文。 |
| `card_application.negative.json` | `schemas/card_application.json` | 申请被拒绝却没有 decision_reason；拒绝必须给出可追溯理由，且不能通过重新发问变成批准。 |
| `card_control.negative.json` | `schemas/card_control.json` | 风控冻结由 customer 角色创建；风控限制只能由风控角色产生，客户与 Agent 无权解除或伪造。 |
| `clarification.negative.json` | `schemas/clarification.json` | status=answered 但缺少 answer 与 answered_at；已回答的澄清必须留下答案与时间。 |
| `domain_result.negative.json` | `schemas/domain_result.json` | source=model_guess 不在来源枚举内；结果来源必须是可追溯的账本／提供方／合成数据，不能是模型猜测。 |
| `error_envelope.negative.json` | `schemas/error_envelope.json` | status=ok 同时携带 error 对象；成功信封的 error 必须为 null，否则调用方无法判断业务状态。 |
| `goal.negative.json` | `schemas/goal.json` | 把未确认应收当作可用资金（unconfirmed_receivables_are_spendable=true），违反 V5 第 5.1、8.1 节的硬约束。 |
| `grant.negative.json` | `schemas/grant.json` | RED 授权缺少已通过的 challenge 引用且 verification_level=NONE；红色动作不能靠点击或模型判断取得授权。 |
| `grant_request.negative.json` | `schemas/grant_request.json` | 客户端自带 grant_id／status／required_level／scope，企图自签授权并降低等级、放大金额。 |
| `grant_use.negative.json` | `schemas/grant_use.json` | use_status=consumed 但 amount_minor=0；已消费的授权必须对应正金额的资金效果。 |
| `mandate.negative.json` | `schemas/mandate.json` | status=revoked 却没有 revoked_at；代扣解除必须留下时间证据。 |
| `merchant_order.negative.json` | `schemas/merchant_order.json` | status=cancelled 却没有 actual_cost_minor；取消后的实际成本（含取消费）必须落账并用于 AA 分摊。 |
| `merchant_quote.negative.json` | `schemas/merchant_quote.json` | refundable=false 却缺少 cancel_fee_minor；不可退服务必须显式给出取消费。 |
| `money.negative.json` | `schemas/money.json` | 金额以浮点 number 提交（100.5）。契约要求 type=integer（整数分），禁止 number。 |
| `money.negative.negative_amount.json` | `schemas/money.json` | 负数金额。契约要求 amount_minor >= 0；退款必须使用独立正金额退款操作与方向标识（V5 第 4.1 节）。 |
| `operation_draft.negative.json` | `schemas/operation_draft.json` | 缺少 expected_revision（并发基线）。写操作没有版本基线就无法区分先后，必须返回 VERSION_CONFLICT 语义。 |
| `operation_view.negative.json` | `schemas/operation_view.json` | status=succeeded 但 effect_confirmed=false；HTTP 200 或模型措辞都不能把未确认效果写成成功。 |
| `plan.negative.json` | `schemas/plan.json` | feasible=false 却没有任何 infeasible_reasons；不可行必须给出可核查的冲突条件。 |
| `plan_step.negative.json` | `schemas/plan_step.json` | status=unknown 但没有绑定 operation_id；未知结果必须用原操作 ID 查询，不得当成失败重做。 |
| `policy_decision.negative.json` | `schemas/policy_decision.json` | RED 却标记 challenge_required=false；红色动作必须走真实验证（AUTH-02/AUTH-05）。 |
| `provider_event.negative.json` | `schemas/provider_event.json` | 回调载荷携带 owner_user_id，企图覆盖本地订单归属；回调不能改写归属。 |
| `receipt.negative.json` | `schemas/receipt.json` | state=conflict 却没有 conflict_reason；冲突终态回执必须隔离并记录原因，不能按到达顺序改账。 |
| `reminder.negative.json` | `schemas/reminder.json` | mode=live 却标记 simulated=true（或反之）；模拟通知必须与真实通知明确区分。 |
| `result_view.negative.json` | `schemas/result_view.json` | 已完成效果 verified=false（未核验）却出现在 completed_effects；结果视图只能展示已核验事实。 |
| `risk_profile.negative.json` | `schemas/risk_profile.json` | risk_level=R5 但 max_loss_tolerance=low；测评等级与可承受损失必须一致，否则适当性判断失真。 |
| `snapshot.negative.json` | `schemas/snapshot.json` | 快照声明未确认应收可花（true），会把待收 AA／退款误当可用资金（V5 第 8.1 节）。 |
| `snapshot_account.negative.json` | `schemas/snapshot_account.json` | 账户 status=frozen 但 spendable_minor 仍为 50000；冻结账户的可支配金额必须为 0。 |
| `snapshot_account.negative.float_balance.json` | `schemas/snapshot_account.json` | 余额以浮点 number 提交（900000.5）。银行余额一律整数分，禁止浮点隐式取整。 |
| `subscription.negative.json` | `schemas/subscription.json` | evidence=inferred 却缺少 inference_confidence_bps 与 is_contract_evidence=false；疑似周期不得冒充合同。 |
| `task.negative.json` | `schemas/task.json` | 请求体携带 user_id／owner_user_id／status 等由服务端持有的字段（冒充他人身份并预置成功状态）。 |
| `task_view.negative.json` | `schemas/task_view.json` | status=blocked 却没有 blocked_reason；阻塞状态必须给出可执行的阻塞原因，否则界面无法解释“不是成功”。 |
| `tool_catalog_entry.negative.json` | `schemas/tool_catalog_entry.json` | 写工具 default_level=UNGRADED（未分级）；工具目录不允许出现未分级的写操作。 |
| `tool_request.negative.json` | `schemas/tool_request.json` | 工具调用信封携带 required_level=GREEN 与自带 grant_id，企图把红色转账降级为绿色并自带授权；写工具还必须带 idempotency_key 与 expected_revision。 |
| `wealth_order.negative.json` | `schemas/wealth_order.json` | side=redeem 却没有 redeemed_shares_e8；赎回必须按可用份额校验，不能只给金额。 |
| `wealth_product.negative.json` | `schemas/wealth_product.json` | nav_is_na=true 却给出 nav_e6 净值；数据不足时必须显示 NA，不得编造净值。 |
