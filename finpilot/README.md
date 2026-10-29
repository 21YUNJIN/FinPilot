> **整合版本提示（2026-10-29）：以下为原交付历史快照，部分模式、接口和测试结论已变化。请先阅读整合目录根 README、docs/INTEGRATION_PROVENANCE.md 和最新审查报告；live 已新增受限模型候选接入，但评测运行器仍仅支持 mock；offline_replay 尚未实现。**

# 融策智能体（FinPilot）

> 2026 深圳国际金融科技大赛参赛作品 —— **银行智能体参考实现**。
> 一个"目标约束编译 → 分级授权 → 可信执行 → 异步恢复"的个人银行智能体后端，
> 外加两个客户端（APP 风格 / IM 风格）、两个独立业务提供方沙箱与一套评测框架。

**一句话定位**：模型可以提议，但**资金、授权、状态与审计只能由内核写入**。
系统把"读懂用户意图"与"能不能动钱"彻底分开：意图层输出的是受约束的执行计划（GoalSpec + Plan），
执行层对每一步做策略分级、授权校验、双重记账与追加写审计，并在外部结果未知时**绝不改写为失败**。

**标注（必须随任何演示/材料一起出现）**：

* 本项目**全部金融数据均为合成数据、虚拟资金**（`data/metadata/source_manifest.json` 声明
  `all_financial_data_is_synthetic: true`），不含真实个人金融信息；
* 渠道是**模拟渠道**（`bank_sandbox_v1` / `merchant_sandbox_v1` / 本地确定性适配器），
  **未接入任何真实银行的授权、认证或清算**；
* 运行模式在任务与响应里如实标注（`mock` / `offline_replay` / `live`，见 §6）；
* 第三方 StockQuant 代码**仅登记为待核验**，仓库未复制其代码、未核验许可证（见 §10）。

---

## 1 目录结构

```text
finpilot/
  README.md                      # 本文件：简介、复现步骤、验证记录、限制
  alembic.ini                    # Alembic 配置（ASCII-only，连接串由 migrations/env.py 解析）
  pyproject.toml                 # Python 包与 pytest 配置（services/api/src 为包根）
  .env.example                   # 环境变量模板（复制为 .env；不提交真实密钥）
  contracts/                     # 契约：Schema、OpenAPI、工具目录、模块清单、正反例
  services/
    api/src/finpilot/            # 后端应用（唯一可信执行实现）
      config.py db.py models.py seed.py app.py
      gateway/                   # HTTP 入口：会话、任务、确认、结果、运营、隐私、回调
      intent/ kernel/            # 目标编译；身份、账本、预留、授权、提交、调度、对账、熔断
      workers/                   # Outbox、调度器（函数级；常驻入口在 scripts/）
      orchestration/ security/   # 规划与执行协调；策略、范围、卡控、信任边界、沙箱、审计
      domains/ adapters/         # 卡、账单、理财、订阅、AA；提供方适配器
    provider-sandbox/            # 独立提供方：bank.py(58101) / merchant.py(58102)，各自独立 sqlite
  migrations/                    # Alembic 迁移链（env.py、script.py.mako、versions/0001_initial.py）
  deploy/                        # 部署与运维：compose/Dockerfile、备份恢复、自检脚本、沙箱策略
  scripts/                       # 一键启动/停止/播种/测试 + 两个 worker 常驻入口 + 分离式启动器
  tests/{unit,contracts,integration,security,e2e}
  eval/                          # 评测运行器、判定器、基线、形式化检查
  data/{synthetic,metadata}/      # 合成数据与来源清单（含许可证状态）
  apps/{web,im,shared-ui}/        # 两个客户端 + 共享组件（pnpm workspace）
  docs/                          # SRS、接口冻结、技术文档、安全自评、证据索引、追溯矩阵
```

---

## 2 架构（文字版）

```text
[客户端]  apps/web (APP 风格, :5173)      apps/im (IM 风格, :5174)
                    │  HTTPS/JSON（统一信封）
                    ▼
┌───────────────────────────────────────────────────────────────────────────────┐
│ services/api  —  FastAPI 应用（唯一可信执行实现）        /healthz 健康检查      │
│                                                                               │
│  gateway/   会话与身份（服务端会话，请求体不得覆盖 user_id）                    │
│            任务、确认、挑战验证、结果、运营接管、隐私导出/删除、提供方回调       │
│  intent/    GoalSpec 编译与澄清（意图 → 受约束目标，不直接动钱）                │
│  orchestration/ DAG 规划（24 节点/128 候选/2s 上限）→ 执行协调 → 增量恢复/接管   │
│  security/  策略分级(GREEN/YELLOW/RED/OPERATOR) → 范围校验 → 卡控 → 信任边界    │
│             → 受限分析沙箱 → **追加写审计链（哈希链 + HMAC 签名摘要）**         │
│  kernel/    身份 → 账本（双重记账，分录和必须为 0）→ 预留 → 授权(Grant/GrantUse) │
│             → 日额度桶 → 提交事务 → 操作状态机 → 调度 → 对账 → 熔断            │
│  domains/   卡、账单、理财、订阅与代扣、AA、提醒：只通过 kernel 写资金与状态      │
│  adapters/  提供方适配器（本地确定性 / HTTP 沙箱），能力不足时**自动关闭金融写入**│
└───────────────┬──────────────────────────────────────────┬────────────────────┘
                │ SQLAlchemy 2.x / psycopg3                │ Outbox（提交后才投递）
                ▼                                          ▼
     PostgreSQL 17（47 张表，Alembic 迁移链）      bank-sandbox :58101  （独立进程 + 独立 sqlite）
     · 账本/授权/操作/审计/任务/计划/领域表          merchant-sandbox :58102（独立进程 + 独立 sqlite）
     · alembic_version 版本锚点                     ↑ 受理/结算/撤单/故障注入；回调走 HTTP
                ▲                                   │
                │ SKIP LOCKED 领取                   │ POST /internal/provider-events/{provider}
     worker-outbox（常驻循环）  ─────────────────────┘   （HMAC 验签 → event_id 去重 → event_version 定序）
     worker-scheduler（到点租约 + 窗口重检）
```

关键机制（决定了本项目的"可信"边界）：

| 机制 | 口径 |
|---|---|
| 资金写入 | 只有 `kernel/ledger.post_transaction` 能改余额；每笔事务分录之和必须为 0 |
| 可用余额 | `可用 = 已入账 − 预留占用 − 其他冻结`；`可支配 = max(0, 可用 − 应急金底线)` |
| 授权 | 精确授权绑定参数哈希与计划修订；范围授权只在已批准子集内继承并共享额度 |
| 分级 | 单日转账累计 ≤ 100000 分为 YELLOW，> 100000 分为 RED（并发分拆不能绕过） |
| 异步 | 写事务内登记 Outbox，**提交后**才投递；超时/未知保留 `unknown` 与资金占用，绝不写失败 |
| 审计 | 只追加；`payload_hash` + `previous_hash` + `event_hash` 链式；`signed_digest` 供外部保存 |
| 失败关闭 | 审计不可写 → 高风险提交暂停（`AUDIT_UNAVAILABLE`）；沙箱不可用 → 不执行代码 |

---

## 3 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| Python | **>= 3.11**（本机实测 3.12.14） | 后端、迁移、评测、运维脚本 |
| PostgreSQL | **17**（本机实测 17.6） | 唯一目标数据库；**不使用 SQLite 替代**（并发与锁语义） |
| pg_dump / pg_restore | 与服务端同版本（实测 17.6） | 备份恢复演练需要 |
| Node.js + pnpm | Node >= 20，pnpm 11 | 仅两个客户端需要（`pnpm-workspace.yaml`） |
| 可选：Docker / compose | 任意较新版本 | 容器部署路径（`deploy/compose.yaml`），**本机未实测** |

Python 依赖在 `pyproject.toml`：`fastapi`、`uvicorn[standard]`、`sqlalchemy>=2.0`、`alembic>=1.13`、
`psycopg[binary]>=3.1`、`pydantic>=2.7`、`pydantic-settings`、`python-multipart`、`httpx`、
`PyYAML`、`cryptography`；开发额外需要 `pytest`、`jsonschema`。

> **本机实测环境（用于本文件所有"已验证"结论）**：
> Python 3.12.14 / alembic 1.20.1 / SQLAlchemy 2.1.6 / psycopg 3.3.6 / FastAPI 0.162.0 /
> uvicorn 0.54.0 / pytest 9.1.1 / httpx 0.28.1 / PostgreSQL 17.6（便携版，`127.0.0.1:55432`）。
> **本机没有** Docker、Node.js、pnpm：容器路径与客户端构建命令属"未在本机验证"。

---

## 4 完整复现步骤（Windows PowerShell，仓库根目录）

> 全部命令都在仓库根目录执行。`alembic.ini` 与所有 `.ps1` 保持 **ASCII-only**：
> PowerShell 5.1 用 ANSI 代码页（本机 cp936）解码无 BOM 的 UTF-8 脚本，Alembic 也用
> locale 编码读取 `alembic.ini`——本机真实踩到过 `UnicodeDecodeError: 'gbk' codec ...`，
> 因此中文说明一律放在 `.md` 里。路径全部通过参数或 `$PSScriptRoot` 推导，不写死中文路径。

### 4.1 安装

```powershell
Set-Location <仓库根目录>

# 1) 创建虚拟环境（已存在可跳过）
python -m venv .venv

# 2) 以 editable 方式安装后端包（含开发依赖）
& ".\.venv\Scripts\python.exe" -m pip install --upgrade pip
& ".\.venv\Scripts\python.exe" -m pip install -e ".[dev]"

# 3) 确认关键依赖（本机实测输出）
& ".\.venv\Scripts\python.exe" -c "import alembic,sqlalchemy,psycopg,fastapi,uvicorn,pytest,httpx,sys; print(sys.version.split()[0], alembic.__version__, sqlalchemy.__version__, psycopg.__version__, fastapi.__version__, uvicorn.__version__, pytest.__version__, httpx.__version__)"
# 3.12.14 1.20.1 2.1.6 3.3.6 0.162.0 0.54.0 9.1.1 0.28.1
```

### 4.2 配置 `.env`

```powershell
Copy-Item .env.example .env
# 按本机实际情况修改 DATABASE_URL / TEST_DATABASE_URL / BANK_SANDBOX_STATE 等
```

三个密钥（`TOTP_ENCRYPTION_KEY`、`AUDIT_SIGNING_KEY`、`PROVIDER_CALLBACK_SECRET`）默认值是
**竞赛演示用的占位值**，只允许用于本地；`.env` 已被 `.gitignore` 忽略。

### 4.3 准备数据库

```powershell
$env:PGPASSWORD="finpilot_dev_pw"
# 开发库（若已存在会报 already exists，可忽略）
& "G:\finpilot_tools\pgsql\bin\psql.exe" -h 127.0.0.1 -p 55432 -U postgres -d postgres -w `
    -c "CREATE DATABASE finpilot_dev_deploy OWNER finpilot;"
# 测试库（集成/安全测试使用；可另建专用库避免并行开发互相清表）
& "G:\finpilot_tools\pgsql\bin\psql.exe" -h 127.0.0.1 -p 55432 -U postgres -d postgres -w `
    -c "CREATE DATABASE finpilot_test OWNER finpilot;"
```

### 4.4 迁移（DATA-04）

```powershell
$url = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"

# 建表（47 张业务表 + alembic_version）
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$url upgrade head

# 版本锚点
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$url current      # -> 0001 (head)

# 关键断言：迁移链与 models.py 无差异
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$url check        # -> No new upgrade operations detected.

# 回滚（清空业务表，仅保留 alembic_version）与再升级
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$url downgrade base
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$url upgrade head
```

### 4.5 播种（可重建的合成种子）

```powershell
& ".\.venv\Scripts\python.exe" -m finpilot.seed --json data/synthetic/seed_app.json `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
# 本机实测：users 3 / accounts 4 / payees 6 / billing_transactions 1257 / products 7 /
#           product_navs 1420 / holdings 4 / cards 3 / subscriptions 6 / merchant_offers 10 / aa_requests 1

# 或使用内置最小演示种子（无需外部文件）
& ".\.venv\Scripts\python.exe" -m finpilot.seed --demo
```

> **已知限制（真实存在，勿掩盖）**：种子装载器对 `users/accounts/payees/...` 是幂等的
> （存在即跳过），但对 `holdings`、`product_navs`、`card_controls` 是**无条件插入**，
> 因此在**非空库**上重复播种可能触发 `uq_holding` 冲突。重建方式：先
> `alembic downgrade base` + `upgrade head` 再播种，或 `scripts\seed.ps1 -Reset`
> （`-Reset` 走 `drop_all/create_all`，仅限开发）。

### 4.6 启动（一键或分步）

```powershell
# 一键：PostgreSQL(未运行时启动) → alembic upgrade head → 播种(仅空库) → api + 两个 worker + 两个沙箱
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 -PgBin "G:\finpilot_tools\pgsql\bin"

# 查看状态 / 停止
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 -Status
powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1 -KeepDatabase   # 保留数据库
```

分步（等价，便于排查）：

```powershell
# API（58000）
& ".\.venv\Scripts\python.exe" -m uvicorn finpilot.app:app --host 127.0.0.1 --port 58000
# Outbox 工作者 / 调度器工作者
& ".\.venv\Scripts\python.exe" scripts\worker_outbox.py --interval 5
& ".\.venv\Scripts\python.exe" scripts\worker_scheduler.py --interval 5
# 两个独立提供方沙箱（各自独立 sqlite 状态库）
& ".\.venv\Scripts\python.exe" -m uvicorn bank:app     --app-dir services/provider-sandbox --port 58101
& ".\.venv\Scripts\python.exe" -m uvicorn merchant:app --app-dir services/provider-sandbox --port 58102
```

健康检查（本机实测，均为 HTTP 200）：

```powershell
Invoke-WebRequest http://127.0.0.1:58000/healthz        -UseBasicParsing   # status=ok, mode=mock
Invoke-WebRequest http://127.0.0.1:58101/bank/health    -UseBasicParsing
Invoke-WebRequest http://127.0.0.1:58102/merchant/health -UseBasicParsing
```

### 4.7 两个客户端的启动方式

方式一（本机已验证，**推荐用于演示**）：直接由 API 托管已构建的静态产物。

```powershell
# apps/web/dist 与 apps/im/dist 存在时，API 启动后自动挂载：
#   APP 客户端  http://127.0.0.1:58000/app/
#   IM  客户端  http://127.0.0.1:58000/im/
# 本机实测：/app/ 200（495 字节 index.html）、/im/ 200（494 字节）、/docs 200
```

方式二（开发模式，**未在本机验证**：本机无 Node.js/pnpm）：

```powershell
pnpm install
pnpm dev:web     # Vite 开发服务器 http://127.0.0.1:5173（apps/web，vite.config.ts server.port=5173）
pnpm dev:im      # Vite 开发服务器 http://127.0.0.1:5174（apps/im）
pnpm build       # 产出 apps/*/dist（方式一即用这些产物）
pnpm test        # 各客户端 vitest
```

API 的 CORS 允许来源为 `http://127.0.0.1:5173` 与 `http://127.0.0.1:5174`（含 localhost 变体）。

### 4.8 测试

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
$env:FINPILOT_PG_BIN="G:\finpilot_tools\pgsql\bin"

# 迁移链 + 备份恢复演练（本工作包交付，本机实测 14 passed）
& ".\.venv\Scripts\python.exe" -m pytest tests/integration/test_migrations.py tests/integration/test_backup_restore.py -q

# 分层 runner（与 CI 三层一致）
powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -Layer quick
powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -Layer integration
powershell -ExecutionPolicy Bypass -File scripts\test_all.ps1 -Layer security
```

> **并行开发注意（本机真实遇到）**：`tests/conftest.py` 的会话级夹具会对 `TEST_DATABASE_URL`
> 指向的库执行 `drop_all`/`create_all`，并逐用例 `TRUNCATE` 所有表。两个进程同时跑测试
> 会造成 `relation "users" does not exist` 这类互相清表错误。**并行开发请各用独立库**
> （例如 `finpilot_dev_deploy` / `finpilot_dev_checks`），CI 里则由 workflow 自带 service 容器隔离。

### 4.9 评测（工作包 D 负责，本工作包未执行）

```powershell
$env:TEST_DATABASE_URL="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test"
& ".\.venv\Scripts\python.exe" -m alembic -x db_url=$env:TEST_DATABASE_URL upgrade head
& ".\.venv\Scripts\python.exe" -m eval.runner --list-configs
& ".\.venv\Scripts\python.exe" -m eval.runner --config FULL --split dev --limit 12 --repeats 1 `
    --mode mock --out eval-results/FULL_dev.jsonl
```

`--mode mock` 的结论只反映确定性策略/账本路径与本地提供方模拟器，**不代表在线大模型能力**。
本工作包只做脚本接线（`scripts/test_all.ps1 -Layer eval`），**未运行完整评测**。

### 4.10 运维：备份、恢复与干净环境自检

见 [`deploy/README.md`](deploy/README.md)。最常用的三条：

```powershell
# 干净环境自检（venv/依赖/连通/迁移最新/种子可重建/healthz 200），退出码=失败项数
powershell -ExecutionPolicy Bypass -File deploy\verify_install.ps1 `
    -DatabaseUrl "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy" `
    -PgBin "G:\finpilot_tools\pgsql\bin"

# 备份（含审计签名摘要、内容摘要、备份内密钥扫描）+ JSON 报告
& ".\.venv\Scripts\python.exe" deploy\backup_restore.py backup `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy" `
    --output-dir backups --pg-bin "G:\finpilot_tools\pgsql\bin"

# 恢复并校验（审计摘要一致 / 账本守恒 / 逐表内容摘要一致）
& ".\.venv\Scripts\python.exe" deploy\backup_restore.py restore `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_restore" `
    --input backups\finpilot-<时间戳>.dump
```

---

## 5 一键自检：`deploy/verify_install.ps1`

对干净环境逐项检查并打印 PASS/FAIL（退出码 = 失败项数，0 表示全通过）：

| 检查项 | 含义 |
|---|---|
| `python.venv` | 虚拟环境解释器存在且 Python >= 3.11 |
| `python.dependencies` | 关键依赖可导入且版本不低于 `pyproject.toml` 要求 |
| `config.ascii_only` | `alembic.ini` 与全部 `*.ps1` 无非 ASCII 字节（locale 编码陷阱护栏） |
| `postgres.pg_isready` | PostgreSQL 在 `127.0.0.1:55432` 可连接 |
| `database.connect` | 目标库连通并返回服务端版本 |
| `migration.at_head` | `alembic current` 处于 head |
| `migration.no_diff_vs_models` | `alembic check` 与 `finpilot.models` 无差异 |
| `seed.rebuildable` | 在**一次性数据库**里真实执行 建库 → 迁移 → 播种，并校验 47 张表与装载计数 |
| `api.healthz` | 临时启动 uvicorn，`/healthz` 返回 200 且 `status=ok`、`database.ok=true`（随后自动停止） |

---

## 6 运行模式与标注

| 模式 | 行为 | 标注位置 |
|---|---|---|
| `mock` | 无模型；确定性规则编译器 + 本地确定性提供方适配器（`bank_sandbox_local`）。本地开发与确定性测试的默认模式 | 任务 `mode` 字段、响应 `provenance.adapter`、`/healthz.labeling` |
| `offline_replay` | 回放冻结轨迹，明确标注"离线回放"，**不冒充实时** | 同上 |
| `live` | 调用在线模型（需 `MODEL_BASE_URL` / `MODEL_API_KEY` / `MODEL_NAME`） | 同上 + 审计记录 `model_version` |

`/healthz` 固定返回 `labeling: "模拟渠道与虚拟资金；mock/offline_replay/live 模式在任务与响应中明确标注"`，
任何界面与材料不得把 `mock`/`offline_replay` 的结果描述为在线模型能力或真实银行业务。

---

## 7 验证记录（本机真实执行，2026-10-27）

环境：Windows + PowerShell 5.1；Python 3.12.14；PostgreSQL 17.6（`127.0.0.1:55432`）；
pg_dump/pg_restore 17.6。数据库：`finpilot_dev_deploy`（先 DROP 再 CREATE，确保是空库）。

```text
A) 空库 -> 建库
   DROP DATABASE / CREATE DATABASE                      (psql, exit 0)

B) alembic upgrade head
   Running upgrade  -> 0001, initial -- FinPilot 完整初始 Schema（47 张表）
   exit 0
   alembic current -> 0001 (head)
   业务表数量（不含 alembic_version）= 47

C) alembic check（关键断言：与 models.py 无差异）
   No new upgrade operations detected.                  exit 0

D) downgrade base / 再 upgrade head
   downgrade base exit 0 -> 仅剩 alembic_version
   再 upgrade head exit 0 -> 业务表 47
   关键唯一约束存在：uq_operation_owner, uq_grant_use_operation,
                    uq_daily_limit_bucket, uq_ledger_tx_operation_kind

E) pytest tests/integration/test_migrations.py tests/integration/test_backup_restore.py -q
   ..............                                       [100%]
   14 passed in 22.53s                                  (exit 0)

F) finpilot.seed --json data/synthetic/seed_app.json
   {"counts": {"users":3,"accounts":4,"protection_policies":1,"payees":6,
    "billing_transactions":1257,"products":7,"product_navs":1420,"holdings":4,
    "cards":3,"card_controls":6,"card_applications":4,"subscriptions":6,
    "mandates":5,"merchant_offers":10,"aa_requests":1}, "dataset_version":"seed-2026.10"}
   exit 0

G) deploy\verify_install.ps1（干净环境自检）
   [PASS] python.venv :: version=3.12.14
   [PASS] python.dependencies :: fastapi=0.162.0; uvicorn=0.54.0; sqlalchemy=2.1.6;
          alembic=1.20.1; psycopg=3.3.6; pydantic=2.14.1; pydantic_settings=2.16.1;
          httpx=0.28.1; yaml=6.0.3; pytest=9.1.1
   [PASS] config.ascii_only :: checked=6
   [PASS] postgres.pg_isready :: exit=0
   [PASS] database.connect :: PostgreSQL 17.6 on x86_64-windows
   [PASS] migration.at_head :: 0001 (head)
   [PASS] migration.no_diff_vs_models :: No new upgrade operations detected.
   [PASS] seed.rebuildable :: tables=47 users=3 billing=1257
   [PASS] api.healthz :: HTTP 200 status=ok mode=mock
   checks: 9 passed, 0 failed, 9 total   RESULT: PASS   exit 0

H) scripts\dev_up.ps1（一键启动）与 dev_down.ps1
   dev_up  exit 0，9.2s 内 UP：
     [1/8] PostgreSQL already accepting connections on 127.0.0.1:55432
     [2/8] alembic upgrade head -> schema at head
     [3/8] finpilot.seed -> skipped: database already has 4 user(s)
     [4/8] api pid=... /healthz 200 on http://127.0.0.1:58000/healthz
     [5/8] worker-outbox pid=...   [6/8] worker-scheduler pid=...
     [7/8] bank-sandbox pid=...    [8/8] merchant-sandbox pid=...
     RESULT: UP
   dev_down exit 0：五个进程 stop requested，PostgreSQL 按 -KeepDatabase 保留
   dev_up -Status：逐个 PID 显示 running/stopped，并探测三个健康端点

I) 静态客户端托管（API 自带 /app、/im）
   /healthz 200 (448 B)   /app/ 200 (495 B)   /im/ 200 (494 B)   /docs 200 (1027 B)

J) 备份恢复演练（tests/integration/test_backup_restore.py，5 passed）
   建库 -> 迁移 -> 播种 -> 真实转账 30000 分（经 Outbox 投递）-> 追加 2 条审计事件
   -> 备份（pg_dump -Fc + 审计摘要 + 内容摘要 + 密钥扫描）-> 再跑一笔转账改动数据
   -> 恢复到独立新库（pg_restore）-> 校验：
     checks = {audit_digest: true, ledger_conservation: true, content_digest: true}
     content_digest.differing_tables = {}，overall 摘要与备份时刻完全一致
     secret_scan: scanned=true, bytes_scanned=452549, hits=[]（备份内无当前密钥明文）
     反向对照：删除恢复库最后一条审计事件后 verify_signed_digest -> ok=false,
               reason_codes 含 CHAIN_CHANGED_SINCE_DIGEST
     前后余额：account_salary 900000 -> 870000，account_clearing 0 -> 30000

K) scripts\test_all.ps1 -Layer quick（用专用库 finpilot_dev_checks 避免并行清表）
   hygiene: 6 ASCII-only files OK
   unit: 2 failed, 236 passed（失败项在 tests/unit/planner/test_oracle.py，属评测工作包）
   contracts: 146 passed
   结论：runner 工作正常；上述 2 项失败不属于本工作包范围，未修改、未掩盖
```

**未在本机验证**（不得对外声称通过）：容器部署（本机无 Docker）、客户端 `pnpm dev/build`
（本机无 Node.js/pnpm）、完整评测 `eval.runner` 全量运行（属评测工作包）、
CI workflow 在 GitHub Actions 上的实际执行（本机无网络/无 runner）。

---

## 8 已知限制与未完成项

### 8.1 安全与合规边界（如实披露）

* **单库哈希链无法抵抗"同时掌握签名密钥与存储"的管理员**：能改 payload、重算哈希并持有
  `AUDIT_SIGNING_KEY` 的人可以重写整条链并重新签名。要收紧必须引入本模块之外的控制
  （审计库独立凭证与只追加权限、密钥放 KMS/HSM、`signed_digest` 定期外送 WORM/第三方时间戳）。
  本项目只提供**检测**能力与可外送摘要。
* **尾部截断对链内自检不可见**，这正是必须把 `signed_digest` 保存到备份介质之外的原因。
* **受限代码执行沙箱在 Windows 上与 Linux 容器不等价**：Windows 侧只有子进程 + AST 白名单 +
  runtime audit hook + 墙钟/输出限额，**CPU/内存/进程数硬限额与内核级文件系统隔离未生效**，
  差距逐条记录在 `deploy/sandbox-policy.yaml` 的 `windows_dev_equivalence.gaps_not_covered`。
* **未接入真实银行**：所有渠道均为模拟渠道，虚拟资金；未做真实清算、对账或 KYC。

### 8.2 工程限制

* 容器路径（`deploy/compose.yaml`、`deploy/Dockerfile`）**未在本机实测**，其"上机验证清单"
  见 `deploy/README.md` §2.4；容器内 `security` 选项是**目标配置**。
* 备份工具只做逻辑备份（`pg_dump -Fc`），**不做 WAL 归档/PITR**；摘要与归档默认落在同一目录，
  真实部署必须把摘要外送（本机未验证外送链路）。
* 种子装载器对 `holdings`/`product_navs`/`card_controls` 非幂等（见 §4.5）。
* 并行开发共用同一个测试库会互相清表（见 §4.8）；CI 用独立 service 容器规避。
* `tests/unit/planner/test_oracle.py` 当前有 2 项失败（评测工作包在改），与本工作包无关。
* 本机无 git 可执行文件，迁移文件头以 **`models.py` 的 sha256** 作为版本锚点
  （`7d4a7901a77e1d2c3fd866dc37ecfb241af7fcb6846318d761247e43161f8786`），而非提交号。

### 8.3 未完成 / 待办

* 客户端构建与端到端浏览器验收（属客户端工作包）。
* 完整评测运行与统计（属评测工作包）；本工作包只接线了 `scripts/test_all.ps1 -Layer eval`。
* 容器镜像构建与上机验证；`deploy/seccomp/sandbox.json`（沙箱策略中引用的 seccomp profile）
  由沙箱工作包交付，本工作包只引用。
* 许可证文件：仓库当前**没有 LICENSE 文件**，最终许可证待项目负责人确定（见 §10）。

---

## 9 数据库迁移链（DATA-04）

* 位置：`alembic.ini`（配置，ASCII-only）+ `migrations/env.py`（连接与 autogenerate 口径）+
  `migrations/versions/0001_initial.py`（完整初始迁移，47 张表 / 86 索引 / 45 外键 / 18 唯一约束）。
* 连接解析顺序：`-x db_url=...` → `DATABASE_URL`（含 `.env`）→ `finpilot.config` 默认值。
  配置文件**不保存口令**，迁移链与应用永远使用同一份连接口径。
* `target_metadata = finpilot.models.Base.metadata`，`compare_type=True`，
  `compare_server_default=False`（避免 `now()`/`CURRENT_TIMESTAMP` 的假差异；由
  "表集合相等 + alembic check 无差异"共同保证）。
* 验收：`upgrade head` → `downgrade base` → `upgrade head` 幂等；
  `alembic check` 输出 `No new upgrade operations detected.`；
  `tests/integration/test_migrations.py`（9 项，含"删表后 check 必须报错"的反向对照）。
* 改动 `models.py` 后：先 `alembic revision --autogenerate -m "<msg>"` 生成新修订，
  再跑 `alembic check` 与上述测试；禁止手写漏表或直接改历史修订。

---

## 10 许可证与第三方说明

**本仓库当前不包含 `LICENSE` 文件**（未在本工作包写入范围内），最终许可证由项目负责人确定；
在此之前不得声称本项目已按某许可证开源。

第三方与数据来源（以 `data/metadata/source_manifest.json` 为准）：

| 名称 | 类型 | 许可证 / 状态 | 说明 |
|---|---|---|---|
| `finpilot_*`（seed_app、tasks_600、attack_catalog_200、fault_catalog、账单流水、净值序列、商户报价） | 合成数据 | **CC0-1.0**（团队自建） | 全部合成、可随仓库分发；无真实个人信息 |
| `bankagent_srs_v5_competition`、`finpilot_interfaces_frozen` | 文档 | 团队自撰内部文档 | 规格与接口口径来源 |
| **`stockquant_third_party_code`** | 第三方代码 | **待核验**：`stockquant_license_verified: false`，`license: "待核验（尚未取得许可证文件，禁止声称已核验）"` | **仅登记为只读适配目标**；本仓库未复制其代码、未核验许可证；接入前必须完成许可证、依赖与版本核验 |
| `llm_provider_api` | 外部服务 | 供应商服务条款（待核验） | 仅在 `live` 模式使用；数据出境与留存条款待核验，不得声称已合规 |
| `python_runtime` | 运行时 | PSF-2.0 | 随 Python 发行版分发 |
| `postgresql_server` | 数据库 | PostgreSQL License | 待归档许可证副本 |
| Python 依赖（FastAPI/uvicorn/SQLAlchemy/Alembic/psycopg/pydantic 等） | 库 | 各自 MIT/BSD/Apache-2.0 等 | 由 `pyproject.toml` 声明；未在本仓库归档许可证副本 |
| 前端依赖（React、Vite、vitest 等） | 库 | 见各包 `package.json` | 由 pnpm workspace 管理 |

---

## 11 相关文档

| 文档 | 内容 |
|---|---|
| [`docs/INTERFACES.md`](docs/INTERFACES.md) | 冻结接口与协作约定（唯一接口来源） |
| [`docs/technical_document.md`](docs/technical_document.md) | 技术设计说明 |
| [`docs/security_self_assessment.md`](docs/security_self_assessment.md) | 安全自评（含边界披露） |
| [`docs/limitations.md`](docs/limitations.md) | 已知限制汇总 |
| [`docs/evidence_index.md`](docs/evidence_index.md) | 证据索引（V5 附录 B 格式） |
| [`docs/requirement-trace.md`](docs/requirement-trace.md) | 需求追溯矩阵 |
| [`deploy/README.md`](deploy/README.md) | 部署路径 A/B、备份恢复、运维脚本、未实测声明 |
| [`services/provider-sandbox/README.md`](services/provider-sandbox/README.md) | 两个提供方沙箱的协议、故障注入与回调口径 |
| [`data/README.md`](data/README.md) | 合成数据与来源清单说明 |