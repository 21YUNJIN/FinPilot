# 融策智能体（FinPilot）冻结接口与协作约定 v1.0

本文件是并行开发的唯一接口来源。任何模块的实现必须与本文一致；需要变更时先改本文与
`contracts/schemas/`，再改实现与消费者。对应 V5 第 4、5、6、7、8、16 节。

**状态：** 已冻结（R0）。变更需在 PR 中列出全部消费者与兼容方案。

---

## 0 仓库结构

```text
finpilot/
  contracts/                     # 契约：Schema、OpenAPI、工具目录、模块清单、正反例
  services/api/src/finpilot/     # 后端应用（唯一可信执行实现）
    config.py errors.py envelope.py ids.py money.py timeutil.py db.py models.py
    gateway/                     # HTTP 入口：会话、任务、确认、结果、控制、隐私、运营、回调
    intent/                      # GoalSpec 编译与澄清
    kernel/                      # 身份、账本、预留、授权、提交事务、操作、调度、对账、熔断、适配器
    workers/                     # Outbox、调度器、对账、运行入口
    orchestration/               # DAG 规划、执行协调、增量恢复、接管、任务仓储
    security/                    # 策略、范围校验、卡控规则、信任边界、沙箱、审计
    domains/                     # cards billing wealth_rules wealth_orders subscriptions aa
    adapters/                    # stockquant merchant（客户端侧适配器）
  services/provider-sandbox/     # 独立提供方：bank.py（B）、merchant.py（C），各自独立状态库
  migrations/                    # Alembic 迁移链（B 维护顺序）
  tests/{unit,contracts,integration,security,e2e}
  eval/                          # 运行器、判定器、基线、形式化、统计、数据
  data/synthetic/ data/metadata/ # 合成数据生成与来源清单
  apps/{web,im,shared-ui}        # 两个客户端 + 共享组件（pnpm workspace）
  deploy/                        # 部署、备份恢复、沙箱策略、本地脚本
  docs/                          # 技术文档、安全自评、证据索引、追溯矩阵
```

## 1 全局口径

| 主题 | 约定 |
|---|---|
| 金额 | `amount_minor: int`（整数分），币种固定 `CNY`；解析用 `money.parse_amount_minor`，超两位小数报 `AMOUNT_PRECISION` |
| 份额 | `shares_e8: int`（1e-8 定点），`money.parse_shares_e8` / `format_shares_e8` |
| 时间 | 持久化 UTC（`timestamptz`）；展示用 `timeutil.format_iso`（Asia/Shanghai）；业务日 `timeutil.business_day` |
| ID | `ids.new_id(prefix)`；对外暴露的稳定 ID 形如 `op_xxx`、`task_xxx`、`grant_xxx` |
| 哈希 | 一律 `ids.canonical_json` + `sha256`；UI 摘要与后端校验同源（`ids.scope_hash` / `ids.parameters_hash`） |
| 状态 | 见 `models.py` 顶部常量；状态转移必须经 `kernel/state_machine.py` 校验 |
| 身份 | 只来自服务端会话（`sessions` 表）；请求体、模型输出不得覆盖 `user_id` |
| 运行模式 | `settings.app_mode ∈ {live, offline_replay, mock}`，所有响应 `provenance.adapter` 与任务 `mode` 字段必须如实标注 |

**错误与信封：** 所有 HTTP 返回 `finpilot.envelope` 结构；业务状态看 `status` 字段，
HTTP 200 不等于成功。异常统一抛 `FinPilotError(code, message=None, details={})`。

## 2 内核接口（B 提供，其他模块只通过它写资金）

```python
# kernel/identity.py
def authenticate(session: Session, token: str) -> AuthContext
def create_session(session: Session, user_id: str, password: str, channel: str) -> tuple[str, AuthContext]
def register_user(session: Session, user_id: str, display_name: str, password: str, *, phone_ref: str = "") -> User
def enroll_totp(session: Session, ctx: AuthContext) -> dict          # {secret, otpauth_uri, confirmed: False}
def confirm_totp(session: Session, ctx: AuthContext, code: str) -> bool

# AuthContext: dataclass(user_id, roles: tuple[str, ...], channel, session_id, is_operator: bool)
```

```python
# kernel/snapshots.py
def get_snapshot(session, ctx: AuthContext, account_ids: list[str] | None = None, task_id: str | None = None) -> dict
# 返回：{"accounts":[{account_id, currency, posted_balance_minor, reserved_minor, frozen_other_minor,
#                    available_minor, spendable_minor, version, protected_floor_minor}],
#        "protected_floor_minor": int, "protection_scope": [...], "observed_at": iso,
#        "pending_inflows_minor": int, "unconfirmed_receivables_minor": int}
def account_balances(session, account_ids: list[str]) -> dict[str, dict]
```

```python
# kernel/ledger.py
def post_transaction(session, *, kind: str, entries: list[tuple[str, int]], operation_id: str | None,
                     task_id: str | None = None, memo: str = "", posted_at: datetime | None = None) -> str
# entries: [(account_id, signed_amount_minor), ...]；同币种分录之和必须为 0，否则抛 SYSTEM_ERROR
def account_available_minor(session, account_id: str) -> int
def reserved_minor(session, account_id: str) -> int
def ledger_history(session, *, owner_user_id: str, account_id: str | None = None, start=None, end=None,
                   limit: int = 200) -> list[dict]   # 只读、按归属过滤（实现签名以此为准）
```

```python
# kernel/reservations.py
def create_reservation(session, ctx, *, account_id: str, amount_minor: int, task_id: str | None,
                       purpose: str = "task_budget", long_lived: bool = False,
                       expires_at: datetime | None = None) -> Reservation
def capture_reservation(session, ctx, *, reservation_id: str, amount_minor: int, operation_id: str | None = None,
                        counterparty_account: str | None = None, memo: str = "") -> Reservation
def release_reservation(session, ctx, *, reservation_id: str, amount_minor: int | None = None,
                        reason: str = "") -> Reservation
def expire_idle_reservations(session, *, now: datetime | None = None) -> list[str]
# 不变量：B=posted_balance；H=Σremaining；可用=B−H−F；可支配=max(0,B−H−F−E)
# 捕获 x：B 与 H 同减 x（禁止双减）；释放只释放确认未占用部分
```

```python
# kernel/limits.py
def grade_transfer(session, ctx, *, principal_minor: int, business_day: date | None = None) -> str  # GREEN/YELLOW/RED
def hold_daily_quota(session, ctx, *, operation_id: str, principal_minor: int, business_day: date) -> None
def settle_daily_quota(session, ctx, *, operation_id: str, principal_minor: int, business_day: date) -> None
def release_daily_quota(session, ctx, *, operation_id: str, principal_minor: int, business_day: date) -> None
def daily_usage(session, ctx, business_day: date | None = None) -> dict
# 边界：恰好 100000 分为 YELLOW，超过为 RED；手续费不计入本金累计；并发分拆不能绕过升级
```

```python
# kernel/grants.py
def create_exact_grant(session, ctx, *, plan_id: str, plan_revision: int, scope: dict,
                       required_level: str, ttl_seconds: int | None = None) -> Grant
def create_scoped_grant(session, ctx, *, plan_id: str, plan_revision: int, scope: dict, required_level: str,
                        max_uses: int, max_total_minor: int, expires_at: datetime) -> Grant
def activate_grant(session, ctx, *, grant_id: str, verification_level: str) -> Grant
def consume_grant(session, ctx, *, grant_id: str, operation_id: str, parameter_hash: str, amount_minor: int) -> GrantUse
def revoke_grant(session, ctx, *, grant_id: str) -> dict            # 返回 {"grant":..., "stopped_steps":[...], "inflight":[...]}
def scope_is_subset(approved_scope: dict, candidate_scope: dict) -> bool
def grant_covers(grant: Grant, *, plan_revision: int, candidate_scope: dict, amount_minor: int) -> tuple[bool, str]
```

```python
# kernel/verifier.py
def issue_challenge(session, ctx, *, scope: dict, plan_id: str, plan_revision: int,
                    factor_type: str = "totp") -> AuthChallenge
def verify_challenge(session, ctx, *, challenge_id: str, factor_response: str) -> Grant
# 120 秒有效、最多 5 次、失败锁定 15 分钟；TOTP 计数器不可重复使用；验证码不进入日志与模型上下文
```

```python
# kernel/submission.py
def prepare_operation(session, ctx, draft: OperationDraft) -> Operation
# OperationDraft: dataclass(operation_id, tool, typed_args, task_id, plan_id, plan_revision,
#                           idempotency_key, required_level, source_account_id, amount_minor, fee_minor)
def submit_operation(session, ctx, *, operation_id: str, grant_id: str | None,
                     expected_version: int | None = None) -> Operation
# 事务内：归属 → 参数哈希 → 策略再判定 → 日额度 → 授权（精确/范围子集）→ 资金 → 版本 → 登记 GrantUse/占用/Outbox
def get_operation(session, ctx, operation_id: str) -> Operation
def operation_view(session, ctx, operation_id: str) -> dict          # OperationView
```

```python
# kernel/transfers.py
def execute_transfer(session, ctx, operation: Operation) -> Operation      # 即时转账：捕获预留、记账、Outbox
def execute_scheduled(session, ctx, job: ScheduledJob) -> Operation
# kernel/provider_adapter.py
class ProviderAdapter(Protocol):
    provider_id: str
    def submit(self, *, stable_operation_ref: str, payload: dict) -> dict   # {provider_ref,status,raw}
    def query(self, *, stable_operation_ref: str) -> dict
    def cancel(self, *, stable_operation_ref: str) -> dict
    def capabilities(self) -> dict     # {"stable_key": bool, "query": bool, "cancel": bool}
```

## 3 编排与策略（D 提供）

```python
# orchestration/planner.py
def build_plan(session, ctx, *, task: Task, goal: GoalSpec, tool_catalog: ToolCatalog,
               snapshot: dict | None = None) -> Plan          # 含 steps、scope、scope_hash、required_level
def solve(session, ctx, *, goal, snapshot, catalog) -> tuple[Plan, dict]   # 返回计划与求解轨迹
def check_dag(steps: list[dict]) -> tuple[bool, list[str]]         # 循环/缺依赖/超节点
# 限额：24 节点 / 128 候选 / 2 秒；超限 → PLANNING_LIMIT（不得声称数学无解）
# 排序：用户明确优先级 → 目标完成数 → 总费用 → 必要确认次数 → 计划变更量（字典序）

# orchestration/executor.py
def execute_step(session, ctx, *, plan: Plan, step: PlanStep) -> dict
def run_task(session, ctx, *, task: Task, max_steps: int | None = None) -> dict   # 逐步执行、遇授权等待即停
def resume_task(session, ctx, *, task_id: str) -> dict

# orchestration/recovery.py
def repair_plan(session, ctx, *, task_id: str, event: dict) -> Plan      # 新修订，保留已完成效果
def affected_nodes(steps: list[PlanStep], changed_facts: set[str]) -> list[str]
def remaining_budget_minor(goal: GoalSpec, confirmed_costs: int, inflight_max: int) -> int

# orchestration/handoff.py
def open_case(session, ctx, *, task_id: str, reason: str) -> OperatorCase
def case_handoff(session, ctx, *, case_id: str) -> dict     # 已完成/未知/未提交/资金/授权清单
def operator_action(session, operator_ctx, *, case_id: str, action: str, payload: dict) -> dict
```

```python
# security/policy.py
def evaluate_policy(session, ctx, *, tool: str, typed_args: dict, snapshot: dict | None = None,
                    task_id: str | None = None) -> PolicyDecision
# PolicyDecision: dataclass(allow: bool, required_level: str, reason_codes: list[str], policy_version: str)
POLICY_VERSION = "policy-2026.10"
# security/scope.py
def build_scope(tool: str, typed_args: dict, *, task: Task | None, snapshot: dict) -> dict   # 与 UI 摘要同源
def describe_scope(scope: dict) -> dict          # 面向用户的展示摘要（金额、对象、费用、时间窗、不可逆性）
# security/card_controls.py
def effective_controls(controls: list[CardControl]) -> dict     # 叠加后的限制视图
def can_release(control: CardControl, actor_role: str) -> bool
```

分级（`POLICY_VERSION = policy-2026.10`）：

| 工具 | 等级 | 附加条件 |
|---|---|---|
| 查询类（余额、账单、卡状态、订单、适当性、报价、量化） | GREEN | 身份与归属检查后直接执行 |
| `subscription.list` / `subscription.cancel` | YELLOW | 官方黄色例外，查询也要确认 |
| `transfer.submit` | YELLOW/RED | 当日累计（含待执行占用）≤100000 分 YELLOW，>100000 RED |
| `aa.create`、`reminder.create/cancel`、`risk.assess`、`card.apply`、`card.set_limit`（调低/增加用户限制）、预留创建 | YELLOW | |
| `card.report_lost`/`unfreeze`、`card.replace`、提额批准、重要解锁、`wealth.purchase`、`wealth.redeem`、`merchant.book`、`mandate.revoke` | RED | 需 `verify_challenge` |
| 定时执行/撤单/补偿 | ≥YELLOW | 继承具体动作等级 |
| 运营审批、风控解冻 | OPERATOR | 独立角色，不能由 Agent 或客户直接批准 |

## 4 HTTP 契约（A 发布，提供者确认行为）

所有写操作必须带 `operation_id`（或 `idempotency_key`）与 `expected_revision`；响应一律为信封。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/auth/sessions` | `{user_id, password, channel}` → `{token, expires_at, user}` |
| POST | `/v1/auth/totp/enroll`、`/v1/auth/totp/confirm` | 强验证因子注册（仅认证用户） |
| POST | `/v1/tasks` | `{message, channel, reference_time?, expected_revision?}` → 任务 + GoalSpec + 澄清项 |
| GET | `/v1/tasks/{task_id}` | 任务视图：状态、修订、目标摘要、计划、进度、等待原因 |
| POST | `/v1/tasks/{task_id}/messages` | 追加指令，带 `expected_revision`；旧修订返回 `VERSION_CONFLICT` |
| POST | `/v1/tasks/{task_id}/plan` | 显式触发规划（可选，默认自动） |
| POST | `/v1/plans/{plan_id}/confirm` | `{scope_hash, expected_revision}` → `{grant_id}` 或 `{challenge_id}` |
| POST | `/v1/auth/challenges/{challenge_id}/verify` | `{factor_response}` → 服务端授权引用 |
| POST | `/v1/tasks/{task_id}/cancel` | 请求取消：返回实际边界与在途操作 |
| POST | `/v1/grants/{grant_id}/revoke` | 撤销授权，停止未提交动作 |
| GET | `/v1/operations/{operation_id}` | 权威操作查询（按归属隔离） |
| GET | `/v1/receipts/{receipt_id}` | 回执查询 |
| POST | `/v1/tasks/{task_id}/resume` | 恢复/继续执行 |
| GET | `/v1/tasks`、`/v1/operations`、`/v1/cards`、`/v1/subscriptions`、`/v1/wealth/products`、`/v1/bills/report`… | 场景只读接口 |
| POST | `/internal/provider-events/{provider}` | 回调：验签、去重、版本核验、入队 |
| GET | `/operator/cases`、`POST /operator/cases/{case_id}/actions` | 运营接管（独立角色，审计必记） |
| GET | `/v1/privacy/export`、`POST /v1/privacy/delete` | 导出与删除（按用户/角色授权） |
| GET | `/v1/meta/tools` | 工具目录（模型可见部分） |

## 5 工具目录（`contracts/tool-catalog.yaml` 为准）

`account.balance`、`payee.resolve`、`transfer.prepare`、`transfer.submit`、`transfer.status`、
`schedule.create`、`schedule.cancel`、`schedule.status`、`aa.create`、`aa.status`、
`bill.report`、`risk.assess`、`wealth.compare`、`wealth.purchase`、`wealth.redeem`、`wealth.status`、
`card.apply`、`card.set_limit`、`card.set_control`、`card.report_lost`、`card.replace`、`card.status`、
`subscription.list`、`subscription.cancel`、`mandate.revoke`、`mandate.status`、
`reminder.create`、`reminder.cancel`、`merchant.quote`、`merchant.book`、`merchant.cancel`、`merchant.status`、
`quant.risk_report`。

每个工具登记：读写性质、权限规则、输入输出 Schema、超时、幂等支持、前置条件、效果、补偿条件、
敏感字段、错误码。`prepare` 不产生外部支付；`submit` 只接受已登记并获授权的操作引用。

## 6 测试与夹具约定

- 全部测试跑真实 PostgreSQL（`TEST_DATABASE_URL`），不使用 SQLite 替代并发保证。
- `tests/conftest.py` 提供：`db`（会话工厂）、`session`（事务回滚的会话）、`clean_db`（截断所有表）、
  `seed`（标准种子：`user_demo_01`/`user_demo_02`、`account_salary`、同名联系人、两年流水）、
  `clock`（可注入时钟）、`client`（FastAPI TestClient）、`ctx`（AuthContext）。
- 金额与账本测试必须给出**手算期望**；并发测试使用真实多连接与线程。
- 证据格式遵循 V5 附录 B：`work_package_id, requirement_ids, test_case_id, commit, dataset_hash,
  model_version, seed, initial_snapshot, expected, actual, result, evidence_paths, reviewer`。
- 禁止为了让测试通过而删除断言或把任务强改成功；失败必须有归因记录。

## 7 交付与复核

每个工作包交付：代码、测试、契约变更、运行说明、证据。资金/授权/策略改动由 B、D 交叉复核；
统计由 C、D 交叉复核；用户展示由 A 与数据提供方核对。详见 `BANKAGENT_TEAM4_PLAN.md`。