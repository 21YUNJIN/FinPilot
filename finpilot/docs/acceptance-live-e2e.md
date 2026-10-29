# 运行态验收记录（live HTTP，非 TestClient）

记录时间：2026-10-27 19:19（Asia/Shanghai）
执行人：集成方（自动生成，未经人工签署）
环境：Windows 11 · PostgreSQL 17.6 @127.0.0.1:55432 · Python 3.12.14 · mock 模式

## 1 目的

在**真实启动的服务进程**上验证「安装 → 迁移 → 播种 → 启动 → 双客户端 → 场景」全链路，
避免只在进程内 TestClient 上"看起来通过"。本记录与 `docs/evidence_index.md` 的 G2/G4 对齐。

## 2 执行步骤与真实输出

```powershell
# 1) 确认库与迁移状态
psql -c "SELECT (SELECT count(*) FROM users), (SELECT version_num FROM alembic_version)"
#  -> 4 | 0001        （47 张业务表 + alembic_version）

# 2) 启动本地栈（API + Outbox worker + 调度 worker + 银行沙箱 58101 + 商户沙箱 58102）
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1
#  -> RESULT: UP（api 58000 / bank 58101 / merchant 58102，全部 /health 200）

# 3) 真实 HTTP 端到端（httpx 直连 127.0.0.1:58000）
python G:\比赛\_bootstrap\live_e2e.py
#  -> PASS 42 / FAIL 0
#  -> LIVE-E2E OK

# 4) 干净停机
powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1
#  -> RESULT: DOWN（5 进程停止，58000/58101/58102 端口释放）
```

## 3 覆盖的 42 项检查（分组）

| 组 | 检查 | 结果 |
|---|---|---|
| 健康与静态 | `/healthz` 200、`mode=mock` 显式标注、数据库连通、适配器注册（bank+merchant） | 4 PASS |
| 双客户端 | `/app/`、`/im/` 由 API 托管 200 且为构建产物（含 `<div id="root">`） | 4 PASS |
| 契约 | `/openapi.json` 41 条路径 | 1 PASS |
| 认证 | 正确口令登录、强验证因子已登记、错误口令 401 且不泄露用户是否存在 | 3 PASS |
| 账户口径 | `可用 = B−H−F`、应急金 800000 分、未穿透底线 | 3 PASS |
| 账单 | 报表可用、带 `as_of`/`data_through` 数据截止时间 | 2 PASS |
| 订阅 | `requires_confirmation=true`（保留官方 YELLOW 例外）、11 条 | 2 PASS |
| 卡片 | 3 张、叠加卡控 `user_lock + risk_freeze` 同时生效 | 2 PASS |
| AA | 份额 3 条、合计 10000 分、最大余数 3334/3333/3333 | 3 PASS |
| 理财 | 7 只产品、比较接口可用 | 2 PASS |
| 转账链路 | 任务 ok、定级 YELLOW、计划可行、`scope_hash` 64 位、篡改 scope_hash 409、确认后执行 succeeded、仅一笔操作、有提供方回执、余额只减 5000、日累计只增 5000 | 10 PASS |
| 越权/幂等 | 跨用户读任务 403、跨用户读操作 403、重复确认不产生第二笔、余额不变 | 4 PASS |
| 提供方 | 银行沙箱 `/bank/health`、商户沙箱 `/merchant/health` 独立可查 | 2 PASS |

## 4 本轮发现并修复的真实缺陷

### 4.1 种子装载器子表非幂等（已修）

现象：`/v1/aa` 返回的 AA 请求**份额为空**（`aa_shares` 0 行），而 `aa_requests` 有 1 行。
根因：`seed.py` 对父表做了"存在即 `continue`"的短路，导致父行已存在时**子行永远补不上**；
同理 `product_navs` / `holdings` / `card_controls` 在非空库重复播种时会撞唯一约束或重复叠加限制。

修复：子表改为按自然键幂等补齐 ——
- `aa_shares` 按 `(aa_id, participant_ref)`
- `product_navs` 按 `(product_id, as_of)`
- `holdings` 按 `(owner_user_id, account_id, product_id)`
- `card_controls` 按 `(card_id, control_type, channel, limit_minor, active)`

修复后对已播种的库重跑，输出为 `{"users": 0, "accounts": 0, "card_controls": 1, "aa_shares": 3}`，
即**只补缺失、不重复插入**；AA 份额随即变为 3334/3333/3333。

回归：`tests/unit/domains/test_data_provenance.py`、`tests/integration/test_migrations.py`、
`tests/integration/test_backup_restore.py` → **45 passed**。

### 4.2 验收脚本自身的三处绝对基线错误（已修）

初版脚本把"工资卡=900000"、"日累计=5000"、"AA 有份额"写成绝对值，在**已有历史数据**的演示库上
必然误报。已改为增量断言（前值 → 后值之差）并改用储蓄卡演示，使验收可重复执行。
这三条是脚本缺陷，**不是系统缺陷**，如实记在此处以避免误读历史结果。

## 5 未覆盖（不得据本记录推断）

- 容器路径（`deploy/compose.yaml`、`Dockerfile`）从未构建运行：本机无 Docker。
- 在线模型：`live` 模式未实现调用，`--mode live` 会拒绝运行。
- 未做并发压测（仅单元/集成层的真双线程并发用例）。
- 本记录由自动脚本生成，`review_status` 仍为 `pending_review`，未经人工签署。

## 6 复现方式

```powershell
cd G:\比赛\finpilot
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1
& ".\.venv\Scripts\python.exe" G:\比赛\_bootstrap\live_e2e.py
& ".\.venv\Scripts\python.exe" -m pytest tests -q          # 620 passed（整仓）
powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1   # 注意：同时会停掉本地 PostgreSQL
```