> **整合版本提示（2026-10-29）：以下为原交付历史快照，部分模式、接口和测试结论已变化。请先阅读整合目录根 README、docs/INTEGRATION_PROVENANCE.md 和最新审查报告；live 已新增受限模型候选接入，offline_replay 仍未实现；具体验证边界见根目录 README 和 docs/QWEN_LIVE_INTEGRATION_2026-10-29.md。**

# 融策智能体（FinPilot）安全自评报告

2026 深圳国际金融科技大赛 · 国内人工智能赛道 · 按官方安全自评模板（OFF-4）组织

---

## 0. 自评范围与口径

### 0.1 自评对象

| 项 | 值 |
|---|---|
| 系统 | 融策智能体（FinPilot）个人银行智能体参考实现 |
| 仓库 | `G:\比赛\finpilot`（快照时点 2026-10-27 17:28—17:55 Asia/Shanghai） |
| 受评组件 | `services/api/src/finpilot`（可信执行后端）、`services/provider-sandbox`（外部提供方沙箱）、`apps/{web,im,shared-ui}`（两个客户端） |
| 运行模式 | `mock`（确定性规则编译器 + 确定性本地提供方），**无大模型调用** |
| 资金 | 全部虚拟资金，全部合成数据，无真实银行授权 |

### 0.2 自评纪律

- 数字只来自本机真实执行；"未实测/未验证"逐处标注，不写成"已通过"。
- 不声称验证方式已获赛事认可（外部问题 **Q02** 未关闭）。
- 不声称"可抵抗掌握全部密钥与存储的管理员"——本实现的审计只能**检测**，不能阻止这一类攻击者（§6.4）。

### 0.3 快照期发现的环境性事实（影响结论强度）

| 事实 | 影响 |
|---|---|
| `services/api/src/finpilot` 内**无任何大模型客户端代码**（`model_base_url` 等仅为 `config.py` 字段） | 本报告的注入防护结论是**机制级**验证，不是"对真实模型有效"的验证 |
| 容器文件已有（`deploy/Dockerfile` + `deploy/compose.yaml`），但本机**无 docker CLI、无 WSL 发行版**，镜像**从未构建** | 容器隔离、seccomp、只读根等**全部未验证**（配置只是声明） |
| Windows 无 `resource` 模块、无 `libseccomp` | CPU/内存/进程数/写入硬限额**未生效** |
| 编制期间存在并行编辑（`docs/INTERFACES.md` 17:31:21 被改动、17:38:00 索引与清单同批重生成） | 数据来源测试一度退化为 30 passed / 1 failed，重生成后恢复 31 passed；`dataset_hash` 由 `319721e9…` 变为 `200ac215…`（载荷文件哈希未变） |

---

## 1. 资产清单与信任边界

### 1.1 受保护资产

| 资产 | 载体 | 泄露/篡改后果 |
|---|---|---|
| 会话凭证 | `SessionToken.token_hash`（sha256） | 冒用身份 |
| 口令摘要 | `User.password_hash`（scrypt） | 离线破解 |
| TOTP 秘钥 | `User.totp_secret_enc`（Fernet 加密） | 强验证失效 |
| 资金与预留 | `Account` / `LedgerTransaction` / `LedgerEntry` / `Reservation` | 余额错误、重复扣款 |
| 授权 | `Grant` / `GrantUse` | 越权写入 |
| 审计链 | `AuditEvent` + `signed_digest` | 事后掩盖 |
| 隐私数据 | `TaskMessage` / `Task.goal_spec` | 个人信息泄露 |

### 1.2 信任边界（与 §2.2 技术文档一致）

**不在信任边界内的输入**：用户自由文本、商户描述、账单备注、检索文档、工具返回自由文本、外部回调 HTTP 载荷、模型输出（未来 live 模式）。

**边界内的唯一权威**：服务端会话身份、`security/policy.py` 的工具目录与等级、`Grant` 行与参数哈希、`ledger.py` 分录、`AuditEvent` 链。

**结构性证据**：写工具与人类端点不在 `AGENT_EXPOSED_TOOLS` 内；`kernel/submission.py:43-45` 直接拒绝 `user_id`/`owner_user_id`/`posted_balance_minor`/`grant_id`/`status`/`effect_confirmed`/`version` 等危险入参字段。

---

## 2. 权限等级表

### 2.1 等级定义

| 等级 | 定义 | 用户体验 | 服务端强制 |
|---|---|---|---|
| **GREEN** | 只读、无资金效果 | 无需二次确认 | 直接执行并写审计 |
| **YELLOW** | 有资金或状态效果，可逆或低额 | 需**明确确认**（展示金额、对象、费用、到账） | 需 `Grant`（`required_level=YELLOW` 创建即 `active`） |
| **RED** | 高额、不可逆或高风险 | 需**真实强验证**（TOTP） | `Grant` 初始 `pending`，仅挑战通过后置 `active` |
| **OPERATOR** | 仅独立运营角色可执行 | 客户身份请求直接拒绝 | `ctx.is_operator` 检查 |

等级不可被降低：`effective_level()` 取最高（`security/policy.py:256-258`）；客户端与模型都不能改等级（`policy.py:1-4`）。

### 2.2 工具目录与等级（37 项，实测计数）

**GREEN（14 项，全部对模型只读暴露）**：`account.balance`、`payee.resolve`、`bill.report`、`transfer.prepare`、`transfer.status`、`wealth.compare`、`wealth.status`、`card.status`、`mandate.status`、`merchant.quote`、`merchant.status`、`quant.risk_report`、`aa.status`、`schedule.status`

**YELLOW（13 项）**：`subscription.list`（官方 YELLOW 例外）、`subscription.cancel`、`transfer.submit`（≤100000 分时）、`schedule.create`、`schedule.cancel`、`aa.create`、`reminder.create`、`reminder.cancel`、`risk.assess`、`card.apply`、`card.set_limit`（调低）、`card.set_control`（增加限制）、`merchant.cancel`

**RED（6 项）**：`card.report_lost`、`card.replace`、`wealth.purchase`、`wealth.redeem`、`merchant.book`、`mandate.revoke`

**OPERATOR（4 项）**：`operator.card_decision`、`operator.release_risk_freeze`、`operator.risk_unfreeze`、`operator.case_action`

> 计数口径：`security/policy.py` 的 `TOOL_POLICY` 共 37 项；对模型暴露 16 项（14 GREEN + `subscription.list` + `subscription.cancel`）；写工具（不对模型暴露、非运营）17 项；运营 4 项。合计 16 + 17 + 4 = 37。

### 2.3 金额相关分级规则

| 规则 | 实现 |
|---|---|
| 转账按**当日累计（含待执行占用）**分档 | `kernel/limits.py` 的 `grade_transfer()`；阈值 `TRANSFER_DAILY_YELLOW_LIMIT_MINOR = 100000` 分 |
| 手续费不计入转账本金累计 | `policy.py:1-5` 注释 + `policy.py:126-136` |
| 定时任务给等级下限（不提前消耗未来日额度） | `policy.py:138-147` |
| 卡片调低额度 YELLOW、申请提额 RED | `policy.py:103-107` |
| 解除重要卡控 RED；风控冻结只能运营解除 | `policy.py:110-118` |
| 被拒绝的提额申请不能靠重新发问变成批准 | `policy.py:196-213` |
| 卡控叠加不互相清除 | `policy.py:273-286` |

---

## 3. 强验证实现

### 3.1 口令与会话

| 项 | 实现 | 位置 |
|---|---|---|
| 口令哈希 | `scrypt`，N=2^14、r=8、p=1、16 字节随机盐、32 字节摘要 | `kernel/identity.py:61-72` |
| 口令校验 | `hmac.compare_digest` 常数时间比较 | `identity.py:75-90` |
| 最小长度 | <8 位拒绝 | `identity.py:68-69` |
| 登录失败文案 | 统一"账号或密码不正确"，**不泄露用户是否存在** | `identity.py:133-137` |
| 会话令牌 | 随机 token，库内只存 sha256；有过期与 `revoked_at` | `identity.py:127-180` |
| 会话有效期 | `SESSION_TTL_MINUTES = 120` | `config.py:47` |

### 3.2 TOTP（独立持有因子）

| 项 | 实现 | 位置 |
|---|---|---|
| 算法 | RFC 6238：HMAC-SHA1、30 秒步长、6 位、默认 ±1 窗口 | `identity.py:213-243` |
| 秘钥生成 | 20 字节随机，base32 | `identity.py:209-210` |
| 秘钥存储 | Fernet 加密（密钥由 `TOTP_ENCRYPTION_KEY` 派生） | `identity.py:193-206` |
| 秘钥外发 | 仅在注册流程返回一次，写入独立验证器；不进入对话与日志 | `identity.py:259-265` |
| 注册确认 | `confirm_totp()` 校验后写 `totp_confirmed_at` | `identity.py:268-279` |
| 未注册即拒 | 无已确认因子时 `POLICY_DENIED` | `identity.py:287-291` |

### 3.3 挑战（有效期 / 次数 / 锁定 / 防重放 / 一次性）

| 控制 | 值 / 逻辑 | 位置 |
|---|---|---|
| 有效期 | `CHALLENGE_TTL_SECONDS = 120`；过期置 `expired` 并 `CHALLENGE_EXPIRED` | `config.py:54`、`verifier.py:105-108` |
| 次数限制 | `CHALLENGE_MAX_ATTEMPTS = 5`；达到即 `locked` | `config.py:55`、`verifier.py:114-124` |
| 锁定时长 | `CHALLENGE_LOCK_SECONDS = 900`；锁定期内 `CHALLENGE_LOCKED`(429) | `config.py:56`、`verifier.py:99-104` |
| 一次性消费 | 已验证且已消费的挑战再次提交 → `GRANT_INVALID` | `verifier.py:97-98` |
| 验证码防重放 | 记录命中计数器 `last_counter`；校验时传入 `used_counters`（回看 10 分钟） | `verifier.py:74-83, 132-136` |
| 并行挑战收敛 | 同一用户未完成旧挑战立即置 `expired`，避免并行放大尝试次数 | `verifier.py:45-53` |
| 范围绑定 | 挑战绑定 `operation_scope_hash`；签发的 `Grant` 继承该范围 | `verifier.py:58, 138-169` |
| 行级串行化 | 校验时 `SELECT ... FOR UPDATE` 锁挑战行 | `verifier.py:91-93` |
| 安全锁 | 可疑行为触发 `lock_out()`，待处理挑战全部置 `locked` | `verifier.py:185-195` |

### 3.4 授权（精确 vs 范围）

| 类型 | 约束 | 关键行为 |
|---|---|---|
| 精确 `exact` | 绑定用户、计划修订、操作、账户、对象、金额、费用上限、时间窗、**参数哈希**；`max_uses = 1` | 修订不符 → `exact_grant_revision_mismatch`；参数不符 → `parameter_hash_mismatch`；已用 → `grant_already_used`（`grants.py:210-221`） |
| 范围 `scoped` | 额外列出允许工具/商户/产品、每笔与总额上限、最多次数、截止时间、可恢复动作 | 候选必须是批准范围的**子集**（`scope_is_subset`，`grants.py:161-188`）；共享额度与次数，**不能靠新 `operation_id` 重置**（`grants.py:326-345`） |
| 消费 | 提交事务内 `FOR UPDATE` 锁授权行，登记 `GrantUse` | 同一授权不能被两个操作重复使用（`grants.py:288-348`） |
| 撤销 | 只停止尚未提交的步骤；已提交与未知结果继续核验 | `grants.py:368-397` |

### 3.5 金额与幂等（防重复扣款）

| 控制 | 实现 |
|---|---|
| 幂等键 + 请求哈希 | `idempotency_request_hash(tool, typed_args)`；同键异参 → `IDEMPOTENCY_CONFLICT`(409)（`submission.py:90-102`、`errors.py:50`） |
| 授权二次绑定 | `GrantUse(grant_id, operation_id)` 唯一；同授权异参 → `IDEMPOTENCY_CONFLICT`（`grants.py:302-308`） |
| 额度占用幂等 | 同操作异参或已释放后重占 → `IDEMPOTENCY_CONFLICT`（`kernel/limits.py:107-112`） |
| 账本唯一性 | `(operation_id, kind)` 唯一：同一业务效果只允许入账一次（`ledger.py:44-46`） |
| 并发 | 账户行锁 + 预留行锁（`db.lock_accounts`），测试用真实 PostgreSQL 而非 SQLite |
| 未知结果 | 操作状态 `unknown` 不计为失败、不释放资金；先查询再决定（`state_machine.py:39-49`、`recovery.py:1-8`） |

---

## 4. 注入与越权防护

### 4.1 防护机制

| 层 | 机制 | 位置 |
|---|---|---|
| 入口扫描 | `scan_injection(text)` 多类原因码：指令覆盖、角色冒充、外泄、政策变更、工具指令、base64/hex/转义编码载荷、跨语言混淆、Unicode 同形字、leet | `security/trust_boundary.py` |
| 编码解码后再扫 | 先解码 base64/hex/百分号/转义，再匹配指令模式 | 同上 |
| 工具入参白名单 | `assert_safe_tool_args()` 按已登记工具校验参数形状 | 同上 |
| 工具输出隔离 | `wrap_tool_result()` 把自由文本标为**非权威**，疑似指令自动隔离；结构化权威字段独立 | 同上 |
| 最小发送 | `sanitize_for_model(payload, allowed_fields)` 白名单 + 敏感项换代理 ID | 同上 |
| 归属校验 | 策略层 `_ownership_ok()` + 持久层查询过滤（双重） | `policy.py:222-241`、`ledger.py:157-175` |
| 不虚构事实 | 判定器与结果视图只认权威状态；"未知"不写成"成功" | `eval/oracle.py`、`state_machine.py` |

### 4.2 真实测试结果

【实跑】`tests/security` 全量：**162 passed**，用时 49.87s，独立库 `finpilot_test_docs`。

```powershell
$env:TEST_DATABASE_URL='postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test_docs'
& .\.venv\Scripts\python.exe -m pytest tests/security -q
```

```text
........................................................................ [ 44%]
........................................................................ [ 88%]
..................                                                       [100%]
162 passed in 49.87s
```

三个文件的覆盖（测试函数 63 个，参数化展开为 162 个用例）：

| 文件 | 用例数贡献 | 覆盖的攻击类 |
|---|---|---|
| `test_trust_boundary.py` | 32 条攻击语料 + 7 条正常语料 + 脱敏/隔离/白名单用例 | 指令覆盖（中/英/伪造 system）、角色冒充、数据外泄与跨用户访问、政策/限额/状态篡改、工具调用指令与凭据索取、base64/hex/转义编码、跨语言与拼音、Unicode 同形字/全角/零宽/双向控制、leet 变形 |
| `test_sandbox.py` | 26 个级联用例 | 合法代码可跑 vs 恶意样本拦截、AST 违规、无界循环、输出爆炸、结果超限、不可用即关闭、环境最小化、audit hook 自检、策略文件与默认一致 |
| `test_audit.py` | 18 个用例 | 篡改检测（payload/删除/重排/互换/签名）、尾部删除、秘密与思维链拒写、失败关闭、并发线性 |

**注入类攻击的处置结论**：文本被识别并给出原因码，不会转化为工具调用权限。但请注意两点诚实限制：
1. 扫描器是**基于模式**的，未做对抗性优化；它降低而非消除注入风险。
2. 因本机无模型，"注入文本能否诱导真实模型产出越权工具调用"**未实测**。防护的结构性保证来自"写工具不对模型暴露 + 服务端授权"，而不是扫描器的准确率。

### 4.3 越权测试

`tests/integration/test_authorization.py` 在 42 passed 组合运行中（与 `test_ledger.py`、`test_dag.py` 合并执行，29.91s）。覆盖：跨用户对象访问、授权参数篡改（改金额后参数哈希不符）、授权重放、范围授权子集继承与共享额度、撤销后停止未提交步骤、并发争抢同一授权/同一额度。

### 4.4 未覆盖的攻击面（如实披露）

| 攻击面 | 状态 |
|---|---|
| 针对真实大模型的注入（越狱、间接注入经模型放大） | **未测**（无模型） |
| 客户端 XSS/CSRF（`apps/web`、`apps/im`） | 前端有 142 条用例，但**未做专项渗透测试** |
| 依赖供应链（npm/pip 包投毒） | **未做审计**（无 SCA 工具记录） |
| 数据库直连攻击（拿到 DB 凭证后改账） | 不在本层防护内；审计链可检测，但不能阻止（§6.4） |
| 拒绝服务与速率限制 | 仅有应用层熔断/安全锁（`SEC-03`），**无网关级限流实测** |

---

## 5. 沙箱：资源限制与**未生效项**

### 5.1 设计（跨平台）

1. **父进程 AST 白名单**：拒绝危险导入、危险调用、dunder 属性访问、常量真值 `while` 循环，给出可读原因码（如 `IMPORT_FORBIDDEN:os`）。
2. **独立子进程**：`sys.executable -I -S <runner>`，一次性临时工作目录，环境变量最小化（无 `DATABASE_URL`、无模型密钥、无 `HOME`），输入走 stdin JSON，输出走 stdout 并按 `output_bytes` 截断。
3. **运行时 audit hook**（`sys.addaudithook`）：拦截网络、子进程创建、文件写入、标准库外打开、非白名单运行时导入、`os.*` 危险操作。
4. **POSIX 资源硬限额**：`RLIMIT_CPU` / `RLIMIT_AS` / `RLIMIT_NPROC` / `RLIMIT_FSIZE` / `RLIMIT_CORE`。
5. **不可用即关闭**：策略禁用、策略加载失败、子进程启动失败 → `status="unavailable"`，**绝不回退到宿主 exec/eval**，个性化报表改走无 I/O 的受限 DSL `run_dsl`。

### 5.2 本机真实生效/未生效矩阵（实测）

平台探测输出：

```text
sys.platform = win32
Windows-11-10.0.26300-SP0
resource module: False
libseccomp: None
```

| 控制项 | 设计目标 | Windows 本机 | 说明 |
|---|---|---|---|
| AST 白名单 | 生效 | ✅ **生效** | 父进程静态校验 |
| audit hook | 生效 | ⚠️ **生效但仅 Python 层** | 绕过 CPython 审计事件的原生扩展不受约束 |
| 墙钟超时 | 生效 | ✅ **生效** | 超时 kill 子进程 |
| 输出字节上限 | 生效 | ✅ **生效** | 结果按 `output_bytes` 截断 |
| 环境变量最小化 | 生效 | ✅ **生效** | 测试断言子进程环境已剥离 |
| 工作目录隔离 | 生效 | ✅ **生效** | 一次性临时目录 |
| `RLIMIT_CPU` | CPU 秒数硬限额 | ❌ **未生效** | Windows 无 `resource` 模块 |
| `RLIMIT_AS` | 内存硬限额 | ❌ **未生效** | 同上 |
| `RLIMIT_NPROC` | 进程数硬限额 | ❌ **未生效** | 同上（仅 audit hook 拦截创建） |
| `RLIMIT_FSIZE` | 写入硬限额 | ❌ **未生效** | 同上（仅 audit hook 拦截写入） |
| 内核级文件系统隔离 / 只读根 | 容器提供 | ❌ **未生效** | 容器未构建未运行（compose 有 `read_only: true` 声明） |
| seccomp / cap-drop / 非 root / 无网络 | 容器提供 | ❌ **未生效** | 容器未运行；compose 有 `cap_drop: ALL`、`no-new-privileges`、`USER 10001` 声明；无 `libseccomp` |

代码在 Windows 分支写入的自述（`sandbox.py:1306-1307`）：

> "Windows 本机：无 resource 模块，CPU／内存／进程数硬限额未生效，由墙钟超时、输出限额、AST 校验与 audit hook 兜底。"
> "audit hook 是 Python 层拦截，不是内核级 seccomp／容器隔离；宿主机文件系统未被内核级隔离。"

模块 docstring 另有明示（`sandbox.py:28-31`）：

> `deploy/sandbox-policy.yaml` 里的 Linux 容器策略（非 root、只读根、cap-drop ALL、无网络、seccomp……）在本环境**未被验证执行**，只是参考配置；本模块实现的是"子进程 + AST + audit hook + 墙钟/输出限额"这一较弱但可运行的等价物。

### 5.3 结论口径

**可以声称**：本实现在 Windows 上提供了"静态白名单 + 子进程 + Python 层 audit hook + 墙钟与输出限额"的可运行受限执行，且不可用时关闭而非降级。
**不能声称**：本项目已验证 CPU/内存/进程数/写入的**硬限额**，或已验证容器级隔离、seccomp、只读根文件系统。
**同样不能声称**：`deploy/sandbox-policy.yaml`、`deploy/Dockerfile` 或 `deploy/compose.yaml` 已被任何容器引擎执行过——本机无 docker CLI、无 WSL 发行版，镜像从未构建（`deploy/Dockerfile:6-9` 自述同一事实）。

---

## 6. 数据隐私

### 6.1 最小发送

`sanitize_for_model(payload, allowed_fields=...)`（`trust_boundary.py:980-1075`）：

- **白名单**：只保留 `allowed_fields`，其余进 `dropped_fields`；
- **敏感键**：password/otp/totp/code/secret/token/pan/cvv 等替换为**确定性代理 ID**（HMAC 派生 12 位十六进制），同一值在同一会话内稳定映射，便于模型引用但不泄露；
- **返回摘要**：`redacted_fields` / `untrusted_fields` / `proxy_map`，**不含任何原始值**；
- 被测试强制：`tests/security/test_trust_boundary.py:177-260`。

### 6.2 留存与用户权利

| 项 | 实现 |
|---|---|
| 聊天留存 | `CHAT_RETENTION_DAYS = 30` |
| 审计留存 | `AUDIT_RETENTION_DAYS = 90` |
| 留存查询 | `GET /v1/privacy/retention`，返回天数、说明与运行模式；注明"竞赛环境配置，不构成银行法定留存声明" |
| 导出 | `GET /v1/privacy/export`：只导出**本人**任务/消息/操作，经 `sanitize_for_model` 脱敏后返回；写 `privacy.export` 审计事件（记录条数与脱敏摘要，不记内容） |
| 删除 | `POST /v1/privacy/delete`，scope ∈ {`chat`, `chat_and_closed_tasks`}；删除用户与助手消息、对已关闭任务的 `goal_spec` 做占位化；**不删除已发生的金融事实与审计**；写 `privacy.delete` 审计事件 |
| 删除记录 | `GET /v1/privacy/deletion-requests` 可查历史请求与删除计数 |
| 前端约束 | `PrivacyPanel` 要求二次确认（输入"删除"），文案明确"不声称抹除金融事实" |

### 6.3 秘密不进入日志/审计/模型

- 审计 `append_event` 拒绝 password/otp/totp/code/secret/pan/cvv/token 等键与疑似 6 位验证码值（`audit.py:16-19`，测试 `test_audit.py:176-229`）。
- 拒绝模型私有思维链字段。
- TOTP 验证码不经过模型、不写日志（`identity.py:4-5`；契约测试断言验证码提交后不残留在页面文本中，见前端 `challenge-dialog.test.tsx`）。

### 6.4 数据驻留与出境的真实状态

- **本机无任何数据出境**：无模型调用、无外部 API 调用（提供方沙箱为本地进程）。
- `source_manifest.json:116-124` 将 `llm_provider_api` 登记为"供应商服务条款（待核验具体模型版本与数据使用条款）"，**未核验**。若未来接入在线模型，数据出境与留存条款必须先核验，当前**不得声称已合规**。

---

## 7. 审计可检测篡改的边界

### 7.1 能检测什么（有测试）

| 篡改方式 | 检测手段 | 测试 |
|---|---|---|
| 改 payload | `payload_hash` 重算不符 | `test_audit.py:75-84` |
| 删中间事件 | `previous_hash` 断链 | `test_audit.py:86-96` |
| 事件重排 | 序列/哈希链不连续 | `test_audit.py:98-108` |
| 载荷互换 | 哈希与内容不匹配 | `test_audit.py:110-121` |
| 换签名 | HMAC 校验失败 | `test_audit.py:123-131` |
| 尾部删除 | 外送 `signed_digest` 比对不符 | `test_audit.py:165-174` |
| 整链重写（无密钥） | 摘要签名不符 | `test_audit.py:133-163` |

### 7.2 **不能**检测/阻止什么（必须写清）

1. **同时掌握 `audit_signing_key` 与审计存储写权限的人**可以改 payload、重算 `payload_hash`/`event_hash`、并重新签名，使 `verify_chain` 与 `signed_digest` **都自洽**。此时链内自检**无法发现**。`audit.py:27-32` 原文承认这一点，并指出要收紧必须引入本模块之外的三种控制：审计库独立凭证与只追加权限、签名密钥放在审计进程不可读的 KMS/HSM、把 `signed_digest` 定期写入外部只写介质或第三方时间戳服务。
2. **尾部截断**对链内自检不可见（链本身仍自洽）；必须依赖**已外送保存的摘要**比对。
3. **本实现未提供**上述三类外部控制：没有 KMS/HSM、没有审计库独立凭证、没有自动外送。因此当前的"可检测"仅在**攻击者没有签名密钥**的前提下成立。
4. 审计表与业务表**同库同事务**：好处是"审计写成功但业务提交失败"不会发生、审计不可写时业务写入被阻断（fail-closed）；代价是数据库管理员权限同时覆盖两者。

### 7.3 审计内容边界

- **记录**：工具名、参数摘要、策略与模型版本、授权引用、验证结果、资金效果、回执与恢复决定（结构化依据与可检查决定）。
- **不记录**：口令、验证码、TOTP 秘钥、完整卡号/CVV、模型私有思维链、原始个人信息。审计"不写入秘密"由 `assert_no_secrets` 强制。

---

## 8. 残余风险与修复计划

| 编号 | 残余风险 | 当前状态 | 影响 | 计划修复 | 优先级 |
|---|---|---|---|---|---|
| R-01 | Windows 无 CPU/内存/进程/写入硬限额 | **未生效**（实测 `resource` 缺失） | 恶意/失控分析代码可能耗尽资源 | 在 Linux 容器内运行并启用 rlimit + seccomp + 只读根；或使用 Job Object 施加 Windows 侧限额 | 高 |
| R-02 | audit hook 为 Python 层，非内核隔离 | 已知限制 | 原生扩展可绕过；同用户其他进程不受约束 | 容器 + seccomp 白名单；或将沙箱迁到独立低权限账户/独立主机 | 高 |
| R-03 | 容器文件已有但**从未构建/运行**（`deploy/Dockerfile`、`deploy/compose.yaml`） | **未验证** | "可容器化"无运行时证据；只读根/cap-drop/非 root 仅为声明 | 在 Linux 主机上 `docker build` + `compose up`，跑通迁移/测试并留存 `limits_enforced` 全项 | 高 |
| R-04 | 无在线模型，"注入对真实模型有效性"未测 | **未验证** | 安全结论只到机制层 | 接入模型后跑攻击集与隐藏集，报告拦截率与漏检例 | 高 |
| R-05 | 审计密钥与审计库同机同权限 | 已知限制 | 具备双权限者可重写整链 | 引入 KMS/HSM 存签名密钥；审计库独立凭证；`signed_digest` 定时外送 WORM | 高 |
| R-06 | 隐藏集为团队内部评测，非第三方盲测 | 已知限制 | 结论存在自评偏差 | 交由第三方或至少在冻结后不参与调参；公布全部分母与失败例 | 中 |
| R-07 | StockQuant 许可证未核验 | `stockquant_license_verified=false` | 知识产权合规风险 | 取得许可证文件并归档；未核验前不分发其代码副本 | 高 |
| R-08 | 依赖供应链未审计 | **未做** | 第三方包漏洞/投毒 | 引入 SCA（如 pip-audit、pnpm audit）并固化锁文件校验 | 中 |
| R-09 | 无网关级限流与 DDoS 防护 | 仅有应用层熔断/安全锁 | 可用性风险 | 网关限流 + 账号维度速率限制 | 中 |
| R-10 | 无备份恢复演练（NFR-02） | **未执行** | 灾备不可验证 | 编排一次"备份→清库→恢复→对账"演练并留证据 | 中 |
| R-11 | 数据清单条目数与文件不符（故障数：清单文字 56 vs 文件 58） | **实测发现，未关闭** | 可追溯性受损（但测试不覆盖条数，故不导致失败） | 更新 `source_manifest.json` 中 `finpilot_fault_catalog` 的 notes 为 58 条 | 中 |
| R-12 | 用户测试与专家复核未开展 | **未开展** | 可用性结论缺失 | 招募匿名参与者 + ≥2 名业务复核者 | 中 |
| R-13 | 客户端未做专项渗透测试 | **未做** | XSS/CSRF 风险未知 | 前端安全测试（CSP、输入转义、依赖审计） | 中 |
| R-14 | `bank_http` 适配器缺失（配置了尝试导入但模块不存在） | `/healthz` 如实报 `unavailable` | 无真实银行 HTTP 对接路径 | 实现该适配器或从装配里移除并写明"仅本地/沙箱适配器" | 低 |
| R-15 | 强验证方式是否被赛事认可 | 外部问题 **Q02 未关闭** | 合规表述风险 | 取得组委会书面说明；预留指定验证器适配位 | 中 |
| R-16 | **端到端小样本评测报出 7 条禁止效果**（4 条 `effect_duplicate_payment`、3 条 `effect_card_state_changed_unrequested`）；同批 30 条 **0 完成**、全部未人工复核 | **本轮实测，未通过，根因未定位** | 若为真实缺陷，则"未知结果不重复扣款"在端到端链路上不成立；若为运行器驱动缺陷，则评测链路尚不可用 | 读 `evidence_paths` 指向的账本分录与操作记录，判定"驱动重复提交"还是"真实重复入账"；修复后以 `--repeats 5` 重跑并人工复核签署 | **最高** |

### 8.1 修复顺序建议

1. **R-16**（端到端禁止效果）——最高优先级。在定位之前，所有"不重复扣款""不越权改卡状态"的表述都必须限定在单元/集成层。先做这一步，因为它直接决定 OC20/OC21 的可信度。
2. **R-01/R-02/R-03**（沙箱与部署）——同一根因：没有 Linux 与容器。这三项决定"隔离"类声明能否成立。
3. **R-05**（审计密钥）——把"能检测"从"未持有密钥"提升到"即使持有也需要外部介质"。
4. **R-04**（在线模型）——决定"换模型仍有效"这一主张成立与否。
5. **R-06/R-12**（评测与复核）——决定对外结论的可信度。
6. **R-07**（许可证）、**R-11**（清单一致性）——合规与可追溯性，成本低、应先做。

---

## 9. 自评结论（不超过可支撑的范围）

**可以支撑的结论**：

- 服务端存在**结构性的权限边界**：写工具与人类端点不在模型可见集合内；身份只来自会话；每次写入必须命中服务端签发的授权与参数哈希。
- 资金侧有**双重记账 + 独立预留 + 幂等 + 未知结果不释放**的完整机制，并由 42 条内核用例与 26 条提供方沙箱用例覆盖。
- 注入/越权/审计/沙箱机制有 **162 条安全用例**覆盖，且沙箱在不可用时**关闭而非降级**。
- 所有金融数据为合成数据，敏感材料不入审计、不入模型上下文。

**不能支撑的结论**（本报告主动排除）：

- 不能声称"已抵御针对真实大模型的注入"——未接模型。
- 不能声称"端到端不重复扣款"——本轮 dev 30 条小样本跑出 4 条 `effect_duplicate_payment` 与 3 条 `effect_card_state_changed_unrequested`，根因未定位（R-16）。幂等与账本约束的结论**仅限单元/集成层**。
- 不能声称"CPU/内存/进程/写入硬限额已生效"——Windows 无 `resource`。
- 不能声称"容器隔离、seccomp、只读根已验证"——容器文件已存在但从未构建运行，本机无 docker、无 WSL 发行版。
- 不能声称"审计不可篡改"——只能检测，且不能抵抗同时持有签名密钥与存储权限者。
- 不能声称"隐藏集第三方盲测通过"或任何完成率数字——隐藏集未调分，dev 30 条小样本为负结果（0 完成、7 条禁止效果）。
- 不能声称"验证方式已获赛事认可"——Q02 未关闭。
- 不能声称"已提交"或任何名次/评审结论。