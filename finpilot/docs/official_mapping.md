> **整合版本提示（2026-10-29）：以下为原交付历史快照，部分模式、接口和测试结论已变化。请先阅读整合目录根 README、docs/INTEGRATION_PROVENANCE.md 和最新审查报告；live 已新增受限模型候选接入，offline_replay 仍未实现；具体验证边界见根目录 README 和 docs/QWEN_LIVE_INTEGRATION_2026-10-29.md。**

# 官方条款到本项目实现／测试／证据的对照表

融策智能体（FinPilot）· 2026 深圳国际金融科技大赛

> 官方条款来源为 `docs/BANKAGENT_SRS_V5_COMPETITION.md` §1.2（OFF-1—OFF-6）与 §22.2（OC01—OC27）。
> **纪律**：本表只写"已实测""已实现未实测""设计覆盖""未完成"四种状态之一，**"设计覆盖"不等于通过**。
> 时点：2026-10-27 17:28—18:05（Asia/Shanghai）。

---

## 1. OFF-1—OFF-6：官方来源与材料对照

| 编号 | 官方来源 | 要求内容 | 本项目实现／产物 | 证据 | 状态 |
|---|---|---|---|---|---|
| **OFF-1** | 赛事官网国内人工智能赛题及"赛题解析" | 六场景、权限等级、安全、规划和材料说明 | 六场景领域实现（`domains/*`）；GREEN/YELLOW/RED 策略（`security/policy.py`）；规划器与 DAG（`orchestration/*`）；材料见本目录 | `docs/BANKAGENT_SRS_V5_COMPETITION.md:27`；`GET /v1/meta/tools`、`/v1/meta/policy` | ✅ 已实现；**六场景端到端人工演示未采集 KPI** |
| **OFF-2** | 参赛规则（国内赛道、人工智能赛道） | 资格、评分、提交与决赛流程 | 评分口径映射到 OC20—OC22（见 §3） | 同上 `:28` | ⚠️ 规则已登记；**资格与提交未核验（G5）** |
| **OFF-3** | 技术文档模板 `.docx` | 架构、算法、安全、部署和测试的提交结构 | `docs/technical_document.docx`（本轮生成，源 `docs/technical_document.md`） | 本目录；模板**正文未归档**（仅链接） | ✅ 已产出；⚠️ 模板原文未存档比对 |
| **OFF-4** | 安全自评模板 `.docx` | 权限、攻击测试、数据隐私、残余风险 | `docs/security_self_assessment.docx`（本轮生成，源 `docs/security_self_assessment.md`） | 本目录 | ✅ 已产出；⚠️ 模板原文未存档比对 |
| **OFF-5** | 源码 README 模板 `.md` | 环境、运行、测试和仓库说明 | `README.md`（**由他人负责，本轮未核验**）；运行命令见 `docs/technical_document.md` §8.2 | — | ❌ **本轮未核验** |
| **OFF-6** | 视频说明 `.docx` | 国内视频可选、时长不超过五分钟 | `docs/demo_script.md`（分镜脚本） | — | ❌ **视频未录制**（`docs/demo.mp4` 不存在） |

### 1.1 已知官方差异（保留登记，不自行消解）

`docs/BANKAGENT_SRS_V5_COMPETITION.md:36` 记录：OFF-2 将安全自评列为**必交**，OFF-4 首页写为**可选**；OFF-1 把演示视频列入作品资料，OFF-2 与 OFF-6 明确国内视频**可选**。本项目按"安全自评必交"准备，并曾计划主动交付视频；**该差异未取得官方书面澄清**。

---

## 2. OC01—OC27：官方条款到需求、测试、证据

状态图例：✅ 已实测 ｜ 🟡 已实现未实测／部分 ｜ 📐 设计覆盖（无实跑） ｜ ❌ 未完成

| 编号 | 官方条款 | 需求 ID | 验收测试族 | 本项目实现 | 本轮可指向的证据 | 状态 |
|---|---|---|---|---|---|---|
| **OC01** | 自然语言理解、规划并执行 | PLAN-01、PLAN-02、STATE-04 | T01、T02、T41 | `intent/compiler.py`（GoalSpec 分区与澄清）、`orchestration/planner.py` | `tests/integration/test_api_flow.py:148-175`（澄清→回答）；`tests/integration` **50 passed** | 🟡 规则编译器已实测；模型侧未实测 |
| **OC02** | APP、IM 等渠道 | SYS-01、NFR-03 | T32、T47 | `apps/web`（APP 式）、`apps/im`（IM 风格）、`apps/shared-ui` | `pnpm -r test` → **142 passed**（133+5+4） | ✅ 组件级已实测；⚠️ **渠道接受度 Q01 未关闭** |
| **OC03** | 至少三场景，鼓励全覆盖 | S1-01…S6-01 | T03、T06、T10、T12、T14、T24 | 六个领域包 `domains/{aa,billing,wealth_rules,cards,subscriptions}` + 跨场景编排 | `tests/unit/domains/*`（随套件通过）；**未逐场景单独统计** | 🟡 六场景实现与用例在仓；S6 跨场景未端到端验证 |
| **OC04** | 姓名／手机号／备注转账、定时、AA | S1-01、S1-03、S1-04 | T03、T04、T05 | `kernel/transfers.py`、`kernel/scheduled_jobs.py`、`domains/aa/service.py` | `GET /v1/schedules`、`GET /v1/aa`；`tests/unit/domains/test_aa.py` | 🟡 已实现；未逐条手工演示 |
| **OC05** | 分类、异常、月报／年报 | S2-01、S2-02、S2-03 | T06、T07、T08 | `domains/billing/{service,classification,anomalies}.py` | `GET /v1/bills/report`、`POST /v1/bills/corrections`；`tests/unit/domains/test_billing.py` | 🟡 已实现 |
| **OC06** | 推荐比较、测评、申购／赎回 | S3-01…S3-04 | T09、T10、T11 | `domains/wealth_rules/*`、`adapters/stockquant/` | `GET /v1/wealth/{products,comparison,orders}`；`tests/unit/domains/{test_quant,test_suitability}.py` | 🟡 已实现；**H4 未实测** |
| **OC07** | 申请、额度、限制／解锁、挂失 | S4-01…S4-04 | T12、T13 | `domains/cards/*`、`security/card_controls.py`、`gateway/operator.py` | `GET /v1/cards`、`GET /operator/card-applications`；`tests/unit/domains/test_cards.py` | 🟡 已实现 |
| **OC08** | 订阅识别、续费提醒、取消 | S5-01…S5-03 | T14、T15 | `domains/subscriptions/{service,mandates,reminders}.py` | `GET /v1/subscriptions`；`tests/integration/test_subscription_cancellation.py` | 🟡 已实现 |
| **OC09** | 生日、活期预留、提前订鲜花蛋糕 | S6-01…S6-03、AUTH-07 | T05、T24、T25、T30 | `orchestration/{planner,recovery}.py`、`kernel/{reservations,limits}.py`、商户沙箱报价 | `tests/contracts/test_provider_sandbox.py` **26 passed**（缺货/变价/取消费/不可退） | 🟡 机制已测；**跨场景端到端演示未做** |
| **OC10** | GREEN 无二次确认 | AUTH-01、AUTH-02 | T16、T47 | `security/policy.py`（14 项 GREEN 工具） | `GET /v1/meta/policy` 返回阈值与说明 | ✅ 策略与契约已测 |
| **OC11** | YELLOW 详情及确认，含订阅查询 | AUTH-01、AUTH-03、S5-01 | T14、T16 | `subscription.list` 显式 YELLOW 例外（`policy.py:48`）；日累计阈值 100000 分 | `tests/integration/test_api_flow.py:54-99`（YELLOW 转账端到端） | ✅ 已实测 |
| **OC12** | RED 强验证及示例 | AUTH-02、AUTH-05 | T17、T35 | `kernel/verifier.py`（TTL/次数/锁定/防重放/一次性）+ RFC 6238 TOTP | `tests/integration/test_api_flow.py:114-145`（错误码→正确 TOTP→只扣一次）；`tests/security` **162 passed** | ✅ 机制已实测；⚠️ **验证方式的接受范围 Q02 未关闭** |
| **OC13** | 不编造交易／账户 | STATE-02、STATE-04、API-03 | T21、T40 | `state_machine.py`（`unknown` 不是失败）、只按权威状态判分 | `kernel/state_machine.py:39-49`；`eval/oracle.py` 设计原则 1 | 🟡 已实现；**未做端到端"超时→unknown→对账"人工演示** |
| **OC14** | Prompt 注入与越权防御 | SYS-02、SEC-01、SEC-04 | T20、T34、T35、T39 | `security/trust_boundary.py`（32 条攻击语料 + 脱敏 + 工具结果隔离） | `tests/security` **162 passed**；`eval/data/attack_catalog.json` 200 条（12 类） | ✅ 机制级已实测；❌ **对真实模型未实测** |
| **OC15** | 完整决策与执行日志 | SEC-05 | T36 | `security/audit.py`（哈希链 + 独立签名摘要 + 失败关闭） | `tests/security/test_audit.py` 18 例；`GET /v1/operations/{id}` | ✅ 已实测；⚠️ 抵抗"持密钥管理员"的边界已披露 |
| **OC16** | 连续失败／可疑行为熔断 | SEC-03 | T38 | 熔断阈值 `CIRCUIT_BREAKER_FAILURE_THRESHOLD=3`、安全锁 `SAFETY_LOCK_REFUSAL_THRESHOLD=5/60s`、`lock_out()` | `config.py:47-49`；`kernel/verifier.py:185-195` | 🟡 已实现；**未做端到端熔断演练** |
| **OC17** | 生成代码隔离、受限和可监控 | SEC-02 | T37 | `security/sandbox.py`（AST + 独立子进程 + audit hook + 墙钟/输出）；不可用即关闭 | `tests/security/test_sandbox.py` 26 例；`deploy/sandbox-policy.yaml` | 🟡 部分实测；❌ **硬限额与容器未生效/未实测** |
| **OC18** | 模糊、省略、隐含意图 | PLAN-01、PLAN-07 | T01、T41 | `intent/compiler.py`（`unresolved` 必提问、`UNSUPPORTED_PATTERNS` 兜底） | `compiler.py:42-50`；`tests/integration/test_api_flow.py:148-155`（同名联系人先澄清） | ✅ 已实测（规则编译器） |
| **OC19** | 有序 DAG、中断、回退和接管 | PLAN-03、PLAN-05、PLAN-06、S6-05 | T24、T28、T29、T31、T45 | `orchestration/{dag,recovery,handoff}.py` | `tests/unit/planner/test_dag.py`（含在 42 passed）；`POST /v1/tasks/{id}/repair`、`/resume`、`/cancel`、`/operator/cases/*` | 🟡 已实现并测；**跨场景动态故障未人工演示** |
| **OC20** | 完成率 40% | EVAL-01、EVAL-03、EVAL-04 | T49、T53 | 数据集冻结 + 判定器 `eval/oracle.py` + 运行器 `eval/runner.py` | 600 任务 / 300-120-180 拆分；本机实跑 dev 30 条：**completed 0 / failed 16 / refused 8 / partially_completed 4 / unknown 2**，禁止效果 7 条，全部未复核 | ❌ **未通过**（不得引用为完成率） |
| **OC21** | 安全合规 30% | SEC-06、AUTH-04、MONEY-03 | T18、T19、T34、T59 | 本自评报告 + 关键不变量测试 | `tests/security` **162 passed**；内核组合 **42 passed** | ✅ 内部证据充分；❌ 外部合规未核验 |
| **OC22** | 创新及交互 30% | PLAN-05、QUANT-03、EVAL-02、USER-02 | T44、T46、T53、T56 | 增量恢复实现、H4 消融开关、`eval/baselines` 声明 | `recovery.py`；`risk_report.py:630`；`compare.py:223,294` | 📐 **消融与用户实验均未实测** |
| **OC23** | 独立、真实、合法及知识产权 | GOV-02 | T51、T59 | `data/metadata/source_manifest.json` 13 条来源 + AI/第三方披露 | `tests/unit/domains/test_data_provenance.py` **31 passed**（强制断言 StockQuant 许可证为"待核验"） | 🟡 数据来源已披露；❌ **许可证未核验（Q03/Q05）** |
| **OC24** | 全日制在校、2—5 人、一人一队 | 第 1.3 节、G5 | T59 | — | 无记录（行政档案不入代码库） | ❌ **未核验（Q05）** |
| **OC25** | 200 字简介、技术／PPT／源码／安全自评 | DEL-01、DEL-02 | T59 | `docs/project_intro.txt`、`docs/technical_document.docx`、`docs/defense.pptx`、`docs/security_self_assessment.docx`、`README.md`（他人负责） | 本目录 | 🟡 本轮产出 4 份；⚠️ **Q04 未关闭** |
| **OC26** | 国内视频可选 ≤5 分钟 | DEL-03 | T59 | `docs/demo_script.md` | — | ❌ **视频未录制** |
| **OC27** | 初赛提交与后续答辩 | DEL-02 | T59 | 交付材料与答辩要点 | `docs/defense.pptx`、`docs/demo_script.md` | ❌ **无上传与回执（Q04/Q05）** |

### 2.1 OC 状态汇总

| 状态 | 数量 | 编号 |
|---|---|---|
| ✅ 已实测 | 5 | OC02（组件级）、OC10、OC11、OC12（机制）、OC18、OC21（内部） |
| 🟡 已实现 / 部分实测 | 14 | OC01、OC03—OC09、OC13、OC15—OC17、OC19、OC23、OC25 |
| 📐 设计覆盖，无实跑 | 1 | OC22 |
| ❌ 未完成 / 无证据 | 6 | OC14（模型侧）、OC20、OC24、OC26、OC27、（OC02 的 Q01） |

> 计数口径说明：上表按主状态归类，OC02 计入"已实测"但其渠道接受度依赖 Q01；OC14 计入"未完成"的是**模型侧**验证，机制级验证已通过（见 §2 行内说明）。合计 27 条（OC01—OC27）。

---

## 3. 官方评分口径（OFF-2）与证据准备

| 维度 | 权重（官网） | 本项目对应证据 | 现状 |
|---|---|---|---|
| 任务完成率 | 40% | 冻结数据集 600 + 判定器 + 运行器 | ❌ **已跑 dev 30 条小样本：completed 0，7 条禁止效果（未通过，根因未定位）** |
| 安全合规 | 30% | `docs/security_self_assessment.docx` + 162 条安全用例 + 42 条内核用例 | 🟡 内部证据充分；外部合规未核验；端到端小样本出现 `effect_duplicate_payment` ×4 |
| 创新及交互 | 30% | 增量恢复、H4 设计、双客户端 | 🟡 双客户端已测；消融与用户实验未做 |

---

## 4. 外部问题清单 Q01—Q05 状态

| 编号 | 问题 | 计划关闭方式（SRS 原文口径） | 当前状态 | 本项目已做的准备 | 未做的部分 |
|---|---|---|---|---|---|
| **Q01** | 自建银行、商户沙箱与第二客户端是否满足国内验收；是否有指定 API/测试平台 | 用组委会公开说明或书面答复关闭 | ❌ **未关闭** | 已在仓提供两个独立客户端与两个独立提供方沙箱（58101/58102）+ 26 条契约用例 | 无组委会答复；沙箱服务本机未启动 |
| **Q02** | 强验证具体实现可接受范围 | 取得接受说明，必要时换为官方指定方式 | ❌ **未关闭** | 已实现 scrypt + 真实 RFC 6238 TOTP，预留适配位（`identity.py` 可替换） | **无书面说明，不得宣称已获认可** |
| **Q03** | AI 辅助开发、第三方代码与模型使用披露要求 | 保存 AI 使用及来源清单、许可证、人工复核记录 | ❌ **未关闭** | `source_manifest.json` 13 条来源、`stockquant_license_verified=false` 由测试强制 | 无官方答复；无人工复核记录；许可证未取得 |
| **Q04** | 报名系统最终字段、文件格式、大小、截止时刻与模板冲突 | 以当前系统和官方通知核验 | ❌ **未关闭** | 材料按 200 字简介 + 4 份文档组织 | 未核验系统字段/格式/大小/截止；**未继承旧版未核实的 50MB/300MB/500MB 限制** |
| **Q05** | 团队学籍、赛道、唯一报名、作品权属及提交有效性 | 由队长核对原始证明及成功提交回执 | ❌ **未关闭** | 审查报告只记录状态，不收集多余身份证明 | 无资格核验、无提交回执 |

### 4.1 纪律

Q01—Q05 属**外部或行政依赖**，**不能由内部测试替代**。在未关闭前：

- 不得对外宣称"已验证方式获赛事认可"（Q02）；
- 不得宣称"已完成知识产权核验"（Q03）；
- 不得宣称"已提交/材料合规"（Q04/Q05）；
- `docs/evidence_index.md` 的 G5 记为**未通过**。

---

## 5. 交付材料与本表的关系

| 材料 | 文件 | 与本表的接口 |
|---|---|---|
| 作品简介（≤200 字） | `docs/project_intro.txt` | 只使用本表中 ✅/🟡 且带限定的表述 |
| 技术文档 | `docs/technical_document.md` / `.docx` | 承载 OFF-3 结构；引用 OC01—OC19 的实现与测试 |
| 安全自评 | `docs/security_self_assessment.md` / `.docx` | 承载 OFF-4 结构；对应 OC14—OC17、OC21 |
| 答辩 PPT | `docs/defense.pptx` | 对应 OC22、OC25、OC26；必答七问见 `docs/demo_script.md` |
| 证据索引 | `docs/evidence_index.md` | G0—G5 逐门槛证据；附录 B 给证据强度分级 |
| 限制说明 | `docs/limitations.md` | L-01—L-18；对外表述自查清单 |
| 演示脚本 | `docs/demo_script.md` | OFF-6 / OC26 的分镜与问答 |