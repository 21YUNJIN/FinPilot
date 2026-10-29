# 质量门槛逐项证据索引（G0—G5）

融策智能体（FinPilot）· 2026 深圳国际金融科技大赛

> 门槛定义出自 `docs/BANKAGENT_SRS_V5_COMPETITION.md` §21.2（第 755—766 行）。
> **本文件的规则**：每条只登记**可核验的证据路径或命令**与**真实结果**。未通过、未验证、未开展的项一律写明，不用"设计覆盖"充数。
> 证据时点：2026-10-27 17:28—18:05（Asia/Shanghai）。仓库处于并行编辑状态，涉及哈希的证据同时给出时间。

---

## 0. 总览

| 门槛 | 退出条件（SRS §21.2） | 结论 | 未通过/未验证项 |
|---|---|---|---|
| **G0** 规则与契约冻结 | 官方对应表、外部问题清单、Schema、初始威胁模型、算法和评测协议完成 | **通过（内部产物齐全）**；官方文本仅以链接登记，未在仓库存档 | Q01—Q05 未关闭；官方模板与网页未归档 |
| **G1** 可信执行内核 | 授权、账本、并发、未知结果、恢复和沙箱关键测试通过 | **通过（离线/mock）** | 并发与恢复未在容器/多进程真实部署下压测 |
| **G2** 六场景完成 | 每场景正常、边界、失败链路均可查真实状态 | **部分通过** | 六场景端到端人工演示 KPI 未采集；`bank_http` 适配器缺失 |
| **G3** 研究证据 | 冻结隐藏集、强基线／消融／两模型、全部结果及统计完成 | **未通过** | 运行器/基线/统计/形式化模块缺失；两模型未跑；H4 未跑 |
| **G4** 可用性与发布 | 专家／用户复核、干净环境复现、材料校验与演示预演完成 | **未通过** | 无用户/专家复核；容器文件在仓但从未构建；视频未录制 |
| **G5** 参赛提交核准 | 资格、Q01—Q05、实际上传与回执核对完成 | **未通过（行政项，无证据）** | 无报名回执、无上传回执；Q01—Q05 全未关闭 |

---

## G0 规则与契约冻结

**退出条件**：官方对应表、外部问题清单、Schema、初始威胁模型、算法和评测协议完成。

| # | 证据项 | 路径 / 命令 | 真实结果 | 状态 |
|---|---|---|---|---|
| G0-1 | 官方来源对应表 OFF-1—OFF-6 | `docs/BANKAGENT_SRS_V5_COMPETITION.md:27-32` | 6 条，逐条给出官网/规则/模板链接与用途 | ✅ 通过 |
| G0-2 | 官方条款到需求/测试/证据对照 | 同上 `:801-829`（OC01—OC27） | 27 行，含"设计覆盖 / Q0x 待核"标注 | ✅ 通过 |
| G0-3 | 外部问题清单 | 同上 `:56-60`（Q01—Q05） | 5 条，各含关闭方式 | ✅ 通过（**5 条全未关闭**） |
| G0-4 | 需求追溯矩阵（92 项） | `docs/requirement-trace.md`（215 行）；生成/校验脚本 `tests/contracts/test_requirement_trace.py` | 92 需求 / 24 工作包 / T01—T60；"当前实现状态"列**故意留空**，空单元格不等于通过 | ✅ 通过 |
| G0-5 | 契约 Schema | `contracts/schemas/*.json` **39 个**；示例 `contracts/examples/*.json` **80 个** | 命令：`pytest tests/contracts -q` → **146 passed in 5.65s** | ✅ 通过 |
| G0-6 | 工具目录冻结 | `contracts/tool-catalog.yaml`、`security/policy.py:31-73` | 37 工具；`GET /v1/meta/tools` 与 `agent_exposed` 分离，契约测试断言写工具不在模型可见集合 | ✅ 通过 |
| G0-7 | 初始威胁模型 | `docs/BANKAGENT_SRS_V5_COMPETITION.md:598-602`（§17.1） | 覆盖恶意账单备注/商户描述/工具返回/检索文档/用户输入、跨用户引用、授权重放、参数篡改、重复提交、伪造回执、越权操作员、模型幻觉、不可信生成代码 | ✅ 通过 |
| G0-8 | 算法规格 | `docs/BANKAGENT_SRS_V5_COMPETITION.md:180`（§5 目标约束编译与计划算法）；实现 `intent/compiler.py`、`orchestration/planner.py`、`orchestration/dag.py` | 字典序比较键已在代码中固化（`planner.py:527-541`） | ✅ 通过 |
| G0-9 | 评测协议 | `eval/data/split_protocol.md`（10288 字节）；索引 `eval/data/task_index.json` | 拆分单位 `template_family`；4 项泄漏检查全为 0/true | ✅ 通过 |
| G0-10 | 金额与数据口径 | `docs/BANKAGENT_SRS_V5_COMPETITION.md:134`（§4）；`docs/INTERFACES.md`；`money.py`、`timeutil.py` | 整数分、份额 1e-8、UTC 持久化、Asia/Shanghai 业务日 | ✅ 通过 |
| G0-11 | 官方模板与网页正文归档 | — | **仓库内只有链接，没有 OFF-1—OFF-6 的正文/模板文件归档** | ❌ **未完成** |
| G0-12 | Q01 自建沙箱与第二客户端是否满足国内验收 | — | 无组委会答复 | ❌ **未关闭** |
| G0-13 | Q02 强验证方式的接受范围 | — | 无书面说明 | ❌ **未关闭** |
| G0-14 | Q03 AI 辅助开发披露要求 | — | 无官方答复；AI 使用与来源清单已在 `source_manifest.json` 部分登记 | ❌ **未关闭** |
| G0-15 | Q04 报名系统字段/格式/截止 | — | 未核验 | ❌ **未关闭** |
| G0-16 | Q05 学籍/赛道/唯一报名/权属 | — | 未核验（行政档案不放代码库） | ❌ **未关闭** |

**G0 结论**：内部可冻结的产物（对照表、Schema、威胁模型、算法、评测协议、追溯矩阵）**齐全且可校验**；**官方侧与行政侧的证据全部缺失**，故 G0 记为"内部通过、外部未关闭"。

---

## G1 可信执行内核

**退出条件**：授权、账本、并发、未知结果、恢复和沙箱关键测试通过。

| # | 证据项 | 命令 / 路径 | 真实结果 | 状态 |
|---|---|---|---|---|
| G1-1 | 账本 + 授权 + DAG 规划 | `pytest tests/unit/kernel/test_ledger.py tests/integration/test_authorization.py tests/unit/planner/test_dag.py -q` | **42 passed in 29.91s** | ✅ 通过 |
| G1-2 | 沙箱安全 | `pytest tests/security -q`（库 `finpilot_test_docs`） | **162 passed in 49.87s**（含 `test_sandbox.py` 26 例级联） | ✅ 通过 |
| G1-3 | 契约（含提供方沙箱） | `pytest tests/contracts -q` | **146 passed in 5.65s**；其中 `test_provider_sandbox.py` 单跑 **26 passed in 5.62s** | ✅ 通过 |
| G1-4 | 端到端 HTTP 流（会话/确认/强验证/隔离/版本冲突/隐私） | `pytest tests/integration -q` | **50 passed in 54.74s** | ✅ 通过 |
| G1-5 | 双重记账与预留公式 | `kernel/ledger.py:1-12, 48-56, 106-154`；`conservation_check()` | 分录和必须为 0；可用 = B−H−F；可支配 = max(0, B−H−F−E)；多账户 E 只扣一次 | ✅ 通过（源码 + 用例） |
| G1-6 | 未知结果与增量恢复 | `kernel/state_machine.py:39-49`；`orchestration/recovery.py:1-8`；`orchestration/dag.py:104-113` | `dispatching→pending→unknown→{succeeded,failed,pending}`；受影响集合 = 变化事实 ∩ 未完成 read_set 的后继闭包 | ✅ 通过（源码 + 用例） |
| G1-7 | 幂等（防重复扣款） | `kernel/submission.py:90-102`；`errors.py:50`；`grants.py:302-308`；`limits.py:107-112` | 同键异参 → `IDEMPOTENCY_CONFLICT`(409)；`(operation_id, kind)` 入账唯一 | ✅ 通过 |
| G1-8 | 审计链 | `security/audit.py:1-37`；`tests/security/test_audit.py`（18 例） | 可检测 payload 改动/中间删除/重排/互换/签名替换/尾部删除 | ✅ 通过 |
| G1-9 | 提供方沙箱独立性 | `services/provider-sandbox/README.md:5-16`；契约测试静态校验不 import `finpilot` | 两个独立进程 + 各自 sqlite；数据库层唯一约束保证只结算一次 | ✅ 通过 |
| G1-10 | 并发在真实 PostgreSQL 上验证 | `tests/conftest.py:1-5, 55-65`（拒绝 SQLite 替代） | ✅ 但仅**进程内并发 + 真实 PG**，**未做多进程/多实例压测** | ⚠️ 部分 |
| G1-11 | 沙箱资源硬限额 | — | Windows 下 CPU/内存/进程/写入**硬限额未生效**（§技术文档 7.3） | ❌ **未通过（平台限制）** |
| G1-12 | 提供方沙箱服务实际运行 | 端口 58101/58102/58000 | 快照时点**无监听**；行为由契约测试在进程内驱动 | ❌ **未验证** |

**G1 结论**：内核类测试**全绿**（42 + 162 + 146 + 50，去重后覆盖授权/账本/并发/未知结果/恢复/沙箱/审计）。未通过项集中在**平台隔离能力**（G1-11）与**真实服务长跑**（G1-12）。

---

## G2 六场景完成

**退出条件**：每场景正常、边界、失败链路均可查真实状态。

| # | 场景 | 实现路径 | 测试路径 | 真实结果 | 状态 |
|---|---|---|---|---|---|
| G2-1 | S1 转账/定时/AA | `domains/aa/service.py`、`kernel/transfers.py`、`kernel/scheduled_jobs.py` | `tests/unit/domains/test_aa.py` | 随前端与领域用例套件通过；**未单独统计本文件用例数** | ⚠️ 部分 |
| G2-2 | S2 账单 | `domains/billing/{service,anomalies,classification}.py` | `tests/unit/domains/test_billing.py` | 同上 | ⚠️ 部分 |
| G2-3 | S3 理财 | `domains/wealth_rules/{assessment,compare,suitability,terms}.py`、`adapters/stockquant/` | `tests/unit/domains/{test_quant,test_suitability}.py` | 同上 | ⚠️ 部分 |
| G2-4 | S4 卡片 | `domains/cards/{service,repository}.py`、`security/card_controls.py` | `tests/unit/domains/test_cards.py` | 同上 | ⚠️ 部分 |
| G2-5 | S5 订阅 | `domains/subscriptions/{service,mandates,reminders}.py` | `tests/unit/domains/test_subscriptions.py`、`tests/integration/test_subscription_cancellation.py` | 同上 | ⚠️ 部分 |
| G2-6 | S6 跨场景 | `orchestration/recovery.py`、`domains/*` 组合 | 数据集 `eval/data/task_index.json` 的 S6 100 条 | **隐藏集未调分**；组合任务未端到端人工演示 | ❌ **未验证** |
| G2-7 | 两个客户端 | `apps/web`（4 测试）、`apps/im`（5 测试）、`apps/shared-ui`（133 测试） | `pnpm -r test` | **142 passed**（133+5+4） | ✅ 通过 |
| G2-8 | 场景状态可查 | 各域 `*.status` 只读工具（GREEN） | `GET /v1/meta/tools` | 37 工具中 14 项 GREEN 只读 | ✅ 通过 |
| G2-9 | 六场景人工演示与 KPI | — | — | **未做**；无延迟/成功率采集 | ❌ **未完成** |

**G2 结论**：六场景**实现与单元/集成测试在仓**，客户端有真实用例；但"每场景可查真实状态"的**人工端到端演示与 KPI 未采集**，S6 跨场景未端到端验证。记 **部分通过**。

---

## G3 研究证据

**退出条件**：冻结隐藏集、强基线／消融／两模型、全部结果及统计完成。

| # | 证据项 | 路径 | 真实结果 | 状态 |
|---|---|---|---|---|
| G3-1 | 数据集冻结 | `eval/data/task_index.json` | 600 任务（dev 300 / val 120 / hidden 180）；`dataset_version=tasks-2026.10`；`generated_at=2026-10-22T10:00:00+08:00` | ✅ 通过 |
| G3-2 | 泄漏检查 | 同上 `leakage_checks` | family/ phrasing_group / shared_utterance 跨拆分计数**均为 0**；实体池拆分互斥为 true；3 条 hidden_novelty | ✅ 通过 |
| G3-3 | 数据集哈希 | 同上 `dataset_hash` | **`200ac215ed600b511960b1208b0fc4787c19eba0cfb9042f829f188c8475ac66`**（本机复算一致；17:38 前为 `319721e9…4e7d`，原因见技术文档 §5.3） | ✅ 通过 |
| G3-4 | 攻击/故障集 | `eval/data/{attack_catalog,fault_catalog}.json` | 攻击 200（dev 100 / hidden 100，12 类）；故障 58（12 类；可恢复 14 / 需重查询 19 / 需重新授权 21 / 不可恢复需接管 4） | ✅ 通过 |
| G3-5 | 判定器（只按权威状态判分） | `eval/oracle.py`（`ORACLE_VERSION=oracle-2026.10`；编制期间被并行改写，不引用字节数） | **可导入可调用**：`judge_task(record, *, expected, state)`、`summarize_judgments(judgments)` | ✅ 通过 |
| G3-6 | 判定词表 | `eval/data/task_index.json` | `final_state_vocabulary` 10 / `forbidden_effect_vocabulary` 10 / `clarification_vocabulary` 10 / `judge_definitions` 10 | ✅ 通过 |
| G3-7 | 评测运行器 | `eval/runner.py`（`RUNNER_VERSION=runner-2026.10`；编制期间由 69283 增至 74625 字节，持续变化） | **已存在**（快照期内由并行工作包新增）；设计为真实 HTTP 链路 + 每任务独立库重建 + JSONL 逐条证据 | ✅ 已实现 |
| G3-8 | 强基线与消融开关 | `eval/baselines.py`（14557 B） | **已存在**；`--list-configs` 可列出 A1—A4 等配置（实测输出 JSON 数组） | ✅ 已实现，**未运行** |
| G3-9 | 统计与失败归因 | `eval/analysis.py` | **文件不存在**；无五次重复、无配对 bootstrap、无区间估计；本轮 `--repeats 1` | ❌ **未实现** |
| G3-10 | 有界状态空间不变量检查 | `eval/__init__.py` 声明 `eval.formal` | **文件不存在** | ❌ **未实现** |
| G3-11 | 两模型复核 | — | **未执行**；仓库无模型客户端，无 API key | ❌ **未执行** |
| G3-12 | H4 消融实测 | 实现已在：`adapters/stockquant/risk_report.py:630`、`domains/wealth_rules/compare.py:223,294` | **未运行**，无增量数字 | ❌ **未执行** |
| G3-13 | 隐藏集调分结果 | — | **未运行** | ❌ **未执行** |
| G3-16 | **小样本实跑（本轮）** | `python -m eval.runner --config FULL --split dev --limit 30 --repeats 1`（`TEST_DATABASE_URL=…/finpilot_test_eval`，输出至仓库外临时路径） | **未通过**：`outcomes = {refused 8, failed 16, partially_completed 4, unknown 2}` → **completed = 0**；`forbidden_effect_count = 7`（`effect_duplicate_payment` ×4、`effect_card_state_changed_unrequested` ×3）；`mean_wall_ms 319.07`；`elapsed 34.39s`；30 条全部 `review_status = pending_review`。对照 3 条冒烟运行：`clarified 1 / completed 1 / partially_completed 1`，禁止效果 0 | ❌ **未通过，根因未定位** |
| G3-17 | 人工复核签署 | JSONL 内 `reviewer = "TBD（未复核…）"` | 30/30 未复核 | ❌ **未完成** |
| G3-14 | 全量失败例归档 | — | 无运行，故无失败例归档 | ❌ **未完成** |
| G3-15 | 第三方盲测 | — | 隐藏集由**团队内部**保管 | ❌ **不存在** |

**G3 结论**：**未通过**。数据、判定器、运行器与基线开关已就绪，并已跑出第一批真实数字；但这批数字是**负结果**——dev 30 条 0 完成、7 条禁止效果，且全部未经人工复核。统计与形式化模块仍缺失，两模型与 H4 未执行。因此**没有任何可对外引用的完成率或提升幅度**；唯一可引用的是"当前 mock 端到端链路未达可用水平"这一事实本身。

---

## G4 可用性与发布

**退出条件**：专家／用户复核、干净环境复现、材料校验与演示预演完成。

| # | 证据项 | 路径 / 命令 | 真实结果 | 状态 |
|---|---|---|---|---|
| G4-1 | 干净环境部署（容器） | `deploy/Dockerfile`（2080 B）、`deploy/compose.yaml`（10782 B） | 容器文件**已存在**（快照期内由并行工作包新增），含只读根/非 root/cap-drop ALL 声明；但本机**无 docker CLI**、`wsl -l -v` 报"没有已安装的分发版" → **从未构建、从未运行** | ❌ **未完成（文件在，未验证）** |
| G4-2 | 数据库迁移可执行 | `migrations/versions/0001_initial.py`（1060 行） | `finpilot_dev_deploy` 实测 `tables=48, alembic_version=('0001',)`；主库 `finpilot` 当前 `tables=0` | ⚠️ 部分（迁移已验证，主库未初始化） |
| G4-3 | 依赖锁定 | `pyproject.toml`、`pnpm-lock.yaml`、`.env.example` | 存在；本机实测版本已记录（技术文档 §8.1） | ✅ 通过 |
| G4-4 | 测试可复现 | 见 G1 各行命令 | 全部通过（除平台限制项） | ✅ 通过 |
| G4-5 | 专家业务复核（≥2 名合格复核者） | — | **未开展**，无记录 | ❌ **未完成** |
| G4-6 | 探索性用户测试（匿名、交叉平衡） | — | **未开展**，无参与者、无问题修复记录 | ❌ **未完成** |
| G4-7 | 性能与成本指标（NFR-01） | — | **未采集** | ❌ **未完成** |
| G4-8 | 备份恢复演练（NFR-02） | — | **未执行** | ❌ **未完成** |
| G4-9 | 材料一致性校验 | 本目录各文件 | 本轮编制完成 8 份材料；相互引用同一组实测数字 | ⚠️ 进行中 |
| G4-10 | 演示视频（≤5 分钟） | `docs/demo.mp4` | **不存在，未录制** | ❌ **未完成** |
| G4-11 | 答辩预演（≤10 分钟） | `docs/defense.pptx`、`docs/demo_script.md` | PPT 与本轮脚本已生成；**现场预演未进行** | ⚠️ 部分 |
| G4-12 | 许可证核验 | `data/metadata/source_manifest.json:106-114` | StockQuant 许可证 **未核验**（`stockquant_license_verified=false`） | ❌ **未完成** |

**G4 结论**：**未通过**。可复现的测试与迁移有证据；干净环境、复核、用户测试、备份演练、视频全部缺失。

---

## G5 参赛提交核准

**退出条件**：资格、Q01—Q05、实际上传与回执核对完成。

| # | 证据项 | 真实结果 | 状态 |
|---|---|---|---|
| G5-1 | Q01 自建沙箱/第二客户端验收口径 | 无答复 | ❌ 未关闭 |
| G5-2 | Q02 强验证方式接受范围 | 无书面说明 | ❌ 未关闭 |
| G5-3 | Q03 AI 辅助与第三方披露要求 | 无答复 | ❌ 未关闭 |
| G5-4 | Q04 报名系统字段/格式/截止 | 未核验 | ❌ 未关闭 |
| G5-5 | Q05 学籍/赛道/唯一报名/权属 | 未核验 | ❌ 未关闭 |
| G5-6 | 报名与资格核验 | 无记录（行政档案不入代码库） | ❌ 无证据 |
| G5-7 | 实际上传与提交回执 | **无回执**；本轮材料不代表已提交 | ❌ 无证据 |
| G5-8 | 提交材料齐备性 | 作品简介/技术文档/安全自评/PPT/源码说明/证据索引/演示脚本/限制说明 = 8 份；**视频未录** | ⚠️ 部分 |

**G5 结论**：**未通过**。本索引不构成任何"已提交"或"已获认可"的声明；提交有效性必须由队长以原始回执核验。

---

## 附录 A：本轮实测命令与原始输出（汇总）

| 序 | 命令 | 输出摘要 |
|---|---|---|
| 1 | `pytest tests/contracts -q`（`TEST_DATABASE_URL=…/finpilot_test`） | `146 passed, 1 warning in 5.65s` |
| 2 | `pytest tests/security -q`（`…/finpilot_test_docs`） | `162 passed in 49.87s` |
| 3 | `pytest tests/contracts/test_provider_sandbox.py -q` | `26 passed, 1 warning in 5.62s` |
| 4 | `pytest tests/unit/domains/test_data_provenance.py -q` | `31 passed in 1.28s`（中间曾 `1 failed, 30 passed`） |
| 5 | `pytest tests/integration -q` | `50 passed, 1 warning in 54.74s`（首次 `1 failed, 44 passed`，因并发修改测试文件） |
| 6 | `pytest tests/unit/kernel/test_ledger.py tests/integration/test_authorization.py tests/unit/planner/test_dag.py -q` | `42 passed in 29.91s` |
| 7 | `pnpm -r test` | shared-ui `133` + im `5` + web `4` = **142 passed** |
| 8 | `python -c "…TestClient…/healthz"` | `status=ok, mode=mock, database ok (PostgreSQL 17.6), adapters: bank_local/merchant_local registered, bank_http unavailable: ModuleNotFoundError` |
| 9 | `python -c "…/openapi.json"` | 41 条路径，title 融策智能体 FinPilot API，version 1.0.0 |
| 10 | `python -c "…psycopg…pg_database"` | PostgreSQL 17.6；含 `finpilot` / `finpilot_dev_deploy` / `finpilot_test` / `finpilot_test_docs` / `finpilot_test_eval` 等（库数量随并行工作变动，编制时 14 → 16） |
| 11 | `python -c "…resource/libseccomp…"` | `sys.platform=win32`、`resource module: False`、`libseccomp: None` |
| 12 | `python -c "…import eval.oracle…"` | `ORACLE_VERSION=oracle-2026.10`；`judge_task` / `summarize_judgments` 可导入 |
| 13 | `python -m eval.runner --config FULL --split dev --limit 30 --repeats 1`（mock，库 `finpilot_test_eval`） | `exit code 0`；`completed 0 / failed 16 / refused 8 / partially_completed 4 / unknown 2`；`forbidden_effect_count 7`（duplicate_payment ×4、card_state_changed_unrequested ×3）；`mean_wall_ms 319.07`；30 条 `pending_review` |
| 14 | `python -m eval.runner --list-configs` | 返回配置数组（含 A1—A4 的显式开关与 `ablation_dimension`） |

## 附录 B：证据强度分级（防止过度解读）

| 级别 | 含义 | 本仓库对应 |
|---|---|---|
| E1 已实测且可复现 | 有命令、有原始输出、有环境记录 | 上表 1—12 全部 |
| E2 已实现但未在目标环境实测 | 源码在仓、测试通过，但目标平台（Linux/容器/在线模型）未验证 | 沙箱硬限额、容器策略、模型相关全部能力 |
| E3 仅有设计 | 文档写了，代码没有 | `offline_replay` 模式、`eval.runner/baselines/analysis/formal`、H4 实测 |
| E4 缺失 | 完全没有 | 用户测试、专家复核、视频、提交回执、许可证核验 |

**对外表述纪律**：E3/E4 项不得在作品简介、PPT 或答辩中以完成态陈述；E2 项必须带"本机未实测"限定。