> **历史交付包说明：本整合版新增依赖需要联网安装，具体以整合目录根 README 为准。下列文件数、哈希及测试数字对应原始 2026-10-28 包。**

# FinPilot 离线可运行包说明（PACKAGE_MANIFEST_OFFLINE）

| 项 | 内容 |
|---|---|
| 包名 | `finpilot-offline-2026-10-28.zip` |
| 定位 | **离线可运行版**：不需要联网即可安装 Python 依赖并运行系统；两个客户端已预构建，**不需要 Node.js** |
| 打包日期 | 2026-10-28 |
| 目标平台 | Windows x64 + CPython 3.12（wheel 按平台编译，见第 4 节） |
| 文件数 / 解压后大小 | 约 630 个文件 / 约 27 MB（其中 `wheelhouse/` 18.3 MB、50 个 wheel） |
| 校验 | 同目录 `finpilot-offline-2026-10-28.zip.sha256.txt`；包内 `SHA256SUMS.txt` 为逐文件哈希 |
| 运行模式 | `mock`（确定性脚本智能体，无在线大模型调用） |
| 验证状态 | 离线安装在本机实测通过；整仓 620 项测试、运行态 42 项验收见 `docs/` |

---

## 1 与「干净源码包」的区别

| 项目 | 源码包 `finpilot-2026-10-28.zip` | 本离线包 |
|---|---|---|
| 源码 / 契约 / 数据 / 评测 / 文档 | 有 | 有（完全相同） |
| `apps/*/dist` 前端构建产物 | 有 | 有 |
| Python 依赖 | 需联网 `pip install` | **`wheelhouse/` 50 个 wheel + 锁定版本，断网可装** |
| 前端依赖 | 需联网 `pnpm install` | 未包含（**运行时不需要**，见第 2 节） |
| `requirements-lock.txt` | 无 | 有（50 行精确版本） |
| 安装引导脚本 | `README.md` 手工步骤 | `scripts/bootstrap_offline.ps1` 一键完成 |

## 2 为什么没有把 `.venv` 与 `node_modules` 打进去

这是我按原计划尝试后**实测否决**的方案，理由是可验证的：

**（1）`.venv` 不可搬迁。** 虚拟环境记录的是绝对路径：

```
.venv/pyvenv.cfg:
    home = C:\Users\MURONGTANGJUN\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python
    executable = C:\Users\MURONGTANGJUN\.dsh\...\python\python.exe
    command = C:\Users\MURONGTANGJUN\.dsh\...\python\python.exe -m venv ...\finpilot\.venv
```

目标机器上不存在这个 `home` 路径，`python.exe` 会直接起不来；`Scripts\*.exe` 里的启动器也写死了本机路径。

**（2）`node_modules` 由 458 个符号链接/联接点组成。** `pnpm` 的 `node_modules/.pnpm` 采用硬链接+符号链接布局；ZIP 打包再解压后这些链接会变成普通文件或断链，`vite`/`vitest` 无法解析。Windows 资源管理器的内置解压也不保留链接。

**（3）这两样都有可靠的替代品：**

- Python 依赖 → `wheelhouse/`（50 个 `.whl`，`pip install --no-index` 直接用，不联网、不编译）
- 前端 → `apps/web/dist`、`apps/im/dist` 已构建，由后端 API 直接托管在 `/app/` 与 `/im/`；**运行演示完全不需要 Node.js / pnpm**。只有需要重新构建界面时才用 `pnpm install && pnpm -r build`。

> 换句话说：本包在“能跑起来”这个目标上比塞进 `.venv` 与 `node_modules` 更可靠；代价是不含前端构建工具链，但前端产物已经在包内。

## 3 安装与启动（断网可完成第 1—2 步）

前置条件：**Python 3.12（64 位）**；**PostgreSQL 16+**（本机实测 17.6）。Node.js 只有重建前端时才需要。

```powershell
# 0) 解压后进入目录
cd finpilot

# 1) 一键离线安装（约 1-2 分钟，全程不联网）
powershell -ExecutionPolicy Bypass -File scripts\bootstrap_offline.ps1
#    如系统里有多个 Python，可指定：
#    powershell -ExecutionPolicy Bypass -File scripts\bootstrap_offline.ps1 -Python "C:\Python312\python.exe"

# 2) 配置（只有数据库连接需要改）
copy .env.example .env

# 3) 建库 → 迁移 → 播种（需要本机已启动 PostgreSQL）
.\.venv\Scripts\python.exe -m alembic upgrade head
.\.venv\Scripts\python.exe -m finpilot.seed --json data\synthetic\seed_app.json

# 4) 启动（API + 2 个 worker + 银行/商户沙箱，约 10 秒）
powershell -ExecutionPolicy Bypass -File scripts\dev_up.ps1

# 5) 打开
start http://127.0.0.1:58000/app/     # APP 式客户端（预构建，无需 Node）
start http://127.0.0.1:58000/im/      # IM 风格客户端（预构建）
start http://127.0.0.1:58000/docs     # 交互式 API 文档

# 6) 停止
powershell -ExecutionPolicy Bypass -File scripts\dev_down.ps1
```

`bootstrap_offline.ps1` 会做三件事并自检：创建 `.venv` → 从 `wheelhouse/` 装依赖 → 以可编辑方式装本项目，最后打印域处理器数量（本机实测 22 个）。

## 4 平台与版本约束（重要）

- `wheelhouse/` 中的 wheel 是 **CPython 3.12 / win_amd64** 二进制（如 `psycopg_binary`、`cryptography`、`pydantic_core`）。换用其他 Python 版本或 Linux/macOS 时，这些 wheel 不可用，改用 `pip install -e .` 联网安装，或按 `requirements-lock.txt` 重新 `pip download` 对应平台的 wheel。
- 退出码与常见问题：
  - 提示 `Python 3.11+ not found` → 安装 Python 3.12 或加 `-Python <路径>`；
  - `pip install` 报 `No matching distribution` → Python 版本与 wheel 不匹配（见上一条）；
  - 已存在 `.venv` 时会复用，重建加 `-Force`。
- **PostgreSQL 不在包内**：它是外部服务，需单独安装；库表由迁移与种子数据重建，包内不含任何真实账户数据。

## 5 验证（预期输出）

| 命令 | 预期 |
|---|---|
| `scripts\bootstrap_offline.ps1` | `imports: OK` + `domain handlers: 22` |
| `pytest tests -q` | `620 passed`（约 6 分钟，需真实 PostgreSQL） |
| `pytest tests/contracts -q` | `146 passed` |
| `alembic check` | `No new upgrade operations detected` |
| `deploy\verify_install.ps1` | `9/9 PASS` |
| `scripts\dev_up.ps1` | `RESULT: UP` |

## 6 完整性校验

```powershell
# 压缩包本身
Get-FileHash .\finpilot-offline-2026-10-28.zip -Algorithm SHA256

# 解压后逐文件校验
Get-Content SHA256SUMS.txt | ForEach-Object {
  $hash, $path = $_ -split '  ', 2
  $actual = (Get-FileHash $path -Algorithm SHA256).Hash.ToLower()
  if ($actual -ne $hash) { "MISMATCH: $path" }
}
```

## 7 已知限制（与源码包一致，未做拔高）

- **运行模式为 `mock`**：仓库无任何大模型调用代码，`--mode live` 会被评测器拒绝；所有评测数字是工程回归，不是模型能力结论。
- **容器未验证**：`deploy/compose.yaml`、`Dockerfile`、nginx 齐备但从未构建运行（打包环境无 Docker）。
- **Windows 沙箱差距**：无 `resource` 模块，CPU/内存/进程/写入硬限额未生效，无 seccomp。
- **未完成项**：8 类故障未注入；澄清/拒绝的未见表达覆盖不足；隐藏集为团队内部评测；StockQuant 许可证待核验；演示视频未录制；评测记录 `review_status=pending_review`；Q01—Q05 未关闭。
- 逐条关闭条件见 `docs/limitations.md` 与《FinPilot 项目详细说明与功能报告》第 11 章。

## 8 建议阅读顺序

1. `PACKAGE_MANIFEST_OFFLINE.md`（本文）
2. `README.md`
3. `docs/FinPilot项目详细说明与功能报告.docx`
4. `docs/technical_document.docx`
5. `docs/evidence_index.md` / `docs/acceptance-live-e2e.md` / `docs/limitations.md`