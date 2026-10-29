# 新环境复现记录（2026-10-29）

本文记录在一台**此前没有 FinPilot 环境**的 Windows 11 机器上，按交接书第 4 节重建环境并取得基线测试结果的真实过程。所有数据库、密钥和运行状态都在本机隔离目录内，未使用任何真实银行、证券或客户数据。

## 1. 环境

| 项目 | 版本 | 位置 |
|---|---|---|
| Windows | Windows 11 专业版 build 26200，64 位 | — |
| Python | 3.12.10（64 位，python.org 用户级安装） | `D:\FinPilot\2026-10-29\Python312` |
| PostgreSQL | 17.9（EnterpriseDB 便携二进制，非服务安装） | `D:\FinPilot\2026-10-29\pgsql\pgsql\bin` |
| Node.js | v24.16.0（本机已有） | `D:\nodejs` |
| pnpm | 11.28.5（`npm i -g`） | npm 全局目录 |
| Git | 2.54.0 | `D:\git` |

Python 用 `scripts/setup.ps1` 建立两个虚拟环境：

- `.venv-finpilot`：离线 wheelhouse 49 个包 + `requests`；54 个已安装包。
- `.venv-quant`：`flask`、`requests`、`numpy`（离线包中不含，需网络）。

## 2. 隔离边界

- PostgreSQL 只监听 `127.0.0.1:5433`，与系统默认 5432 分开；便携集群数据目录 `D:\FinPilot\2026-10-29\pgdata`，可整目录删除，不影响本机其他数据库。
- 三个**互相独立**的数据库：`finpilot_demo`（迁移 + 种子，供演示）、`finpilot_test`（pytest 会 drop/create 全部表）、`finpilot_eval`（评测运行器建库用）。`TEST_DATABASE_URL` 未指向任何需要保留的数据。
- 演示栈端口 58000（API + 两客户端静态页）、58101（银行沙箱）、58102（商户沙箱）、8770（量化服务），均为回环地址。
- 模型密钥：本机不配置，`APP_MODE=mock`。`finpilot/.env` 由脚本生成，含随机数据库口令与三个本地签名/加密密钥，已在 `.gitignore` 中，不进入仓库与交付包。

## 3. 复现命令

```powershell
# 1) 便携 PostgreSQL（已初始化，重启机器后执行）
& D:\FinPilot\2026-10-29\pgsql\pgsql\bin\pg_ctl.exe -D D:\FinPilot\2026-10-29\pgdata -l D:\FinPilot\2026-10-29\logs\pg.log start

# 2) 迁移 + 种子（只作用于 finpilot_demo）
. D:\FinPilot\2026-10-29\ops\env.ps1
cd D:\FinPilot\2026-10-29\FinPilot_Competition_20261028\finpilot
& ..\.venv-finpilot\Scripts\python.exe -m alembic upgrade head
& ..\.venv-finpilot\Scripts\python.exe -m finpilot.seed --demo

# 3) 启动演示栈（Ctrl+C 停止）
cd ..
& .\.venv-finpilot\Scripts\python.exe .\scripts\run_demo.py

# 4) 全量后端回归（独立测试库）
. D:\FinPilot\2026-10-29\ops\env.ps1
$env:QUANT_BACKEND='local_reference'; $env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:FINPILOT_PG_BIN='D:\FinPilot\2026-10-29\pgsql\pgsql\bin'
cd .\finpilot
& ..\.venv-finpilot\Scripts\python.exe -m pytest tests -q
```

种子结果：5 用户、5 账户、65 条账单流水、5 个理财产品 / 210 条净值、2 张卡片、2 个订阅与 2 个代扣授权、AA 请求与分摊，`dataset_version=seed-demo-2026.10`。

## 4. 基线结果

### 4.1 后端全量回归

| 运行 | 命令要点 | 结果 | 日志 |
|---|---|---|---|
| 第 1 次 | 直接继承演示 `.env`（`QUANT_BACKEND=remote`） | **665 passed / 3 failed / 4 skipped** | `evidence/finpilot_pytest_20261029_161500_firstrun_ambient_env.log` |
| 第 2 次 | 显式 `QUANT_BACKEND=local_reference`、`FINPILOT_PG_BIN` 指向便携 PostgreSQL | **672 passed / 0 failed / 0 skipped**，375.74s | `evidence/finpilot_pytest_20261029_162940_envrepro.log` |
| 第 3 次 | 修复 Worker 适配器注册 + 新增 2 个回归用例后 | **674 passed / 0 failed / 0 skipped**，257.98s | `evidence/finpilot_pytest_20261029_170900_workerfix.log` |
| 第 4 次 | S3 问卷/产品解析 + 回调验签与任务对账 + 六场景修复后 | **679 passed / 0 failed / 0 skipped**，466.45s | `evidence/finpilot_pytest_20261029_180900_scenes.log` |
| 第 5 次 | 容器隔离配置 + 外泄拒绝规则后 | **681 passed / 0 failed / 0 skipped**，358.27s | `evidence/finpilot_pytest_20261029_184500_round3.log` |

第 1 次的 3 个失败全部在 `tests/unit/domains/test_quant.py`、`test_suitability.py`：单元测试继承了演示配置的 `QUANT_BACKEND=remote`，`compare_products` 尝试请求未启动的量化服务，历史指标回退成 `QuantUnavailable`，而不是 `insufficient_history`。**这不是代码回归，而是测试基线受本机配置污染。**

已修复根因（`finpilot/tests/conftest.py`）：把 `APP_MODE`、`QUANT_BACKEND` 从 `os.environ.setdefault` 改为强制赋值，并在注释中说明需要其它模式的用例应自行 `monkeypatch.setenv`。修复后在**故意保留** `QUANT_BACKEND=remote`、`APP_MODE=live` 的环境下重跑：`test_quant.py` + `test_suitability.py` 46 passed；`test_model_live.py` + `test_quant_service.py` 43 passed。

第 2 次运行包含此前因缺少 `pg_dump` 而跳过的 4 个备份恢复用例（`FINPILOT_PG_BIN` 指到便携 PostgreSQL 后真实执行），因此从 668 项变为 672 项且无跳过。

### 4.2 演示栈

`run_demo.py` 在 mock 模式启动成功，五个进程均就绪：

| 检查 | 结果 |
|---|---|
| `GET /healthz` | `status=ok`，`database=PostgreSQL 17.9`，适配器 `bank_sandbox_local`、`bank_sandbox_v1`、`merchant_sandbox_local`、`merchant_sandbox_v1` 均已注册 |
| `GET /app/` | 200，503 字节，加载 `index-BW6YiIQO.js` / `index-JG0chknX.css`（仓库内预构建产物） |
| `GET /im/` | 200，500 字节 |
| `GET :58101/bank/health` | 200 |
| `GET :58102/merchant/health` | 200 |
| `GET :8770/healthz` | 200 |

### 4.3 前端

`pnpm install --frozen-lockfile`（pnpm 11.28.5、Node 24.16.0）成功安装 166 个包，锁文件供应链校验通过。随后：

| 检查 | 结果 | 日志 |
|---|---|---|
| `pnpm test` | **144 passed**（shared-ui 135、im 5、web 4），16s | `evidence/frontend_tests_20261029_envrepro.log` |
| `pnpm build` | 三个包全部通过，5s | `evidence/frontend_build_20261029_envrepro.log` |

重建产物文件名与交接包内预构建产物一致（`index-BW6YiIQO.js`、`index-JG0chknX.css`、`index-gr6Aj6UX.js`、`index-BUHEGP9v.css`），说明前端源码与预构建产物同源、构建可复现。注意 `finpilot/.gitignore` 忽略 `apps/*/dist/`，构建产物靠 `SHA256SUMS.json` 校验，不在 Git 跟踪范围内。

### 4.4 交接书声明与本次实测的差异

| 交接书 | 本次实测 |
|---|---|
| 历史一轮全量 671 项通过，但之后又改过代码 | 当前代码 679 项通过（含 4 项备份恢复、2 项 Worker 适配器回归、5 项 S3 问卷与申购），无跳过、无失败 |
| 备份恢复演练未执行 | 已在便携 PostgreSQL 上真实执行并通过 |
| 原 wheelhouse 只适配 Python 3.12 | 属实：3.12.10 离线安装成功，`requests`/`numpy`/`flask` 需联网 |
| 真实模型、六场景闭环、隔离、评测未完成 | 真实模型、隔离、评测仍未完成；六场景已从 23/35 修到 **35/35**（含回调对账），见 `docs/E2E_HTTP_2026-10-29.md` |

## 5. 仍未完成（不得计入已通过）

1. **真实模型联调**：本机没有新密钥，`live` 模式只跑过 HTTP 模拟模型服务；真实供应商的端到端、超时/429/非法输出和费用记录未做。
2. **六场景完整用户旅程**：真实 HTTP 验收已 35/35 通过（`evidence/e2e_http_full_20261029.json`），包含 S1 受理→回调→对账、S6 多商户确认/结算与资金捕获。但"推进结算"目前由验收脚本调用沙箱控制面触发；产品化的演示脚本/运营界面尚未提供，现场演示需要这一步骤。
3. **隔离**：Windows 侧仍只有 AST/子进程/审计钩子；Linux/容器无特权、资源与系统调用限制未验证。
4. **评测**：`eval/runner.py` 仍只接受 mock；真实模型任务集、留出集、消融与人工复核未做。
5. **赛事材料与官方要求**：27 项 / 92 项状态表仍未逐条举证；报名状态、截止时间、模拟渠道认可度需团队向组委会核实。

## 6. 复现结论

交接包文件完整（工程内 945/945、补充材料 29/29 与清单一致），在一台全新机器上可以按文档从零装起并跑通迁移、种子、五进程演示栈、679 项后端回归与六场景 35/35 真实 HTTP 验收。交接书对“环境可复现”的描述成立；对“真实模型与六场景闭环未完成”的描述同样成立。