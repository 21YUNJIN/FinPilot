"""构建《FinPilot 融策智能体 · 项目详细说明与功能报告》."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

from build_project_report_lib import (  # noqa: E402
    ACCENT,
    CN_HEAD,
    MUTED,
    OUT,
    _set_run_font,
    _shade,
    add_page_number_footer,
    bullets,
    caption,
    code_block,
    h1,
    h2,
    h3,
    note_box,
    numbered,
    page_break,
    para,
    rich,
    setup_document,
    table,
)

TODAY = "2026-10-27"


# ==========================================================================
# 封面
# ==========================================================================
def cover(doc):
    for _ in range(4):
        doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("融策智能体 FinPilot")
    _set_run_font(run, name=CN_HEAD, size=30, bold=True, color=ACCENT)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("项目详细说明与功能报告")
    _set_run_font(run, name=CN_HEAD, size=19, bold=True, color=RGBColor(0x33, 0x33, 0x33))

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6)
    run = p.add_run("个人银行智能体的可信执行参考实现 · 六场景 · 双客户端")
    _set_run_font(run, size=11.5, color=MUTED)

    for _ in range(3):
        doc.add_paragraph()

    info = [
        ("文档编号", "FP-REPORT-001"),
        ("文档性质", "项目说明与功能说明（描述已交付实现，不是需求规格）"),
        ("依据规格", "BANKAGENT_SRS_V5_COMPETITION.md（FP-SRS-005）"),
        ("交付日期", TODAY),
        ("代码位置", r"G:\比赛\finpilot"),
        ("验证状态", "本机实测：整仓 620 项测试通过；运行态 HTTP 验收 42/42 通过"),
        ("运行模式", "mock（确定性脚本智能体）· 虚拟资金 · 模拟渠道"),
        ("编写方式", "由集成方依据仓库实测结果生成，未经人工签署"),
    ]
    table(doc, ["项目", "内容"], info, widths=[3.6, 11.6], font_size=9.5, first_col_bold=True)

    for _ in range(2):
        doc.add_paragraph()
    note_box(
        doc,
        "阅读提示",
        "本报告只描述仓库中真实存在并已在本机验证的内容。凡是未实测的能力（在线大模型、容器部署、"
        "真实银行接口、人工业务复核、演示视频）均在正文明确标注为“未验证”，请勿据此推断其已具备。",
        fill="EAF1FB",
    )
    page_break(doc)


# ==========================================================================
# 目录
# ==========================================================================
def contents(doc):
    h1(doc, "目录")
    items = [
        ("1", "项目概述与交付边界"),
        ("2", "总体架构与信任边界"),
        ("3", "核心机制说明"),
        ("4", "六大场景功能报告"),
        ("5", "双客户端功能说明"),
        ("6", "契约体系"),
        ("7", "数据资产与评测框架"),
        ("8", "测试与验证证据"),
        ("9", "部署、迁移与运维"),
        ("10", "竞赛交付材料清单"),
        ("11", "已知限制与未验证项"),
        ("12", "复现指南"),
        ("附录 A", "仓库目录结构"),
        ("附录 B", "接口清单"),
        ("附录 C", "状态枚举与错误码"),
        ("附录 D", "需求与验收追溯摘要"),
    ]
    for number, title in items:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(5)
        p.paragraph_format.left_indent = Cm(0.6 if len(number) <= 2 else 0.6)
        p.paragraph_format.first_line_indent = Cm(-0.6)
        r1 = p.add_run(f"{number}　")
        _set_run_font(r1, size=10.5, bold=True, color=ACCENT)
        r2 = p.add_run(title)
        _set_run_font(r2, size=10.5)
    page_break(doc)


# ==========================================================================
# 第 1 章
# ==========================================================================
def chapter_1(doc):
    h1(doc, "1　项目概述与交付边界")

    h2(doc, "1.1 背景")
    para(
        doc,
        "本项目面向 2026 深圳国际金融科技大赛国内人工智能赛道，题目要求以自然语言完成个人银行场景中的"
        "查询与分析、资金操作、卡片管理、订阅管理与跨场景组合任务，并在用户改意、报价变化、工具故障与"
        "恶意输入等条件下仍保持授权有效与资金一致。",
    )
    para(
        doc,
        "交付前，工作目录中只有规划类文档：总计划、四份个人任务书、完整需求规格与一份审核报告。README 明确写着"
        "“本次没有创建银行应用”“代码目录、接口和测试命令是未来实现约定”。本报告描述的，是把这些文档落成"
        "可运行系统的结果。",
    )

    h2(doc, "1.2 目标与范围")
    table(
        doc,
        ["维度", "本项目覆盖", "不在本项目范围"],
        [
            ("业务场景", "转账（含定时、AA）、账单分析、理财、卡片、订阅代扣、跨场景组合共六大场景", "真实开户、信贷审批、证券交易、跨币种支付、真实商户采购"),
            ("资金", "模拟银行内的双重记账、资金预留、退款与补偿，虚拟资金", "真实银行账户扣款与清算"),
            ("渠道", "APP 式 Web 客户端、IM 风格客户端（同一后端、同一账号体系）", "接入真实微信、真实银行 APP"),
            ("智能", "确定性目标编译 + 有界 DAG 规划 + 服务端策略校验", "在线大模型调用（仓库无模型客户端，未实现）"),
            ("环境", "本机 Windows 11 + PostgreSQL 17.6，可一键启动与复现", "Linux 容器生产部署（配置已备，未构建运行）"),
        ],
        widths=[2.4, 7.4, 6.2],
        font_size=9,
        first_col_bold=True,
    )
    note_box(
        doc,
        "边界口径",
        "“已实现并验证”= 本机跑通；“已实现未验证”= 代码在、环境不具备；“未实现”= 明确不在范围。"
        "本报告在每一处都标注属于哪一种，禁止把前两类混为一谈。",
    )

    h2(doc, "1.3 交付形态与技术栈")
    table(
        doc,
        ["项", "选型", "本机版本"],
        [
            ("后端语言/框架", "Python 3.12 + FastAPI", "3.12.14 / FastAPI 0.162.0"),
            ("数据访问", "SQLAlchemy 2.x（同步事务）+ Alembic 迁移", "2.1.6 / 1.20.1"),
            ("数据库", "PostgreSQL（行锁、advisory lock、SKIP LOCKED）", "17.6，127.0.0.1:55432"),
            ("驱动", "psycopg 3", "3.3.6"),
            ("客户端", "React 18 + TypeScript 5 + Vite 5", "Node v24.21.0 / pnpm 11.28.5"),
            ("前端测试", "vitest + @testing-library/react + jsdom", "142 项通过"),
            ("提供方沙箱", "FastAPI + 标准库 sqlite3（独立进程、独立状态库）", "端口 58101 / 58102"),
            ("文档与材料", "python-docx / python-pptx 生成 docx/pptx", "python-docx 1.2.0"),
        ],
        widths=[3.0, 7.2, 5.8],
        font_size=9,
        first_col_bold=True,
    )

    h2(doc, "1.4 代码规模（实测统计）")
    table(
        doc,
        ["部分", "文件数", "代码行数", "说明"],
        [
            ("后端服务（services/api/src/finpilot）", "82", "22,085", "网关、意图、内核、编排、安全、六场景领域、适配器"),
            ("提供方沙箱（services/provider-sandbox）", "6", "3,455", "银行/商户两个独立进程与状态库"),
            ("测试（tests）", "29", "11,584", "契约、单元、集成、安全四层"),
            ("评测（eval）", "11", "5,656", "运行器、判定器、基线、统计、有界模型检查"),
            ("数据生成（data/synthetic）", "4", "5,052", "确定性生成 600 任务 / 200 攻击 / 58 故障 / 种子"),
            ("客户端（apps，.ts/.tsx）", "81", "10,941", "shared-ui + web + im 三个包"),
            ("契约（contracts）", "122", "11,221", "39 个 JSON Schema、OpenAPI、工具目录、正反例"),
            ("合计", "335", "约 7.0 万", "不含 node_modules、构建产物与虚拟环境"),
        ],
        widths=[5.6, 1.9, 2.6, 6.0],
        font_size=9,
        first_col_bold=True,
        align_center_cols=(1, 2),
    )
    caption(doc, "统计口径：排除 .venv、node_modules、__pycache__、dist 目录；行数为物理行。")

    h2(doc, "1.5 全局口径（贯穿全部模块）")
    table(
        doc,
        ["主题", "约定", "为什么这样做"],
        [
            ("金额", "整数“分”（amount_minor），币种固定 CNY；输入最多两位小数，超精度直接报错", "避免浮点误差进入资金链路"),
            ("份额", "1e-8 定点整数（shares_e8）", "份额需要 8 位小数，定点可精确复算"),
            ("时间", "持久化 UTC；业务日与日累计用 Asia/Shanghai；相对日期基于任务参考时间解析", "跨时区与“明天”歧义是资金类缺陷高发点"),
            ("ID", "稳定字符串 ID（op_/task_/grant_/plan_/ltx_ 等），幂等键与操作 ID 分离", "重试必须复用同一逻辑操作"),
            ("哈希", "规范 JSON + SHA-256；界面摘要与后端校验同源", "防止“看到的”和“批准的”不是一回事"),
            ("状态", "任务、操作、授权、调度、预留各自独立状态机，转移集中校验", "避免用一个 status 表达所有含义"),
        ],
        widths=[2.2, 7.6, 6.2],
        font_size=9,
        first_col_bold=True,
    )
    page_break(doc)


# ==========================================================================
# 第 2 章
# ==========================================================================
def chapter_2(doc):
    h1(doc, "2　总体架构与信任边界")

    h2(doc, "2.1 分层与模块职责")
    table(
        doc,
        ["层", "目录", "职责", "关键约束"],
        [
            ("客户端", "apps/web、apps/im、apps/shared-ui", "两个渠道的界面、任务面板、授权卡、验证弹窗、回执", "只展示服务端状态，不自造成功"),
            ("网关", "finpilot/gateway", "会话、任务、确认、验证、结果、隐私、运营、回调", "人类/运营端点不对模型开放"),
            ("意图", "finpilot/intent", "自然语言 → GoalSpec，区分目标/硬约束/偏好/事实/待澄清", "关键字段缺失只能提问"),
            ("编排", "finpilot/orchestration", "DAG 规划、统一执行、增量恢复、人工交接", "模型只能产出候选计划"),
            ("内核", "finpilot/kernel", "身份、账本、预留、授权、强验证、提交事务、操作、调度、对账", "唯一资金与授权写入点"),
            ("安全", "finpilot/security", "分级策略、范围校验、卡控规则、信任边界、沙箱、审计链", "最终校验在服务端"),
            ("领域", "finpilot/domains", "账单、理财规则、理财订单、卡片、订阅、AA", "只写业务状态，不直接改余额"),
            ("适配器", "finpilot/adapters", "银行/商户/量化只读适配", "稳定业务键 + 查询能力"),
            ("提供方沙箱", "services/provider-sandbox", "与主实现完全分离的银行/商户服务", "独立进程、独立状态库"),
            ("数据与评测", "data/、eval/", "冻结数据集、运行器、判定器、基线、统计", "原始结果只追加不改写"),
        ],
        widths=[1.9, 4.3, 6.4, 5.0],
        font_size=8.6,
        first_col_bold=True,
    )

    h2(doc, "2.2 信任边界")
    para(doc, "系统把“谁说的”和“系统认什么”严格分开，这是全部安全设计的前提：")
    bullets(
        doc,
        [
            ("身份", "：只来自服务端会话表；请求体、模型输出都不能覆盖 user_id、角色或权限等级。"),
            ("模型", "：只能产出候选计划与工具参数；不能签发授权、不能批准卡片、不能把业务结果改成成功。"),
            ("人类确认与强验证", "：独立端点（confirm / challenge verify），在 OpenAPI 中标记 x-agent-exposed=false，不进模型工具目录。"),
            ("外部文本", "：商户描述、账单备注、工具返回的自由文本一律进 untrusted_text，与结构化金额分栏，永不参与金额与授权判断。"),
            ("金额与资金效果", "：只能由内核的双重记账端口产生；领域模块只能申请效果。"),
            ("审计", "：追加写 + 哈希链 + 独立签名摘要；审计不可写时高风险新提交失败关闭。"),
        ],
    )

    h2(doc, "2.3 运行模式")
    table(
        doc,
        ["模式", "含义", "本机状态"],
        [
            ("mock", "无模型，使用确定性规则编译与脚本驱动；用于开发、测试与回归", "默认模式，全部实测均在此模式"),
            ("offline_replay", "离线重放冻结轨迹，明确标注不冒充实时", "仅在配置与文档中声明，未实现回放器"),
            ("live", "在线模型运行，需要 MODEL_BASE_URL / MODEL_API_KEY", "未实现模型客户端；评测器遇到 --mode live 会拒绝运行并说明原因"),
        ],
        widths=[2.6, 7.8, 5.6],
        font_size=9,
        first_col_bold=True,
    )
    note_box(
        doc,
        "诚实披露",
        "仓库中 model_base_url / model_api_key 只是配置字段，没有对应客户端实现。因此本报告所有评测数字都是"
        "“工程回归”，不是模型能力结论，也不能用来回答“换一个模型是否仍然有效”。",
    )

    h2(doc, "2.4 数据流（一次转账的完整路径）")
    code_block(
        doc,
        [
            "用户(APP/IM) ──自然语言──▶ 意图编译",
            "                            │ GoalSpec: 目标/硬约束/偏好/事实/待澄清",
            "                            ▼",
            "                    规划: DAG + 预算/底线/期限校验",
            "                            │",
            "              ◀── 计划 + scope_hash ──┘",
            "确认界面(展示与校验同源) ──confirm──▶ 授权",
            "                            │ GREEN/YELLOW 直接签发",
            "                            │ RED → 独立 TOTP → 签发受限授权",
            "                            ▼",
            "提交事务: 归属→哈希→策略→日额度→授权→资金→版本→Outbox",
            "                            │",
            "Outbox 工作者 ──▶ 提供方沙箱 ──▶ 签名回调",
            "                            │ 去重 / 版本 / 冲突隔离",
            "                            ▼",
            "双重分录记账 ──▶ 回执 + 操作状态 + 审计事件",
            "                            │",
            "                客户端展示“已受理 / 待确认 / 已到账 / 未知”",
        ],
    )
    page_break(doc)


# ==========================================================================
# 第 3 章
# ==========================================================================
def chapter_3(doc):
    h1(doc, "3　核心机制说明")

    h2(doc, "3.1 双重记账与资金口径")
    para(doc, "账户、预留、冻结与应急金共同决定“能花多少”。定义与公式如下：")
    table(
        doc,
        ["符号", "含义", "计算方式"],
        [
            ("B", "已记账余额", "accounts.posted_balance_minor"),
            ("H", "有效任务预留", "Σ reservations.remaining_minor（active / partially_captured）"),
            ("F", "其他银行冻结", "accounts.frozen_other_minor（不与 H 重叠）"),
            ("E", "应急金底线", "保护策略中用户确认的不可动用金额"),
        ],
        widths=[1.4, 4.6, 9.6],
        font_size=9,
        first_col_bold=True,
        align_center_cols=(0,),
    )
    para(doc, "银行可用余额 = B − H − F；规划可支配金额 = max(0, B − H − F − E)。", bold=True)

    para(doc, "守恒规则：每笔账本事务的分录金额之和必须为 0，否则拒绝写入；客户账户禁止负余额。", space_after=6)
    para(doc, "手算示例（与测试断言一致）：", space_after=2)
    table(
        doc,
        ["步骤", "操作", "B（已记账）", "H（预留剩余）", "可用（B−H−F）"],
        [
            ("①", "初始：余额 1000 元，无预留", "100000", "0", "100000"),
            ("②", "为任务创建 800 元预留（不改 B）", "100000", "80000", "20000"),
            ("③", "从预留中捕获 300 元（B 与 H 同减）", "70000", "50000", "20000"),
            ("④", "释放未使用的 500 元预留（只减 H）", "70000", "0", "70000"),
        ],
        widths=[1.3, 7.4, 2.6, 2.8, 2.5],
        font_size=9,
        align_center_cols=(0, 2, 3, 4),
    )
    caption(doc, "第 ③ 步是“禁止双减”的关键：捕获同时减少余额与预留，可用额不变；若只减余额再额外扣可用，就是双减。")

    h2(doc, "3.2 分级授权与强验证")
    para(doc, "策略等级由服务端判定，客户端与模型都不能降低等级。1000 元是“验证等级边界”，不是转账限额。")
    table(
        doc,
        ["等级", "执行方式", "覆盖的操作（节选）", "失败语义"],
        [
            ("GREEN", "身份与归属检查后直接执行", "余额、账单报表、产品比较、卡状态、商户报价、量化查询、transfer.prepare", "只读失败不影响资金"),
            ("YELLOW", "展示对象/金额/影响后取得明确确认", "≤1000 元的当日累计转账、订阅查询（官方例外）、订阅取消、AA 请求、提醒、卡片申请、调低额度、增加用户限制", "未确认则停在待授权，不产生操作"),
            ("RED", "确认摘要 + 实际强验证，通过后签发受限授权", ">1000 元转账、挂失/解挂、补卡、申购、赎回、付费商户预订、解除代扣、申请提额、解除重要限制", "验证失败零提交；尝试过多锁定 15 分钟"),
            ("OPERATOR", "独立运营角色，审计必记", "模拟银行审批、风控解冻、接管复核", "客户身份调用直接 403"),
        ],
        widths=[1.9, 4.4, 7.2, 4.1],
        font_size=8.6,
        first_col_bold=True,
        align_center_cols=(0,),
    )
    para(doc, "日累计口径：已成功转账本金 + 已占用的待执行本金 + 本次本金；恰好 100000 分为 YELLOW，超过为 RED；手续费不计入本金累计。", space_after=6)

    para(doc, "强验证实现要点：", space_after=2)
    bullets(
        doc,
        [
            "独立的真实 TOTP 因子（RFC 6238，HMAC-SHA1，30 秒步长，±1 窗口），不是固定验证码、不是“已刷脸”动画；",
            "挑战有效期 120 秒、最多 5 次尝试、失败锁定 15 分钟；",
            "成功证明绑定 用户 + scope_hash + challenge_id + 过期时间，只能满足指定范围；",
            "已成功消费的时间计数器不可在另一个挑战复用（防重放）；",
            "验证码不进入对话、模型上下文、日志与审计（审计层另有断言，写入即报错）。",
        ],
    )

    h2(doc, "3.3 精确授权与范围授权")
    table(
        doc,
        ["维度", "精确授权（exact）", "范围授权（scoped）"],
        [
            ("绑定内容", "用户、计划修订、操作 ID、账户、对象、金额、费用上限、时间窗、条款版本、参数哈希", "额外列出允许的工具/商户/产品集合、每笔上限、总额上限、最多次数、截止时间、可自动执行的恢复动作"),
            ("使用次数", "1 次", "按授权声明的次数"),
            ("修订继承", "不允许（精确授权不存在修订继承例外）", "仅当新修订经确定性检查属于已批准子集时才可沿用，并共享额度与次数"),
            ("重置风险", "换 operation_id 无法复用同一授权", "换 operation_id 不能重置额度与次数（按 GrantUse 共享计数）"),
            ("撤销", "停止未提交动作；已提交与未知结果继续核验", "同左，且返回被停止的步骤与在途操作"),
        ],
        widths=[2.4, 6.6, 8.6],
        font_size=8.8,
        first_col_bold=True,
    )

    h2(doc, "3.4 操作状态与“未知”处理")
    para(doc, "系统刻意区分三件不同的事：任务状态、授权状态、业务（操作）状态。操作状态机：")
    code_block(
        doc,
        [
            "created → awaiting_authorization → authorized → dispatching → pending → succeeded",
            "                                              │            │",
            "                                              ▼            ▼",
            "                                           unknown ◀──────┘   （外部已受理但应答丢失）",
            "unknown ──用原 operation_id 查询──▶ succeeded / failed（查询失败则保持 unknown，不写成失败）",
        ],
    )
    para(doc, "关键规则：未知结果不得释放资金占用、不得重复支出、不得据到达顺序改账；冲突终态（本地已成功却收到失败回执）进入隔离并开人工工单。", space_after=6)

    h2(doc, "3.5 异步执行：Outbox、独立提供方与对账")
    numbered(
        doc,
        [
            "提交事务内只写“提交意图”与 Outbox 事件，外部效果在该事务提交之后才可能发生；",
            "Outbox 工作者用 SKIP LOCKED 领取，调用适配器前强制重读并锁定操作行（防陈旧状态）；",
            "提供方是独立进程 + 独立 SQLite 状态库（不共享主库、不使用 SQLAlchemy），支持稳定业务键、查询、取消与故障注入；",
            "回调需 HMAC 签名 + 时间窗 + nonce；按 (provider_id, event_id) 去重、按 event_version 拒绝旧回执回退；",
            "不同业务方状态词表（confirmed / booked / settled / rejected）统一归一为 succeeded / failed / pending / cancelled / unknown；",
            "对账器用原 operation_id 查询在途操作：终态则结算，仍未知则保持占用，超过 15 分钟升级人工工单。",
        ],
    )

    h2(doc, "3.6 审计：哈希链与独立签名摘要")
    para(doc, "事件哈希 = SHA-256(上一条哈希 + payload_hash + 事件类型 + 时间戳)；签名 = HMAC-SHA256(独立密钥, 事件哈希)。")
    bullets(
        doc,
        [
            ("可检出", "：改 payload、删中间事件、重排、断链、伪造签名。"),
            ("额外能力", "：签名摘要另含按实际内容重算的 content_hash，可发现“只改 payload 不改哈希”和尾部删除。"),
            ("写入拒绝", "：含 password/otp/totp/cvv/pan 等键、疑似 6 位验证码字符串、私有思维链键的 payload 直接拒写。"),
            ("失败关闭", "：审计表不可写时，高风险新提交返回 AUDIT_UNAVAILABLE，在途回执转可靠待补队列。"),
            ("已知边界", "：单库哈希链无法抵抗同时掌握签名密钥与存储写权限的管理员整链重写；已注明收紧手段（KMS/HSM、独立只追加权限、摘要外写）。"),
        ],
    )

    h2(doc, "3.7 目标编译与规划")
    para(doc, "目标被编译成结构化 GoalSpec，严格区分五类信息：")
    table(
        doc,
        ["类别", "示例", "用途"],
        [
            ("goals", "transfer / schedule_transfer / analyze_bill / purchase_wealth / card_lost_report / plan_event", "决定生成哪些工具步骤"),
            ("hard_constraints", "金额、总预算（含费用）、应急金底线、执行时刻、截止期限", "不可放宽；违反即判不可行"),
            ("preferences", "更便宜 / 更快 / 先订某类", "仅用于排序，不改变可行性"),
            ("facts", "收款人、账户、产品、期间、渠道", "来自数据库或用户原话"),
            ("unresolved", "同名联系人、缺金额、缺日期、“你看着办”", "阻断执行，转为可回答的澄清"),
        ],
        widths=[2.6, 8.6, 6.4],
        font_size=8.8,
        first_col_bold=True,
    )
    para(doc, "规划器在有限候选上做确定性搜索：默认上限 24 个节点 / 128 个候选 / 2 秒；超限返回“规划限额”而不断言“无解”。", space_after=6)
    para(doc, "可行解按字典序排序：用户明确优先级 → 目标完成数 → 总费用（含手续费）→ 必要确认次数 → 计划变更量。", space_after=6)
    para(doc, "不可行时不擅自放宽约束，而是给出可验证原因与需要用户决定的选项，例如：", space_after=2)
    code_block(doc, ["BUDGET_EXCEEDED:72000>50000    PROTECTED_FLOOR_OR_FUNDS:account_salary:120000>100000"])

    h2(doc, "3.8 增量恢复")
    para(doc, "恢复的目标不是“重新跑一遍”，而是保留已确认效果、只重做受影响的未完成部分：")
    numbered(
        doc,
        [
            "先用原 operation_id 核对在途未知结果，避免重复支出；",
            "把变化事件映射为 read_set 可匹配的事实键（商户、报价、账户、条款、政策、预算、卡状态）；",
            "选出 read_set 与变化事实相交的未完成节点，再取依赖后继闭包，得到受影响集合；",
            "已完成节点不可变；在途未知节点作为待核验边界，不重新生成同目的的新支付；",
            "剩余预算 = 目标预算 − 已确认成本 − 在途最大承诺金额；",
            "对保留节点做新硬约束的全量校验（不因“不在局部影响集合”而跳过全局预算）；",
            "产生新修订并重新判定授权：超出原授权范围必须重新确认。",
        ],
    )
    para(doc, "当已发生不可撤销成本超过新预算时，系统准确说明“新约束与既成事实不兼容”，并停止后续支出，不伪造回退。", space_after=6)

    h2(doc, "3.9 生成代码沙箱与不可信输入")
    table(
        doc,
        ["防护", "本机实现", "差距"],
        [
            ("代码执行隔离", "父进程 AST 白名单 → 独立子进程（-I -S、一次性临时目录、最小环境）→ audit hook 拦截 socket/子进程/写文件/动态导入", "非内核级隔离；C 扩展直呼系统调用可绕过 Python 层 hook"),
            ("资源限额", "墙钟超时、输出上限、进程内限额声明", "Windows 无 resource 模块，CPU/内存/进程硬限额未生效"),
            ("失效处理", "策略不可用或子进程无法启动 → 返回 unavailable，绝不回退宿主 exec/eval；只读报表改走受限 DSL", "—"),
            ("注入防御", "结构化权威字段与自由文本分栏；注入扫描（指令覆盖/角色冒充/外泄/编码绕过/同形字等）+ 来源标记", "检测只是辅助，主防线是服务端能力边界"),
        ],
        widths=[2.8, 8.2, 6.6],
        font_size=8.6,
        first_col_bold=True,
    )
    page_break(doc)