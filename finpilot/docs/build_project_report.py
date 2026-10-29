"""报告第 9—12 章与附录，并完成装配。"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx.shared import Cm  # noqa: E402

from build_project_report_lib import (  # noqa: E402
    OUT,
    add_page_number_footer,
    bullets,
    caption,
    code_block,
    h1,
    h2,
    note_box,
    numbered,
    page_break,
    para,
    setup_document,
    table,
)
from build_project_report_part1 import chapter_1, chapter_2, chapter_3, contents, cover  # noqa: E402
from build_project_report_part2 import chapter_4, chapter_5, chapter_6, chapter_7, chapter_8  # noqa: E402


def chapter_9(doc):
    h1(doc, "9　部署、迁移与运维")

    h2(doc, "9.1 本机一键启动")
    code_block(
        doc,
        [
            "powershell -ExecutionPolicy Bypass -File scripts\\dev_up.ps1",
            "  → api 58000 / worker-outbox / worker-scheduler",
            "  → bank-sandbox 58101 / merchant-sandbox 58102",
            "  → RESULT: UP（实测 9.2 秒，5 个进程，3 个 health 端点均 200）",
            "powershell -ExecutionPolicy Bypass -File scripts\\dev_down.ps1",
            "  → RESULT: DOWN（停 5 个进程并释放端口；注意：同时会停掉本地 PostgreSQL）",
        ],
    )
    para(doc, "启动脚本用独立进程 + PID 文件 + close_fds 的方式拉起长驻服务，避免子进程继承父管道导致脚本挂死（这是本轮踩过的真实问题）。", space_after=6)

    h2(doc, "9.2 数据库迁移")
    table(
        doc,
        ["项", "结果"],
        [
            ("迁移文件", "migrations/versions/0001_initial.py（47 张业务表、86 个索引、45 个内联外键、18 个内联唯一约束）"),
            ("一致性", "alembic check → No new upgrade operations detected（与 models.py 零差异）"),
            ("可回滚", "downgrade base 仅剩 alembic_version；再 upgrade head 恢复 47 张表"),
            ("关键约束", "uq_operation_owner / uq_grant_use_operation / uq_daily_limit_bucket / uq_ledger_tx_operation_kind 均在"),
            ("锚点", "迁移文件头记录 models.py 的文件哈希，替代提交号（本机无 git）"),
        ],
        widths=[3.0, 13.0],
        font_size=8.8,
        first_col_bold=True,
    )

    h2(doc, "9.3 容器化配置（已备，未构建）")
    table(
        doc,
        ["文件", "内容", "状态"],
        [
            ("deploy/compose.yaml", "10 个服务：postgres、api、worker-outbox、worker-scheduler、银行沙箱、商户沙箱、客户端静态托管等", "本机无 Docker，未构建未运行"),
            ("deploy/Dockerfile", "非 root 用户、只读根文件系统、cap-drop ALL、健康检查", "同上；文件内自带“未构建”声明"),
            ("deploy/nginx-clients.conf", "两客户端静态托管与反向代理", "同上"),
            ("deploy/initdb/*.sql", "初始化测试库与角色", "同上"),
            ("deploy/README.md", "容器路径与本机路径两条部署说明 + 10 条上机验证清单", "已写"),
        ],
        widths=[3.6, 8.4, 4.0],
        font_size=8.6,
        first_col_bold=True,
    )

    h2(doc, "9.4 运维脚本")
    table(
        doc,
        ["脚本", "作用", "实测"],
        [
            ("deploy/verify_install.ps1", "干净环境自检：依赖、数据库、迁移是否最新、种子可重建、/healthz", "9/9 PASS"),
            ("deploy/backup_restore.py", "备份/恢复 + 审计摘要 + 账本守恒 + 逐表内容摘要 + 密钥扫描", "三项校验一致，密钥 0 命中"),
            ("scripts/seed.ps1", "播种（可指定库）", "幂等"),
            ("scripts/test_all.ps1", "分层跑测试（quick / integration / eval）", "quick 层可用"),
            ("scripts/worker_outbox.py / worker_scheduler.py", "常驻工作者入口", "随 dev_up 启动"),
        ],
        widths=[5.2, 7.4, 3.4],
        font_size=8.6,
        first_col_bold=True,
    )

    h2(doc, "9.5 环境相关的四个真实坑")
    numbered(
        doc,
        [
            "Alembic 用系统区域编码读 alembic.ini，中文注释会直接 UnicodeDecodeError → 配置文件保持纯 ASCII；所有 .ps1 同样只用 ASCII（自检脚本里有护栏）。",
            "种子装载器对子表曾非幂等 → 已按自然键改为幂等补齐，重复播种只补缺失。",
            "多个测试进程共用同一测试库会互相清表（会话级建表/截断）→ 各自使用独立测试库；评测运行器改用子进程 + 独立库。",
            "Start-Process 重定向输出会继承父管道，长驻子进程会让脚本永久挂住 → 改用独立进程 + PID 文件方案。",
        ],
    )
    page_break(doc)


def chapter_10(doc):
    h1(doc, "10　竞赛交付材料清单")
    table(
        doc,
        ["材料", "文件", "状态"],
        [
            ("技术文档", "docs/technical_document.docx（另存 .md）", "已生成，结构回读校验（404 段 / 49 标题 / 21 表）"),
            ("安全自评", "docs/security_self_assessment.docx", "已生成（138 段 / 38 标题 / 17 表）"),
            ("答辩 PPT", "docs/defense.pptx", "18 页，几何溢出检查 0"),
            ("作品简介", "docs/project_intro.txt", "534 字节（≤200 字要求内）"),
            ("证据索引", "docs/evidence_index.md", "G0—G5 逐项，未通过项如实标注"),
            ("限制说明", "docs/limitations.md（含附录 Z 复核更正）", "未验证项逐条"),
            ("官方条款映射", "docs/official_mapping.md", "OFF-1..6 与 OC01—OC27 对照，Q01—Q05 未关闭"),
            ("演示脚本", "docs/demo_script.md", "≤5 分钟分镜 + 10 分钟问答要点（未录制视频）"),
            ("需求追溯", "docs/requirement-trace.md", "92 项需求 → 工作包 → 测试族，实现状态列留空待填"),
            ("接口约定", "docs/INTERFACES.md", "冻结口径（签名以实现为准处已更正）"),
            ("验收记录", "docs/acceptance-live-e2e.md", "42 项运行态验收 + 本轮缺陷"),
            ("评测诊断", "docs/eval-oracle-diagnosis.md", "判定器误报的逐条证据与修复口径"),
            ("源代码与 README", "README.md、services/、tests/、deploy/", "已交付；根 LICENSE 缺失（已在 README 说明）"),
        ],
        widths=[3.2, 7.4, 5.4],
        font_size=8.6,
        first_col_bold=True,
    )
    note_box(doc, "缺口提示", "演示视频未录制；根目录没有 LICENSE 文件；报名与资格材料属团队行政事项，不在仓库内。", fill="EAF1FB")
    page_break(doc)


def chapter_11(doc):
    h1(doc, "11　已知限制与未验证项")
    para(doc, "以下每一条都对应仓库中的具体事实，不是“待优化”的泛泛表述。")
    table(
        doc,
        ["类别", "未验证项", "影响", "关闭条件"],
        [
            ("模型", "无在线模型调用代码；live 模式不实现，评测器拒绝运行", "所有评测数字只能代表确定性脚本，不能回答“换模型是否有效”", "接入模型客户端并跑 B0—B2/FULL 对照"),
            ("容器", "compose / Dockerfile / nginx 从未构建运行", "Linux 生产路径未经验证", "在具备 Docker 的机器上按 deploy/README 清单逐条验证"),
            ("沙箱", "Windows 无 resource 模块，CPU/内存/进程/写入硬限额未生效；无 seccomp", "沙箱隔离强度低于容器方案", "在 Linux 容器中启用 rlimit 与 seccomp 并复测"),
            ("覆盖率", "澄清/拒绝的未见表达覆盖不足（有具体任务 ID）", "部分澄清/拒绝任务被直接执行", "扩充意图规则并用隐藏集回归"),
            ("故障", "58 个故障变体中 8 类未实现注入", "故障恢复率分母不含这些类别", "补齐注入点并纳入运行器"),
            ("数据", "隐藏集与开发集同仓库，属团队内部隔离评测", "不构成第三方盲测", "引入外部保管与盲测流程"),
            ("许可证", "StockQuant 许可证待核验，未复制其代码", "第三方依赖披露不完整", "取得许可结论并登记"),
            ("人工", "用户研究、专家业务复核、评测签署均未开展；review_status=pending_review", "结论未经人工背书", "按 V5 要求组织复核并签署"),
            ("演示", "演示视频未录制；PPT 页数无法用本机工具核验（python-docx 不分页、无 LibreOffice）", "材料完整性差一项", "按分镜录制并核验时长"),
            ("行政", "Q01—Q05（模拟环境可接受性、强验证方式、AI 披露、提交格式、资格）均未关闭", "不能宣称“全部赛事条件已满足”", "取得组委会书面答复与回执"),
        ],
        widths=[1.8, 6.2, 5.2, 4.8],
        font_size=8.2,
        first_col_bold=True,
    )
    page_break(doc)


def chapter_12(doc):
    h1(doc, "12　复现指南")
    para(doc, "以下命令均在仓库根目录 G:\\比赛\\finpilot 下执行，顺序即真实执行顺序。")

    h2(doc, "12.1 冷启动（从零到可运行）")
    code_block(
        doc,
        [
            "# 1) Python 虚拟环境与依赖（本机使用内置 Python 3.12.14）",
            "python -m venv .venv",
            ".venv\\Scripts\\python.exe -m pip install -e .",
            "",
            "# 2) 配置（复制示例并按本机修改数据库连接）",
            "copy .env.example .env",
            "",
            "# 3) 数据库：建库 → 迁移 → 播种",
            "psql -h 127.0.0.1 -p 55432 -U postgres -c \"CREATE DATABASE finpilot OWNER finpilot;\"",
            ".venv\\Scripts\\python.exe -m alembic upgrade head",
            ".venv\\Scripts\\python.exe -m finpilot.seed --json data/synthetic/seed_app.json",
            "",
            "# 4) 启动本地服务栈（api + 两个 worker + 两个提供方沙箱）",
            "powershell -ExecutionPolicy Bypass -File scripts\\dev_up.ps1",
            "",
            "# 5) 验证",
            "curl http://127.0.0.1:58000/healthz",
            "start http://127.0.0.1:58000/app/     # APP 式客户端",
            "start http://127.0.0.1:58000/im/      # IM 风格客户端",
            "start http://127.0.0.1:58000/docs     # 交互式 API 文档",
        ],
    )

    h2(doc, "12.2 跑测试与评测")
    code_block(
        doc,
        [
            '$db = "postgresql+psycopg://finpilot:finpilot_dev_pw@127.0.0.1:55432/finpilot_test_core"',
            "$env:TEST_DATABASE_URL = $db",
            ".venv\\Scripts\\python.exe -m pytest tests -q           # 620 passed（约 6 分钟）",
            "",
            "# 分层运行",
            ".venv\\Scripts\\python.exe -m pytest tests/contracts -q          # 146",
            ".venv\\Scripts\\python.exe -m pytest tests/security -q           # 162",
            ".venv\\Scripts\\python.exe -m pytest tests/unit -q               # 256",
            ".venv\\Scripts\\python.exe -m pytest tests/integration -q        # 56",
            "",
            "# 评测（mock 模式；--mode live 会被拒绝）",
            ".venv\\Scripts\\python.exe -m eval.runner --config FULL --split dev --limit 30 --repeats 1",
        ],
    )

    h2(doc, "12.3 前端")
    code_block(
        doc,
        [
            "$env:PATH=\"<node bin>;$env:PATH\"",
            "pnpm install",
            "pnpm -r typecheck",
            "pnpm -r test        # 142 passed",
            "pnpm -r build       # 产物由 API 在 /app 与 /im 托管",
        ],
    )

    h2(doc, "12.4 预期结果对照")
    bullets(
        doc,
        [
            ("pytest tests -q", "：620 passed / 0 failed"),
            ("alembic check", "：No new upgrade operations detected（无漂移）"),
            ("deploy/verify_install.ps1", "：9/9 PASS"),
            ("scripts/dev_up.ps1", "：RESULT: UP（约 10 秒）"),
            ("live_e2e.py", "：PASS 42 / FAIL 0"),
            ("pnpm -r test", "：142 passed"),
        ],
    )


def appendix_a(doc):
    h1(doc, "附录 A　仓库目录结构")
    code_block(
        doc,
        [
            "finpilot/",
            "├─ README.md",
            "├─ pyproject.toml   alembic.ini",
            "├─ migrations/",
            "│  └─ versions/0001_initial.py",
            "├─ contracts/",
            "│  ├─ schemas/       （39 个 JSON Schema）",
            "│  ├─ examples/      （80 个正反例）",
            "│  ├─ openapi.yaml   tool-catalog.yaml",
            "│  └─ module-manifest.yaml",
            "├─ services/",
            "│  ├─ api/src/finpilot/",
            "│  │  ├─ gateway/",
            "│  │  ├─ intent/",
            "│  │  ├─ orchestration/",
            "│  │  ├─ kernel/",
            "│  │  ├─ security/",
            "│  │  ├─ domains/",
            "│  │  ├─ adapters/",
            "│  │  ├─ workers/",
            "│  │  └─ app.py  seed.py  models.py",
            "│  └─ provider-sandbox/",
            "├─ apps/",
            "│  ├─ shared-ui/   web/   im/",
            "├─ data/synthetic/",
            "├─ eval/",
            "│  ├─ runner.py  oracle.py  baselines.py",
            "│  ├─ analysis/   formal/",
            "│  ├─ data/       （dev / val / hidden）",
            "│  └─ results/",
            "├─ tests/",
            "│  ├─ contracts/  security/  unit/  integration/",
            "├─ deploy/",
            "├─ scripts/",
            "└─ docs/",
        ],
    )
    table(
        doc,
        ["目录", "内容"],
        [
            ("contracts/", "39 个 JSON Schema、OpenAPI（发布版 25 路径 26 操作）、36 项工具目录、37 个模块清单、80 个正反例"),
            ("services/api/", "网关、意图、编排、内核、安全、六场景领域、适配器、工作者与种子（82 个文件，22,085 行）"),
            ("services/provider-sandbox/", "独立的银行与商户服务（FastAPI + sqlite3，6 个文件，3,455 行，端口 58101 / 58102）"),
            ("apps/", "shared-ui（组件与 HTTP 客户端）、web（APP 式）、im（IM 式），81 个 .ts/.tsx，10,941 行"),
            ("data/synthetic/", "确定性生成器与冻结种子（2,737 条记录，dataset_hash=200ac215…ac66）"),
            ("eval/", "运行器、判定器、基线、统计、有界模型检查、数据与结果（11 个文件，5,656 行）"),
            ("tests/", "契约 146、安全 162、单元 256、集成 56，共 620 项"),
            ("deploy/ 与 scripts/", "compose、Dockerfile、nginx、备份恢复、环境自检、一键启停与 worker 入口"),
            ("docs/", "技术文档、安全自评、答辩 PPT、证据索引、限制、验收记录、需求追溯"),
        ],
        widths=[4.6, 11.6],
        font_size=8.6,
        first_col_bold=True,
    )


def appendix_b(doc):
    h1(doc, "附录 B　接口清单")
    para(doc, "运行时 OpenAPI 共 41 条路径；下表按用途分组列出主要端点（省略了仅内部使用的少量重复方法）。")
    table(
        doc,
        ["分组", "方法与路径", "说明"],
        [
            ("认证", "POST /v1/auth/sessions　POST /v1/auth/totp/enroll　POST /v1/auth/totp/confirm　POST /v1/auth/logout　GET /v1/auth/me", "会话与强验证因子；人类专用"),
            ("任务", "POST /v1/tasks　GET /v1/tasks　GET /v1/tasks/{id}　POST /v1/tasks/{id}/messages　POST /v1/tasks/{id}/cancel　POST /v1/tasks/{id}/resume　POST /v1/tasks/{id}/repair", "创建、追问、改意、取消、恢复、增量重规划"),
            ("确认与验证", "POST /v1/plans/{id}/confirm　POST /v1/auth/challenges/{id}/verify", "人类确认与强验证；模型无权"),
            ("授权", "POST /v1/grants/{id}/revoke", "撤销：返回被停止步骤与在途操作"),
            ("操作与回执", "GET /v1/operations　GET /v1/operations/{id}　GET /v1/receipts/{id}", "权威状态查询，按归属隔离"),
            ("账户与联系人", "GET /v1/accounts　GET /v1/payees", "快照含 B/H/F/E 与日累计"),
            ("账单", "GET /v1/bills/report　POST /v1/bills/corrections", "月/年/自定义账期；分类纠正不改原流水"),
            ("卡片", "GET /v1/cards　GET /operator/card-applications", "卡状态、卡控、申请（审批走运营端点）"),
            ("理财", "GET /v1/wealth/products　GET /v1/wealth/comparison　GET /v1/wealth/orders", "产品、比较、订单与持仓"),
            ("订阅", "GET /v1/subscriptions", "合同与代扣分列；带 requires_confirmation"),
            ("AA", "GET /v1/aa", "分摊结果、到账状态、调整记录"),
            ("定时", "GET /v1/schedules", "任务状态、下次执行、窗口"),
            ("商户", "GET /v1/catalog/merchants", "报价与库存；自由文本单列 untrusted_text"),
            ("运营", "GET /operator/cases　POST /operator/cases/{id}/actions　GET /operator/cases/{id}/handoff", "接管与复核；独立角色"),
            ("隐私", "GET /v1/privacy/export　POST /v1/privacy/delete　GET /v1/privacy/retention　GET /v1/privacy/deletion-requests", "导出、删除、留存"),
            ("回调", "POST /internal/provider-events/{provider}", "验签、去重、版本核验"),
            ("元数据", "GET /healthz　GET /v1/meta/tools　GET /v1/meta/policy", "健康、工具目录、策略阈值"),
        ],
        widths=[2.4, 9.6, 4.0],
        font_size=7.8,
        first_col_bold=True,
    )


def appendix_c(doc):
    h1(doc, "附录 C　状态枚举与错误码")
    h2(doc, "C.1 主要状态机")
    table(
        doc,
        ["对象", "状态取值"],
        [
            ("任务", "draft / clarifying / planning / awaiting_authorization / ready / running / reconciling / replanning / awaiting_human / succeeded / partially_succeeded / failed / cancelled / blocked"),
            ("操作", "created / awaiting_authorization / authorized / dispatching / pending / unknown / succeeded / failed / cancelled"),
            ("授权", "pending / active / consumed / revoked / expired"),
            ("预留", "active / partially_captured / captured / released / expired"),
            ("调度任务", "scheduled / leased / running / completed / awaiting_authorization / missed / failed / cancelled"),
            ("理财订单", "submitted / accepted / awaiting_confirmation / confirmed / rejected / redeeming / settled / failed / cancelled"),
            ("卡片申请", "submitted / under_review / approved / rejected / withdrawn"),
            ("卡生命周期", "inactive / active / lost / closed"),
        ],
        widths=[2.6, 13.4],
        font_size=8.4,
        first_col_bold=True,
    )
    h2(doc, "C.2 错误码分类（共 42 个，按语义分组举例）")
    table(
        doc,
        ["类别", "代表错误码", "HTTP 语义", "重试方式"],
        [
            ("参数与结构", "PARAM_INVALID / AMOUNT_PRECISION / SCHEMA_INVALID / EXTRA_DANGEROUS_FIELD / UNKNOWN_TOOL", "400", "修正后重试（never / after_input_fix）"),
            ("身份与权限", "UNAUTHENTICATED / FORBIDDEN / OBJECT_NOT_ACCESSIBLE / POLICY_DENIED", "401 / 403", "never 或重新登录"),
            ("授权与验证", "GRANT_REQUIRED / GRANT_INVALID / GRANT_SCOPE_VIOLATION / CHALLENGE_EXPIRED / CHALLENGE_FAILED / CHALLENGE_LOCKED / FACTOR_NOT_ENROLLED", "403 / 429", "重新确认或稍后重试"),
            ("并发与资金", "VERSION_CONFLICT / IDEMPOTENCY_CONFLICT / INSUFFICIENT_FUNDS / PROTECTED_FLOOR_VIOLATION / RESERVATION_EXCEEDED", "409 / 422", "刷新或修改参数"),
            ("业务与条款", "RISK_MISMATCH / ASSESSMENT_REQUIRED / INFEASIBLE / PLANNING_LIMIT / CAPABILITY_NOT_SUPPORTED / NOT_REVERSIBLE", "409 / 422", "after_input_fix"),
            ("外部与未知", "PROVIDER_TIMEOUT / PROVIDER_UNAVAILABLE / PROVIDER_REJECTED / RESULT_UNKNOWN / PROVIDER_CAPABILITY_MISSING", "422 / 503", "query_same_operation 或 retry_same_operation"),
            ("安全与系统", "SANDBOX_UNAVAILABLE / SANDBOX_VIOLATION / AUDIT_UNAVAILABLE / CIRCUIT_OPEN / SAFETY_LOCKED / RATE_LIMITED / SYSTEM_ERROR", "422 / 429 / 500 / 503", "after_backoff 或 never"),
        ],
        widths=[2.4, 8.2, 2.2, 3.2],
        font_size=8.0,
        first_col_bold=True,
    )


def appendix_d(doc):
    h1(doc, "附录 D　需求与验收追溯摘要")
    h2(doc, "D.1 需求分配（与四人分工计划一致）")
    table(
        doc,
        ["角色", "职责", "需求项数", "主要工作包"],
        [
            ("A", "交互、目标理解、卡片与用户闭环", "22", "A01—A06（双客户端、澄清、确认、卡片、改意接管、体验与材料）"),
            ("B", "交易内核、状态一致性、部署", "26", "B01—B06（身份账本、授权并发、操作与对账、定时、理财订单、适配器与部署）"),
            ("C", "金融规则、账单、订阅、AA、统计", "22", "C01—C06（数据来源、账单、理财规则与量化、订阅、商户与 AA、统计与复核）"),
            ("D", "规划、策略、恢复、沙箱、评测", "22", "D01—D06（DAG、运行时策略、增量恢复、信任边界、评测、红队与门禁）"),
            ("合计", "—", "92", "24 个工作包"),
        ],
        widths=[1.6, 6.2, 2.0, 6.2],
        font_size=8.6,
        first_col_bold=True,
        align_center_cols=(2,),
    )

    h2(doc, "D.2 本报告的验证覆盖")
    table(
        doc,
        ["层级", "状态", "依据"],
        [
            ("设计覆盖（92 项需求均有对应设计）", "已完成", "docs/requirement-trace.md（92 行，主责与测试族已交叉核对）"),
            ("实现覆盖（代码存在且可运行）", "已完成", "仓库代码 + 620 项测试 + 运行态 42 项验收"),
            ("测试证据（T01—T60 对应测试族）", "部分完成", "多数测试族有对应用例；故障注入缺 8 类，隐藏集未调分"),
            ("外部依赖（Q01—Q05）", "未关闭", "docs/official_mapping.md 逐条标注"),
            ("行政资格（学籍、组队、唯一报名、提交回执）", "未开展", "属团队行政事项，不在仓库内"),
        ],
        widths=[6.2, 2.2, 7.6],
        font_size=8.6,
        first_col_bold=True,
        align_center_cols=(1,),
    )
    note_box(
        doc,
        "结论口径",
        "可以表述为“具备完整实现基线，六场景与安全机制在本机实测可运行”。不可表述为“已通过赛事验收”"
        "“全部条件已满足”或“在测评中优于某方案”——那需要外部核验、盲测与统计结论支撑。",
    )


def build() -> Path:
    doc = setup_document()
    add_page_number_footer(doc, "融策智能体 FinPilot · 项目详细说明与功能报告")
    cover(doc)
    contents(doc)
    chapter_1(doc)
    chapter_2(doc)
    chapter_3(doc)
    chapter_4(doc)
    chapter_5(doc)
    chapter_6(doc)
    chapter_7(doc)
    chapter_8(doc)
    chapter_9(doc)
    chapter_10(doc)
    chapter_11(doc)
    chapter_12(doc)
    appendix_a(doc)
    appendix_b(doc)
    appendix_c(doc)
    appendix_d(doc)
    doc.save(OUT)
    return OUT


if __name__ == "__main__":
    path = build()
    print(f"saved: {path}")
    print(f"bytes: {path.stat().st_size}")
    from build_project_report_lib import WIDE_LINES

    if WIDE_LINES:
        print(f"WARN: {len(WIDE_LINES)} 行代码块可能超出正文宽度（会折行）：")
        for width, line in WIDE_LINES:
            print(f"  [{width}] {line}")
    else:
        print("OK: 所有代码块行宽均在正文宽度内")