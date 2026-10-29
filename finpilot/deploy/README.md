# FinPilot 部署与运维手册（deploy/）

> 对应 V5 需求 **NFR-02（可部署与可恢复）、DEL-02（交付与复现）、SYS-04（运行环境）、
> GOV-01（CI 分层）**。
>
> **本文件的诚实声明先写在最前面：**
> 本机（Windows 竞赛开发机）**没有容器运行时**（已实测 `docker` 不在 PATH），因此
> `deploy/compose.yaml`、`deploy/Dockerfile`、`deploy/nginx-clients.conf`、
> `deploy/initdb/*.sql` 属于**目标配置，未在本机实测**。本手册把两种部署路径分开写：
> **路径 B（Windows 本机）已逐条实测**并附带真实输出摘要；**路径 A（Linux 容器）标注为
> 未实测**，并列出上机验证清单。任何"已验证"的说法都只适用于路径 B。

---

## 1 生产/演示拓扑（文字版）

```text
                    ┌──────────────────────────┐
   APP 客户端 :5173 │  web (静态托管, 可选)     │
   IM  客户端 :5174 │  im  (静态托管, 可选)     │
                    └─────────────┬────────────┘
                                  │  /v1/*  /operator/*  /internal/*  /healthz
                    ┌─────────────▼────────────┐        ┌───────────────────────┐
                    │ api  (uvicorn :58000)    │◀──────▶│ postgres:17 :55432    │
                    │  - 会话/任务/授权/账本    │  同库   │  47 张表 (Alembic)    │
                    │  - /healthz 健康检查      │        │  alembic_version      │
                    └───────┬──────────┬───────┘        └───────────────────────┘
                            │          │                          ▲
        Outbox 投递(HTTP)   │          │  轮询/认领 (SKIP LOCKED)  │
                            │          │                          │
        ┌───────────────────▼──┐  ┌────▼──────────────────┐  ┌────┴─────────────────┐
        │ bank-sandbox :58101  │  │ worker-outbox         │  │ worker-scheduler      │
        │ merchant-sandbox     │  │ (常驻循环)            │  │ (run_job_forever)     │
        │        :58102        │  └───────────────────────┘  └───────────────────────┘
        │ 各自独立 sqlite 状态库│
        └──────────┬───────────┘
                   │  回调 POST /internal/provider-events/{provider}
                   └──────────────▶ api（HMAC 验签 + event_id 去重 + event_version 定序）
```

要点：

* **api 与两个 worker 共用一个镜像/一个 venv**，但**是三个独立进程**：api 只处理请求，
  外部副作用只由 worker 从 Outbox 领取后发生（提交后才发生外部效果）。
* **两个提供方沙箱是独立进程 + 独立 sqlite 状态库**，不 import `finpilot`；它们的状态
  永远以自身库为准，主库只保存自己的授权、账本与回执。
* **迁移在 api 启动之前完成**（容器路径用一次性 `migrate` 服务 + `service_completed_successfully`；
  本机路径用 `deploy/verify_install.ps1` 或 `scripts/dev_up.ps1` 先跑 `alembic upgrade head`）。

### 端口与环境变量一览

| 组件 | 端口 | 关键环境变量 |
|---|---|---|
| postgres | 55432（宿主）→ 5432（容器） | `POSTGRES_USER/PASSWORD/DB`，应用侧 `DATABASE_URL` |
| api | 58000 | `DATABASE_URL`、`APP_MODE`、`TOTP_ENCRYPTION_KEY`、`AUDIT_SIGNING_KEY`、`PROVIDER_CALLBACK_SECRET` |
| worker-outbox | 无对外端口 | 同上（只需要数据库与提供方地址） |
| worker-scheduler | 无对外端口 | 同上 |
| bank-sandbox | 58101 | `BANK_SANDBOX_STATE`、`PROVIDER_CALLBACK_SECRET`、`PROVIDER_CALLBACK_BASE_URL` |
| merchant-sandbox | 58102 | `MERCHANT_SANDBOX_STATE`、`PROVIDER_CALLBACK_SECRET`、`PROVIDER_CALLBACK_BASE_URL` |
| web（可选） | 5173 | 无（静态文件 + 反向代理到 api） |
| im（可选） | 5174 | 无 |
| 回调基址 | api 的 `/internal/provider-events/{provider}` | `PROVIDER_CALLBACK_BASE_URL`（容器内必须是 `http://api:58000`，不能是 127.0.0.1） |

安全相关的三个密钥（`TOTP_ENCRYPTION_KEY`、`AUDIT_SIGNING_KEY`、`PROVIDER_CALLBACK_SECRET`）
**不写入仓库、不写入镜像、不写入备份**：前两者由环境注入，`AUDIT_SIGNING_KEY` 还必须保存在
备份介质之外（否则"整链重写/尾部删除"不可检测，见 §4）。

---

## 2 路径 A：Linux 容器（**未在本机实测**）

> 本机无 `docker`（`Get-Command docker` 返回空）。以下命令**没有在本机执行过**，
> 请勿在报告或答辩中引用为"已验证"。上机验证清单见 §2.4。

### 2.1 文件

| 文件 | 作用 |
|---|---|
| `deploy/compose.yaml` | 10 个服务：`postgres`、`migrate`(一次性)、`api`、`worker-outbox`、`worker-scheduler`、`bank-sandbox`、`merchant-sandbox`、`seed`(tools profile)、`web`/`im`(clients profile) |
| `deploy/Dockerfile` | api 与两个 worker 共用镜像（`python:3.12-slim` + editable 安装 `finpilot`，非 root uid 10001） |
| `deploy/Dockerfile.dockerignore` | 构建上下文排除 `.venv/`、`node_modules/`、`tests/`、`eval/` 等 |
| `deploy/initdb/01_test_database.sql` | 首次初始化时额外创建 `<user>_test` 库（`TEST_DATABASE_URL` 用） |
| `deploy/nginx-clients.conf` | 客户端静态托管的 SPA fallback + `/v1`、`/operator`、`/internal`、`/healthz` 反向代理 |
| `deploy/compose.env.example` | 环境变量模板（复制为 `deploy/compose.env`，该文件已被 `.gitignore` 忽略） |

### 2.2 启动步骤（目标流程）

```bash
# 1) 准备环境变量（把 dev 占位密钥换成部署自己的密钥）
cp deploy/compose.env.example deploy/compose.env

# 2) 起数据库（其余服务依赖它的健康检查）
docker compose -f deploy/compose.yaml --env-file deploy/compose.env up -d postgres
docker compose -f deploy/compose.yaml --env-file deploy/compose.env ps

# 3) 迁移（一次性容器，退出码 0 才继续）——DATA-04
docker compose -f deploy/compose.yaml --env-file deploy/compose.env run --rm migrate
docker compose -f deploy/compose.yaml --env-file deploy/compose.env run --rm migrate \
    python -m alembic current          # 期望输出 0001 (head)
docker compose -f deploy/compose.yaml --env-file deploy/compose.env run --rm migrate \
    python -m alembic check            # 期望 "No new upgrade operations detected."

# 4) 播种（tools profile；生产可跳过）
docker compose -f deploy/compose.yaml --env-file deploy/compose.env --profile tools run --rm seed

# 5) 起全部常驻服务
docker compose -f deploy/compose.yaml --env-file deploy/compose.env up -d

# 6) 自检
curl -fsS http://127.0.0.1:58000/healthz          # {"status":"ok", ...}
curl -fsS http://127.0.0.1:58101/bank/health
curl -fsS http://127.0.0.1:58102/merchant/health

# 7) 可选：客户端静态托管
docker compose -f deploy/compose.yaml --env-file deploy/compose.env --profile clients up -d web im
# 打开 http://127.0.0.1:5173（APP 客户端）与 http://127.0.0.1:5174（IM 客户端）

# 8) 停止 / 回滚
docker compose -f deploy/compose.yaml --env-file deploy/compose.env down          # 保留卷
docker compose -f deploy/compose.yaml --env-file deploy/compose.env run --rm migrate \
    python -m alembic downgrade base                                              # 回滚到空库（谨慎）
```

### 2.3 健康检查、依赖顺序与安全选项

* **依赖顺序**：`postgres`(healthy) → `migrate`(completed) → `api` / `worker-*`；
  `api` 还等 `bank-sandbox`、`merchant-sandbox` 的 `/bank/health`、`/merchant/health` 通过。
  `web`/`im` 等 `api` healthy。
* **健康检查**：postgres 用 `pg_isready`；api 用 `python -c urllib.request` 打 `/healthz`
  （镜像里没有 curl，避免额外依赖）；两个沙箱打各自的 `/health`（这两个端点在故障注入
  `unavailable` 下依然可用，便于区分"进程死了"与"业务不可用"）。
* **卷**：`pgdata`（数据库）、`bank_state` / `merchant_state`（两个沙箱的 sqlite），
  都是具名卷；客户端 `dist` 与 nginx 配置以 `:ro` 只读挂载。
* **安全选项**：FinPilot 进程容器统一 `read_only: true` + `tmpfs /tmp` +
  `security_opt: [no-new-privileges:true]` + `cap_drop: [ALL]` + 非 root（uid 10001）。
  宿主机端口全部绑定 `127.0.0.1`，只在需要联调时开放。
* **沙箱策略对照**：容器内代码执行沙箱的目标策略在 `deploy/sandbox-policy.yaml`
  （别人已交付，本工作包只读引用）；其 `verification` 段的 V1–V11 必须逐条留证据，
  不能只凭"用了 Docker"声称隔离生效。

### 2.4 上机验证清单（**尚未执行**）

在 Linux + Docker 主机上按顺序执行并留证据；任一条失败都不得宣称容器路径可用：

1. `docker compose config -q` 通过（语法/插值校验，不需要启动）。
2. `docker compose build` 成功，镜像内 `python -c "import finpilot, alembic; print(alembic.__version__)"` 正常。
3. `migrate` 一次性服务退出码 0，且 `psql -c "\dt"` 看到 47 张业务表 + `alembic_version`。
4. `api` 的 `/healthz` 返回 `{"status":"ok"}` 且 `database.ok = true`。
5. 两个 worker 起来后，跑一笔转账能看到 `outbox.delivery_state` 从 `pending → delivered`。
6. 两个沙箱的 `/bank/health`、`/merchant/health` 返回 200；回调走 `http://api:58000` 成功。
7. 非 root 运行：`docker compose exec api id -u` 输出 `10001`。
8. 只读根：`docker compose exec api touch /etc/x` 必须失败。
9. 客户端 profile：`web`/`im` 页面可打开且 `/v1/*` 反代 200。
10. 备份链路：`backup_restore.py` 用容器内的 `pg_dump` 走通 §4 的演练。

---

## 3 路径 B：Windows 本机（**已实测**）

本机环境（实测值）：

| 组件 | 实测值 |
|---|---|
| Python | 3.12.14（`G:\比赛\finpilot\.venv`，editable 安装了 `finpilot`） |
| alembic / sqlalchemy / psycopg | 1.20.1 / 2.1.6 / 3.3.6 |
| fastapi / uvicorn / pytest / httpx | 0.162.0 / 0.54.0 / 9.1.1 / 0.28.1 |
| PostgreSQL | 17.6 便携版，`G:\finpilot_tools\pgsql\bin`，数据目录 `G:\finpilot_tools\pgdata`，监听 `127.0.0.1:55432` |
| pg_dump / pg_restore | 17.6（`G:\finpilot_tools\pgsql\bin`，备份演练真实跑通） |
| 容器运行时 / Node.js / pnpm | **不存在**（`docker`、`node`、`pnpm` 均不在 PATH） |

### 3.1 一键启动 / 停止

```powershell
Set-Location "G:\比赛\finpilot"

# 启动：PostgreSQL(若未运行) + alembic upgrade head + api + worker-outbox + worker-scheduler
#       + bank-sandbox(58101) + merchant-sandbox(58102)
# 全部用 Start-Process 重定向日志，避免占用管道（pg_ctl 尤其必须如此）
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1

# 状态与日志
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1 -Status
Get-Content .run\logs\api.out.log -Tail 40

# 停止（含 PostgreSQL，可用 -KeepDatabase 保留数据库进程）
powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1
```

### 3.2 分步命令（与 README「完整可复现步骤」一致）

```powershell
# 1) 迁移到 head
& ".\.venv\Scripts\python.exe" -m alembic -x db_url="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot" upgrade head

# 2) 与 models.py 的差异检查（关键断言：必须输出 No new upgrade operations detected.）
& ".\.venv\Scripts\python.exe" -m alembic -x db_url="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot" check

# 3) 回滚（空库）与再升级
& ".\.venv\Scripts\python.exe" -m alembic -x db_url="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot" downgrade base
& ".\.venv\Scripts\python.exe" -m alembic -x db_url="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot" upgrade head

# 4) 播种（可重建种子，1257 条账单 / 1420 条净值等）
& ".\.venv\Scripts\python.exe" -m finpilot.seed --json data/synthetic/seed_app.json `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot"

# 5) 干净环境自检（venv / 依赖 / 连通 / 迁移是否最新 / 种子可重建 / /healthz 200）
powershell -ExecutionPolicy Bypass -File deploy\verify_install.ps1 `
    -DatabaseUrl "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
```

> **编码陷阱（本机真实踩到）**：Windows PowerShell 5.1 读取**无 BOM 的 UTF-8** `.ps1`
> 会把中文路径解码成乱码，且 **Alembic 用 locale 编码（本机 cp936/GBK）读取
> `alembic.ini`**，因此 `deploy/*.ps1`、`scripts/*.ps1`、`alembic.ini` 一律
> **ASCII-only**；中文说明只放在 `.md` 文件里。`migrations/env.py` 通过
> `Path(__file__).resolve().parents[1] / "services/api/src"` 自己拼路径，不依赖 ini 里的中文。

---

## 4 备份与恢复（NFR-02、T22、T55）

工具：`deploy/backup_restore.py`（`pg_dump` / `pg_restore` 封装 + 审计摘要 + 账本守恒 + JSON 报告）。

### 4.1 设计口径

1. **备份前**调用 `finpilot.security.audit.signed_digest`，把"链头 + 事件数 + 整链内容摘要
   + HMAC 签名"写入 `<dump>.audit-digest.json`（**备份文件之外**）。
   链内自检看不到"尾部整段删除"，只有与外部保存的摘要比对才能发现——所以摘要不能只留在库里。
2. **备份内不得含密钥**：默认 `--exclude-table-data=public.sessions`（会话令牌哈希是凭证类
   数据，且恢复旧登录态本身不安全）；并把归档用 `pg_restore -f -` 展开成 SQL 文本，
   **逐字扫描**当前配置的 `AUDIT_SIGNING_KEY` / `PROVIDER_CALLBACK_SECRET` /
   `TOTP_ENCRYPTION_KEY` / `MODEL_API_KEY` 是否出现在备份里；一旦出现即判定备份不合格
   （fail-closed，退出码 2），而不是只写一句"应该没有密钥"。
   数据库本身不保存这三把密钥（它们在环境变量里），库里的 `users.totp_secret_enc` 是密文。
3. **恢复后三项校验**：
   * `verify_signed_digest`：审计摘要是否仍与链一致（检测整链重写/尾部删除）；
   * `kernel.ledger.conservation_check` + 一条与进程状态无关的 SQL 校验
     （每笔事务分录之和为 0）；
   * 逐表内容摘要（`row_to_json(t)::text` 按主键排序后 md5，UTC 会话）与备份时刻比对。
4. **退出码**：`0` 通过；`2` 校验未通过；`3` 找不到 pg 工具；`4` 外部命令失败。

### 4.2 命令

```powershell
$env:FINPILOT_PG_BIN = "G:\finpilot_tools\pgsql\bin"

# 工具自检（pg_dump / pg_restore / psql / pg_isready）
& .\.venv\Scripts\python.exe deploy\backup_restore.py check-tools --pg-bin $env:FINPILOT_PG_BIN

# 备份（默认输出到仓库根 backups/，可用 --output-dir 改）
& .\.venv\Scripts\python.exe deploy\backup_restore.py backup `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot" `
    --output-dir backups --label nightly

# 恢复到一个全新库（先创建目标库）并校验
& "G:\finpilot_tools\pgsql\bin\psql.exe" -h 127.0.0.1 -p 55432 -U postgres -d postgres -w `
    -c "CREATE DATABASE finpilot_restore OWNER finpilot;"
& .\.venv\Scripts\python.exe deploy\backup_restore.py restore `
    --database-url "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_restore" `
    --input backups\finpilot-<时间戳>.dump
```

`restore` 会自动寻找同名的 `.audit-digest.json` 与 `.manifest.json`；报告是一份 JSON，
可用 `--report <path>` 指定落盘位置。

### 4.3 演练结果（本机真实执行）

`tests/integration/test_backup_restore.py` 把整条链路做成可重复的自动化演练：
**建库 → Alembic 迁移 → 播种（`data/synthetic/seed_app.json`）→ 跑一笔真实转账（30000 分，
经 Outbox 投递）→ 追加两条审计事件 → 备份 → 再跑一笔转账改动数据 → 恢复到另一个数据库 →
校验审计摘要 / 账本守恒 / 逐表内容摘要**。执行结果：

```text
$env:TEST_DATABASE_URL="postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_dev_deploy"
$env:FINPILOT_PG_BIN="G:\finpilot_tools\pgsql\bin"
& ".\.venv\Scripts\python.exe" -m pytest tests/integration/test_backup_restore.py -q
.....                                                                    [100%]
5 passed in 17.85s
```

关键断言（全部为真实数据库状态，不是 mock）：

* 转账后 `account_salary = 870000`、`account_clearing = 30000`（手算期望 900000 − 30000）；
* 备份报告 `secret_scan.scanned = true`、`hits = []`（展开 452549 字节 SQL 文本逐字扫描）；
* 恢复后 `checks = {audit_digest: true, ledger_conservation: true, content_digest: true}`，
  `content_digest.differing_tables = {}`，`overall` 摘要与备份时刻**完全一致**；
* 反向对照：删掉恢复库里最后一条审计事件后，`verify_signed_digest` 返回
  `ok=false` 且 `reason_codes` 含 `CHAIN_CHANGED_SINCE_DIGEST`（证明校验不是空转）；
* 备份产物里不存在会话令牌哈希（`token_hash` 不出现在归档 SQL 中），
  但同表结构与其它表数据（`COPY public.users`）确实存在。

### 4.4 未覆盖 / 未验证

* **不做 PITR**：只做逻辑备份（`pg_dump -Fc`），没有 WAL 归档与时间点恢复；
* **没有异地/对象存储上传**：摘要与归档都落在本机目录，真实部署需要把
  `.audit-digest.json` 送出去（WORM/外部对账系统），否则该摘要与库同生共死；
  这一点本机**未验证**，只在本手册中作为要求写明；
* **没有演练"恢复到同一库"**：演练恢复到独立新库（更强）；原地覆盖恢复
  （`--clean --if-exists`）虽已实现但未单独演练；
* 备份并发写下的极端一致性窗口未压测（`pg_dump` 的一致性由 PostgreSQL 快照语义保证）。

---

## 5 运维脚本清单

| 脚本 | 用途 | 实测状态 |
|---|---|---|
| `deploy/verify_install.ps1` | 干净环境自检：venv、依赖版本、数据库连通、迁移是否最新、种子可重建、`/healthz` 200；逐项 PASS/FAIL + 退出码 | 已实测（见 README 证据段） |
| `deploy/backup_restore.py` | 备份/恢复 + 摘要与守恒校验 + JSON 报告 | 已实测（§4.3） |
| `scripts/dev_up.ps1` | 一键启动 PostgreSQL + 迁移 + api + 两 worker + 两沙箱（Start-Process 重定向日志） | 已实测 |
| `scripts/dev_down.ps1` | 按 PID 文件停止上述进程 | 已实测 |
| `scripts/seed.ps1` | 播种封装（可 `-Reset` 重建表结构） | 已实测 |
| `scripts/test_all.ps1` | 按 CI 分层跑快速检查 / 集成 / 安全 / 评测 | 已实测（见 README） |

所有 `.ps1` **只用 ASCII 字符**；日志与 PID 写在 `.run/`（已 gitignore），不污染仓库。

---

## 6 与 `deploy/sandbox-policy.yaml` 的关系

`deploy/sandbox-policy.yaml` 由另一工作包交付，本工作包**只读引用**：
`compose.yaml` 的安全选项（`read_only`、`cap_drop: ALL`、`no-new-privileges`、`tmpfs`）
与它的 `container.*` 段保持一致；该文件 `verification` 段的 V1–V11 属于**运行时验证义务**，
本手册 §2.4 只列出与本部署拓扑相关的条目，未在本机执行。