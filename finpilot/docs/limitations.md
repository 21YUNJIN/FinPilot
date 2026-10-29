# 已知限制与未验证项

融策智能体（FinPilot）· 2026 深圳国际金融科技大赛

> 本文件是交付材料中**唯一有权写"做不到"的地方**，其余材料遇到此处列出的项必须写成"未验证/未实测"，不得反向包装。
> 每条包含：限制内容 / 为什么存在 / 影响的结论 / 解除条件 / 当前证据。
> 时点：2026-10-27 17:28—18:05（Asia/Shanghai）。

---

## 0. 必须置顶的七条

| 编号 | 限制 | 一句话影响 |
|---|---|---|
| L-01 | **无在线模型运行** | "换模型仍有效""注入防护对真实模型有效"**无法成立为已验证结论** |
| L-02 | **容器文件已存在但从未构建/运行** | 不得声称"可容器化部署"或"容器隔离已验证" |
| L-03 | **Windows 沙箱与 Linux 容器隔离存在实质差距** | 不得声称 CPU/内存/进程/写入硬限额已生效 |
| L-04 | **隐藏集是团队内部评测，不是第三方盲测** | 任何"隐藏集通过率"都不能作为独立证据 |
| L-05 | **StockQuant 许可证待核验** | 不得声称"已完成知识产权核验" |
| L-06 | **用户研究与专家复核未实际开展** | 不得声称"经用户验证/专家认可" |
| L-07 | **演示视频未录制** | 不得声称"已提交视频" |

> **另有第八条必须同等置顶（本轮新增实测）**：
> **L-08 评测小样本结果为未通过** —— mock/FULL/dev 30 条出现 **0 完成** 与 **7 条禁止效果**（4 条 `effect_duplicate_payment`、3 条 `effect_card_state_changed_unrequested`），根因未定位。详见 §L-08。

---

## L-01 无在线模型运行

**限制内容**
- 本机运行模式为 `mock`：确定性规则编译器 + 确定性本地提供方，**不调用任何大模型**。
- `services/api/src/finpilot` 内**不存在模型客户端代码**：`config.py:39-45` 只有 `model_base_url` / `model_api_key` / `model_name` / `model_temperature` / `model_max_retries` / `model_timeout_seconds` 六个字段，全仓检索无 `openai`、`chat/completions`、LLM SDK 调用【实跑】。
- 本机**没有** `MODEL_BASE_URL` / `MODEL_API_KEY`。
- `live` 模式**只有字符串声明**：`config.py:88-89` 的 `is_live_model` 属性定义了判定条件，但没有任何代码消费它去发起模型调用。

**为什么存在**：赛事准备期无可用的模型配额与密钥；且团队选择先把"服务端能力边界"做实，再谈模型质量。

**影响的结论**
| 主张 | 是否可声明 |
|---|---|
| "服务端权限边界不依赖 Prompt" | ✅ 可以（有源码与用例） |
| "换成任意模型仍保持同样的安全边界" | ⚠️ 只能作为**架构论证**（模型不在信任边界内），**不能**作为实测结论 |
| "对真实模型的注入攻击已被拦截" | ❌ 不可声明 |
| "两模型复核完成" | ❌ 不可声明 |
| 任何"模型完成率/token 成本/延迟"数字 | ❌ 不可声明 |

**解除条件**：接入至少两个模型提供方 → 跑攻击集 + 隐藏集 → 报告拦截率、完成率、区间与失败例；并在方案中说明模型标识、温度、Prompt 哈希与成本。

**当前证据**：`config.py:39-45, 88-89`；`intent/compiler.py:3-4`（"默认使用确定性规则编译器（可在无模型环境下复现）"）；【实跑】`/healthz` 返回 `"mode": "mock"`。

---

## L-02 容器文件已存在，但从未构建/运行

**限制内容**
- **快照期内**并行工作包新增了 `deploy/Dockerfile`（2080 B）与 `deploy/compose.yaml`（10782 B），其中声明了 `read_only: true`、`no-new-privileges:true`、`cap_drop: ALL`、非 root（`USER 10001:10001`）、`tmpfs /tmp`。
- **但这些文件从未被构建或运行**：本机**没有 docker CLI**（`Get-Command docker` 无结果）；`wsl -l -v` 返回"适用于 Linux 的 Windows 子系统没有已安装的分发版"。
- `deploy/Dockerfile:6-9` 自己写明："NOT VERIFIED ON THE REFERENCE HOST: this machine has no Docker/container runtime (verified: `docker` is not on PATH), so this image has never been built here."
- `deploy/sandbox-policy.yaml` 是**参考配置**（含 Linux 容器策略与 `windows_dev_equivalence` 差距段），**从未被任何容器引擎加载执行**。

**为什么存在**：本机为 Windows 开发环境，未安装 Docker Desktop，也未启用 WSL 发行版。

**影响的结论**
| 主张 | 是否可声明 |
|---|---|
| "提供了 Linux 容器策略参考配置" | ✅ 可以（文件在仓） |
| "已验证容器隔离/只读根/seccomp/非 root" | ❌ 不可声明 |
| "可一条命令容器化部署" | ❌ 不可声明 |
| "干净环境部署完成（DEL-02）" | ❌ 未完成 |

**解除条件**：在 Linux 主机上构建镜像 → 跑通迁移+测试 → 导出沙箱返回的 `limits_enforced`（应包含 `cpu_rlimit`/`memory_rlimit`/`process_rlimit`/`file_write_rlimit`）→ 留存容器运行时配置证据。

**当前证据**：`deploy/sandbox-policy.yaml`；`security/sandbox.py:28-31`（原文承认容器策略"在本环境**未被验证执行**"）。

---

## L-03 Windows 沙箱与 Linux 容器隔离的实质差距

**限制内容**：本机沙箱是"父进程 AST 白名单 + 独立子进程（`-I -S`） + Python 层 audit hook + 墙钟/输出限额"，**不是**内核级隔离。

| 控制 | Linux 容器目标 | Windows 本机实际 |
|---|---|---|
| CPU 时间硬限额 | `RLIMIT_CPU` / cgroup | ❌ 未生效（无 `resource` 模块，实测 `False`） |
| 内存硬限额 | `RLIMIT_AS` / cgroup 512MB | ❌ 未生效 |
| 进程数上限 32 | `RLIMIT_NPROC` / pids cgroup | ❌ 未生效；仅 audit hook 拦截创建 |
| 文件写入禁止 | 只读根 + `RLIMIT_FSIZE` | ❌ 未生效；仅 audit hook 拦截 |
| 系统调用约束 | seccomp | ❌ 未生效（实测无 `libseccomp`） |
| 文件系统隔离 | 只读根 + 无宿主挂载 | ❌ 无内核级隔离 |
| 网络禁止 | network=none | ⚠️ 仅 audit hook 拦截 `socket.*` |
| 身份 | 非 root | ❌ 未降权 |
| 静态代码校验 | 可选 | ✅ 生效 |
| 墙钟超时 + 输出上限 | 生效 | ✅ 生效 |

**为什么存在**：Windows 无 `resource` 模块；audit hook 是 CPython 层的审计事件，绕过它的原生扩展调用（自带 C 扩展直接 `open(2)`）不会被拦截；同一用户下并发运行的其他进程不受约束。

**影响的结论**：可以声称"受限执行 + 不可用即关闭"；**不能**声称"资源硬限额"或"隔离"。代码本身也如实输出 `limits_enforced` / `limits_not_enforced`，并在 Windows 分支写入明确提示（`sandbox.py:1306-1307`），这是刻意设计而非兜底话术。

**解除条件**：迁到 Linux 容器后，让 `limits_enforced` 覆盖 `cpu_rlimit`/`memory_rlimit`/`process_rlimit`/`file_write_rlimit`，并补 `seccomp` 与只读根的运行时证据。

**当前证据**：`security/sandbox.py:1-33, 1104-1129, 1306-1307`；`tests/security/test_sandbox.py`（26 例，含"不可用即关闭且宿主不被触碰"）。

---

## L-04 隐藏集为团队内部评测，不是第三方盲测

**限制内容**
- `eval/data/hidden/{tasks.json,attacks.json}` 由**团队自己**保管与使用；`source_manifest.json:33` 记为"隐藏集仅由保管人维护（团队内部评测）"。
- 因此"隐藏集"只保证**拆分不泄漏**（family / phrasing_group / shared_utterance 跨拆分计数均为 0），**不保证**"调参者看不到它"。
- 更进一步：**隐藏集至今尚未实际调分**——运行器 `eval/runner.py` 已存在，但本轮只跑过 dev 30 条（结果为未通过，见 L-08）。

**影响的结论**：任何"隐藏集完成率/拦截率"数字都不能作为**独立**证据；只能读作"团队自评"。评审若要求独立证据，本仓库当前无法提供。

**解除条件**：① 实现运行器并跑出冻结结果；② 或交由第三方/组委会保管与评分；③ 至少公开全部分母、失败例与判定器版本。

**当前证据**：`eval/data/task_index.json` 的 `leakage_checks`（4 项为 0/true）；`eval/__init__.py:5-9`（声明 `eval.runner` 但文件不存在）。

---

## L-05 StockQuant 许可证待核验

**限制内容**
- `data/metadata/source_manifest.json:106-114`：`name=stockquant_third_party_code`，`type=public`，`license="待核验（尚未取得许可证文件，禁止声称已核验）"`，`sha256=null`，`available_at=null`。
- `declaration.stockquant_license_verified = false`，`stockquant_license_status = "待核验（未核验，禁止声称已核验）"`。
- 本仓库**未复制**其代码副本，`adapters/stockquant/risk_report.py` 是**本项目自行实现**的统计方法（年化波动、历史 CVaR、最大回撤），仅在"来源清单"里登记为"只读适配目标"。

**为什么存在**：未取得许可证文件原件，也未核验其依赖与版本。

**影响的结论**：不得声称"已完成知识产权核验"（OC23 / GOV-02）；不得声称"复用 StockQuant 代码"。可以声称"以只读受限适配器**接入其研究思路**，指标为自实现且方法版本可追溯"。

**解除条件**：取得许可证文件并归档 → 更新清单（`license` / `sha256` / `available_at`）→ 把 `stockquant_license_verified` 置 true，并保留核验记录。

**当前证据**：`source_manifest.json:106-114`；`tests/unit/domains/test_data_provenance.py:780-786`（测试**强制断言**该状态必须是"待核验"，防止被悄悄改成已核验）。

---

## L-06 用户研究与专家复核未实际开展

**限制内容**
- USER-01（≥2 名合格复核者的业务审查）：**无复核者、无记录**。
- USER-02（匿名、交叉平衡的探索性用户测试）：**无参与者、无任务脚本、无问题修复记录**。
- 仓库内没有任何用户测试报告、访谈记录、SUS/任务成功率采集脚本或知情同意文件。

**影响的结论**：不得声称"经用户验证""专家认可""体验优于同类"。可用性主张只能停留在"组件级一致性由 142 条前端用例保证"，且这**不是**可用性研究。

**解除条件**：招募匿名参与者（含平衡设计）→ 记录任务完成与问题清单 → 至少 2 名业务复核者签字 → 归档原始材料（不含多余身份信息）。

**当前证据**：`docs/requirement-trace.md:105-106`（USER-01/02 行的"当前实现状态"列为空）；仓库内检索无相关产物。

---

## L-07 演示视频未录制

**限制内容**：`docs/demo.mp4` **不存在**。仓库内无任何视频、录屏或分镜素材文件。`docs/demo_script.md` 只是**分镜脚本**。

**影响的结论**：不得声称"已提交视频"；视频属国内赛道**可选**项（OFF-6），但本项目此前计划"主动交付"，当前**未兑现**。

**解除条件**：按 `docs/demo_script.md` 录制 ≤5 分钟、标明运行模式、保留失败片段不剪。

**当前证据**：`docs/` 目录实测无 mp4；交付材料清单见 `docs/evidence_index.md` G4-10。

---

## 8. 其他已核实限制（非七条置顶，但同样必须披露）

### L-08 评测链路已跑通但结果未通过；统计与形式化模块仍缺失

- 快照期内 `eval/runner.py` 与 `eval/baselines.py` 已由并行工作包新增（`runner.py` 编制期间从 69283 增至 74625 字节，持续变化）；`eval/analysis.py`、`eval/formal.py` **仍不存在**。
- **本轮实跑（mock / FULL / dev 30 条 / 1 次重复，2026-10-27 17:50）结果为未通过**：
  - `outcomes = {refused 8, failed 16, partially_completed 4, unknown 2}` → **completed = 0**
  - `forbidden_effect_count = 7`：`effect_duplicate_payment` ×4、`effect_card_state_changed_unrequested` ×3
  - 平均 319.07 ms，总耗时 34.39 s，30 条全部 `review_status = pending_review`（未人工复核）
- **根因未定位**。两种可能都不能排除：(a) 运行器驱动与 `mock` 链路未对齐（同一意图被驱动提交两次）；(b) 系统存在真实的重复扣款/未请求卡状态变更缺陷。
- **受影响的结论**：
  - ❌ 不得声称"端到端不重复扣款"；
  - ⚠️ 只能声称"单元/集成层的幂等、账本唯一约束、授权单次消费与回调去重测试通过（42 passed / 26 passed / 50 passed）"；
  - ❌ 不得声称任何完成率；本次 0 完成的数字也**不可**用于说明系统"差"，因为可能是评测链路自身问题。
- **解除条件**：定位这 7 条的成因（读 `evidence_paths` 指向的账本分录与操作记录，判断是驱动重复提交还是真实重复入账）→ 修复 → 重跑并给出 `--repeats 5` 的区间结果与人工复核签署。

### L-09 `offline_replay` 模式只有枚举，没有实现

- `settings.app_mode ∈ {live, offline_replay, mock}` 在 `config.py`、`contracts/schemas/*.json`、前端 `ModeBadge` 中都登记了；但**没有任何代码**读取冻结轨迹并重放驱动任务。
- 后果：不得声称"支持离线重放"；只能声称"运行模式字段已预留且前端会如实标注"。
- 解除条件：实现轨迹录制与回放器，并在任务与响应中标注 `offline_replay`。

### L-10 `bank_http` 适配器缺失

- `app.py:38-44` 尝试 `from .adapters.bank_http import HttpBankAdapter`，该模块**不存在**；`/healthz` 如实返回 `"bank_http": "unavailable: ModuleNotFoundError"`【实跑】。
- 后果：当前**只有本地/沙箱适配器**，没有"真实银行 HTTP 适配器"。不得声称"已对接银行接口"。
- 解除条件：实现该适配器（或从装配中移除并写明现状）。

### L-11 数据清单仍有一处不一致

- `source_manifest.json:52` 的 notes 写"56 条故障变体"，`fault_catalog.json` 实际 **58** 条。
- 后果：不影响测试（用例未断言条数），但影响可追溯性。引用时应以文件为准。
- 解除条件：更新 notes 为 58，或说明 2 条为何不计入。

### L-12 并发验证的边界

- 并发/锁语义测试在**真实 PostgreSQL** 上跑（`tests/conftest.py:1-5` 明确禁止用 SQLite 替代），这是加分项。
- 但仍局限于**单进程内并发**：没有多实例、多进程、跨机器压测；没有对连接池耗尽、锁等待超时（`lock_timeout 20s` / `15s`）在真实负载下的表现留证。
- 后果：不得声称"高并发下已验证"。

### L-13 未做备份恢复演练（NFR-02）

- 仓库有 `migrations/versions/0001_initial.py`（1060 行）与 `data/README.md`，但**没有**任何"备份→清库→恢复→对账"的演练记录。
- 主库 `finpilot` 当前 `tables=0`（未初始化）；`finpilot_dev_deploy` 有 48 张表且 `alembic_version=0001`【实跑】。

### L-14 未做前端专项安全测试

- 前端有 142 条行为用例，但**没有** CSP、XSS、CSRF、依赖审计（SCA）的专项测试或报告。
- `pnpm` 依赖树未做漏洞扫描。

### L-15 未做负载与成本测量（NFR-01）

- 无延迟分位数、无成功率、无并发上限、无 token/成本记录（因无模型、无压测）。

### L-16 未核验外部依赖条款

- `llm_provider_api`（模型供应商服务条款）、`postgresql_server`（许可证待归档）、`python_runtime`（PSF-2.0 未单独归档）均**未核验/未归档**。

### L-17 官方模板与网页未归档

- OFF-1—OFF-6 在 `docs/BANKAGENT_SRS_V5_COMPETITION.md:27-32` 以链接登记，**正文/模板文件未存入仓库**。
- 后果：若官网内容变更，无法比对"我们按哪一版做的"。属可追溯性缺陷。

### L-18 Q01—Q05 全部未关闭

见 `docs/official_mapping.md` §4。这五项属外部/行政依赖，**不能**由内部测试替代。

---

## 9. 不得出现的表述清单（对外材料自查用）

以下句子若出现在简介、PPT、答辩或技术文档中，即为违规：

1. "已通过隐藏集测试，完成率 XX%"（L-04、G3）
2. "支持在线大模型运行 / 已接入某模型"（L-01）
3. "已验证容器隔离 / 沙箱硬限额"（L-02、L-03）
4. "StockQuant 许可证已核验"（L-05）
5. "经用户测试与专家评审认可"（L-06）
6. "已录制并提交演示视频"（L-07）
7. "已提交/已获初赛资格/获第 X 名"（G5）
8. "支持离线重放模式"（L-09）
9. "已对接真实银行接口"（L-10）
10. "高并发场景已验证"（L-12）
11. "验证方式已获赛事认可"（Q02）
12. 任何未标注来源的覆盖率、通过率、提升幅度数字。
13. **"端到端已证明不重复扣款"**（L-08：小样本跑出 4 条 `effect_duplicate_payment`，未定位）。
14. **"评测已全部通过"**（L-08：G3 记为未通过）。

**正确写法示例**：
- ✅ "本机以 `mock` 模式实测 146 条契约用例、162 条安全用例、142 条前端用例全部通过；在线模型运行未实测（无 API key，仓库无模型客户端）。"
- ✅ "沙箱在 Windows 上生效的是 AST 白名单、audit hook、墙钟与输出限额；CPU/内存/进程/写入硬限额未生效（无 `resource` 模块），容器路径未验证。"
- ✅ "隐藏集为团队内部评测拆分，泄漏检查 4 项通过；隐藏集尚未调分。"
---

## 附录 Z 复核更正（2026-10-27 19:10，最终验收时复核）

以下为验收阶段复核后**推翻或更新**的条目，请以本节为准；正文相关段落已过时。

### Z.1 已不成立的条目

| 编号 | 原表述 | 复核结果 |
|---|---|---|
| GAP-6 | `source_manifest.json` 的哈希与索引记录不符 | **不成立**。磁盘实测 `f070486b…e121b`，`eval/data/task_index.json` 的 `artifacts[source_manifest].sha256` 同为 `f070486b…e121b`；`dataset_hash = 200ac215…ac66` 复算一致。 |
| GAP-3 | `card.set_control` / `mandate.revoke` / `subscription.cancel` / `aa.create` 处理器缺失 | **不成立**。`finpilot.orchestration.executor.tool_handlers()` 现返回 **22 个**处理器，上述四个均在列（实测输出含 aa.create、card.set_control、mandate.revoke、subscription.cancel）。 |
| — | `effect_duplicate_payment` 由"分录条数当付款次数"引起 | **该诊断错误**。判定器一直按 `(tool, typed_args)` 签名归并成功操作；那 7 条触发实为卡控快照形状比较 ×3 与只读任务副作用要求 ×4。已按正确原因修复。 |

### Z.2 已更新的数字

- 全仓测试：`pytest tests -q` → **620 passed / 0 failed**（此前记录的 577/570 为阶段性结果）。
- 判定器修复前后（同命令、同 seed、`--limit 12`）：`forbidden_effect_count` **7 → 0**；`completed` 2 → 2；`partially_completed` 2 → 5；`failed` 6 → 2。
- 仍为负结果且**未修复**的部分：`executable_completion_rate 2/6`、`clarification_accuracy 2/6`、`refusal_accuracy 2/6`；`attack_block_rate 12/12`、`constraint_violation_rate 0/30`。样本仅 30 条，区间很宽，不构成方法结论。

### Z.3 仍然成立的限制

无在线模型（`--mode live` 会拒绝运行）/ 容器未构建 / Windows 沙箱无 CPU·内存·进程硬限额 / 8 类故障未注入 / 隐藏集为团队内部评测 / StockQuant 许可证待核验 / 演示视频未录制 / 所有评测记录 `review_status = pending_review`（未经人工签署）/ Q01—Q05 未关闭。