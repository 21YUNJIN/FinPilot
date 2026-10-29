"""Build the shareable DOCX decision memo for the S3 quant integration."""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "S3_量化接入决策与实施说明_2026-10-28.docx"

BODY_FONT = "Microsoft YaHei"
MONO_FONT = "Consolas"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
GREY = RGBColor(0x59, 0x59, 0x59)


def set_run_font(run, name=BODY_FONT, size=10.5, bold=None, color=None):
    run.font.name = name
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    fonts.set(qn("w:eastAsia"), name)
    fonts.set(qn("w:ascii"), name)
    fonts.set(qn("w:hAnsi"), name)


def shade(cell, hex_color):
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def add_para(doc, text, *, size=10.5, bold=False, color=None, space_after=4, indent=None, font=BODY_FONT):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(space_after)
    paragraph.paragraph_format.line_spacing = 1.25
    if indent is not None:
        paragraph.paragraph_format.left_indent = Cm(indent)
    run = paragraph.add_run(text)
    set_run_font(run, font, size, bold, color)
    return paragraph


def add_bullets(doc, items, *, size=10.5):
    for text in items:
        paragraph = doc.add_paragraph(style="List Bullet")
        paragraph.paragraph_format.space_after = Pt(2)
        paragraph.paragraph_format.line_spacing = 1.2
        run = paragraph.add_run(text)
        set_run_font(run, BODY_FONT, size)


def add_heading(doc, text, level=1):
    heading = doc.add_heading(level=level)
    heading.paragraph_format.space_before = Pt(10 if level == 1 else 6)
    heading.paragraph_format.space_after = Pt(4)
    run = heading.add_run(text)
    set_run_font(run, BODY_FONT, 14 if level == 1 else 11.5, True, ACCENT)


def add_table(doc, headers, rows, widths=None, font_size=9):
    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = False
    if widths:
        for idx, width in enumerate(widths):
            table.columns[idx].width = Cm(width)
    for idx, header in enumerate(headers):
        cell = table.rows[0].cells[idx]
        cell.text = ""
        run = cell.paragraphs[0].add_run(header)
        set_run_font(run, BODY_FONT, font_size, True)
        shade(cell, "DCE6F1")
        if widths:
            cell.width = Cm(widths[idx])
    for row in rows:
        cells = table.add_row().cells
        for idx, value in enumerate(row):
            cells[idx].text = ""
            run = cells[idx].paragraphs[0].add_run(str(value))
            set_run_font(run, BODY_FONT, font_size)
            if widths:
                cells[idx].width = Cm(widths[idx])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return table


def add_code(doc, text):
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.4)
    paragraph.paragraph_format.space_after = Pt(4)
    paragraph.paragraph_format.line_spacing = 1.0
    ppr = paragraph._p.get_or_add_pPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), "F2F2F2")
    ppr.append(shd)
    run = paragraph.add_run(text)
    set_run_font(run, MONO_FONT, 8.5)


def add_footer(section):
    footer = section.footer
    paragraph = footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run("S3 理财 · StockQuant 接入决策 · 第 ")
    set_run_font(run, BODY_FONT, 8.5, color=GREY)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run2 = paragraph.add_run()
    set_run_font(run2, BODY_FONT, 8.5, color=GREY)
    run2._r.append(fld_begin)
    run2._r.append(instr)
    run2._r.append(fld_end)
    run3 = paragraph.add_run(" 页")
    set_run_font(run3, BODY_FONT, 8.5, color=GREY)


def main():
    doc = Document()
    section = doc.sections[0]
    section.left_margin = Cm(2.3)
    section.right_margin = Cm(2.3)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)

    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)

    title = doc.add_paragraph()
    title.paragraph_format.space_after = Pt(2)
    run = title.add_run("S3 理财模块：StockQuant 接入决策与实施说明")
    set_run_font(run, BODY_FONT, 20, True, ACCENT)

    subtitle = doc.add_paragraph()
    subtitle.paragraph_format.space_after = Pt(12)
    run = subtitle.add_run("融策智能体 · 六场景第三模块 · 2026-10-28")
    set_run_font(run, BODY_FONT, 10, False, GREY)

    add_para(doc,
             "结论先行：第三步（S3 理财）使用 StockQuant 作为只读风险计算内核，"
             "但必须银行化改造；FinPilot 自带的适当性、条款、费率、流动性与资金执行继续由 FinPilot 负责。"
             "不是二选一，而是“决策约束归银行、风险度量归量化系统”。",
             size=11, bold=True)

    add_heading(doc, "一、结论与分工")
    add_table(
        doc,
        ["层", "负责内容", "系统"],
        [
            ["产品层", "风险问卷、R1—R5 适当性、产品条款、费率、期限、赎回到账、申购赎回、份额与资金", "FinPilot（自带）"],
            ["计算层", "净值波动、回撤、CVaR、Sharpe、压力情景、偏好排序与可复现证据", "StockQuant 银行版（本次改造）"],
            ["隔离", "独立进程、独立 Python 环境、Bearer 令牌、只读、无下单函数", "服务边界"],
        ],
        widths=[2.2, 10.2, 4.0],
    )
    add_para(doc, "对应 V5 第 11 节（S3）与第 15 节（StockQuant 边界），分工包 C03。", size=9, color=GREY)

    add_heading(doc, "二、为什么不能只用 FinPilot 自带")
    add_para(doc, "现有量化代码不在比赛主线上，无法直接支撑 S3 的产品风险计算：")
    add_table(
        doc,
        ["现有代码", "实际能力", "对 S3 是否够用"],
        [
            ["bankagent/quant_adapter", "个股因子 z 分、CSI300 行情组合波动；压力测试与回测摘要未实现", "不够：输入是股票代码，不是产品净值"],
            ["bankagent/finance/metrics", "营收增速、毛利率、ROE、DCF", "不够：企业投研口径"],
            ["bankagent/valuation", "估值与反方质询", "与产品风险无关"],
        ],
        widths=[4.6, 7.0, 4.8],
    )
    add_para(doc, "若沿用内置，C03 需要从零补净值处理、指标口径、样本门槛、压力情景与来源追踪，"
                  "等于重写一套量化统计。")

    add_heading(doc, "三、为什么不能原样搬 StockQuant")
    add_table(
        doc,
        ["StockQuant 能力", "与 S3 的关系", "处理"],
        [
            ["因子、选股、组合优化、回测 CAGR", "银行理财不选股、不承诺收益", "不接入"],
            ["依赖 5.5 GB A 股行情", "产品是净值序列，不是行情", "改为调用方传入净值"],
            ["回测收益、DSR/PBO 主张", "评审指出未过门槛，不能当比赛成绩", "不进入产品页"],
            ["下单、QMT、券商凭证", "银行 Agent 不得调用", "物理隔离"],
            ["MIT 许可", "可复用、可修改、可分发", "保留版权声明"],
        ],
        widths=[5.2, 6.6, 4.6],
    )

    add_heading(doc, "四、改造清单（已落地）")
    add_bullets(doc, [
        "输入银行化：股票代码/股数 → 产品净值序列 + 官方风险等级 + 费率/期限/到账/起购。",
        "输出银行化：value、unit、sample_count、window、method、na_reason，另加 source_hash、computed_at、method_version。",
        "口径守卫：≥60 个有效日收益才算年化波动（252 因子）；<252 个收益不报正式 95% CVaR；回撤标注整个窗口；缺数据返回 null 与原因。",
        "压力情景：净值冲击、样本内最差 20 日重放、波动×2 参数情景，逐条写假设并声明“不是损失上限”。",
        "影响决策：硬约束先过滤（风险、期限、流动性、起购），再按用户明确偏好排序，同时输出 A4 消融排序。",
        "可追溯：净值 SHA-256、客户端独立复算、方法版本钉住、审计日志记录 request_id 与响应哈希。",
        "服务化：独立进程、Bearer 令牌、限流、4 MB 上限；FinPilot 侧只有一个 HTTP 客户端。",
        "降级有语义：指标可选时标 NA 继续；指标是硬条件时阻断；绝不把“没数据”当“低风险”。",
    ])

    add_heading(doc, "五、接口")
    add_para(doc, "三个只读端点，统一信封（V5 §16.2），HTTP 200 不等于业务成功：")
    add_table(
        doc,
        ["方法", "路径", "用途"],
        [
            ["GET", "/v1/meta", "能力、引擎版本、口径门槛、排序权重"],
            ["POST", "/v1/quant/risk-report", "逐产品指标与来源"],
            ["POST", "/v1/quant/rank", "硬过滤 + 偏好排序 + A4 基线"],
            ["POST", "/v1/quant/stress", "压力情景"],
        ],
        widths=[2.0, 6.4, 8.0],
    )
    add_para(doc, "请求节选：", size=9, color=GREY)
    add_code(doc, '''POST /v1/quant/rank
{"as_of":"2026-10-23",
 "user":{"risk_level":"R3","preference_mode":"stability",
         "horizon_days":365,"liquidity_need_days":10,"budget_minor":1000000},
 "products":[{"product_id":"WM-BOND-102","official_risk_level":"R2",
              "total_fee_bps":30,"term_days":0,"settlement_days":2,
              "min_amount_minor":10000,
              "nav":[{"date":"2026-10-22","value":1.0831}, ...]}]}''')
    add_para(doc, "响应节选：", size=9, color=GREY)
    add_code(doc, '''{"status":"ok","data":{
   "eligible":6,
   "excluded":[{"product_id":"WM-EQ-401",
                "reasons":[{"code":"RISK_LEVEL_EXCEEDS_USER", ...}]}],
   "ranked":[{"rank":1,"product_id":"WM-BOND-101","score":98.0,
              "metrics_brief":{"annual_volatility":0.0113,
                               "max_drawdown":-0.0356,
                               "historical_cvar_95":-0.0014}}],
   "baseline_ranked":[...], "rank_changes":[...]}}''')

    add_heading(doc, "六、已验证的证据")
    add_table(
        doc,
        ["项", "结果"],
        [
            ["测试", "60 项通过（公式、口径边界、契约、真实 HTTP 往返）"],
            ["引擎一致性", "CVaR 尾部分位与 stockquant.portfolio.optimizer.portfolio_cvar 数值一致"],
            ["联调", "12 个产品全部计算成功，11 个可排序；新发产品正确标记为样本不足"],
            ["可追溯", "12/12 产品的客户端复算哈希与服务端 source_hash 一致"],
            ["偏好影响", "同一产品集合只改偏好：stability 首位为现金管理，return 首位变为纯债基金"],
            ["A4 消融", "同一响应返回无量化基线；return 模式下首位与 A4 不同"],
            ["延迟", "12 产品 × 994 点：239—1187 ms；同快照重复调用约 108 ms"],
        ],
        widths=[4.0, 12.2],
    )
    add_para(doc, "数据为 synthetic_bank_catalog_v2 合成净值，只证明管道与契约，不构成收益或风险结论；"
                  "H4 仍需在真实产品与真实用户偏好下重跑。", size=9, color=GREY)

    add_heading(doc, "七、V5 需求映射")
    add_table(
        doc,
        ["需求", "本实现", "状态"],
        [
            ["QUANT-01 真实代码/只读/可追溯", "真实 import StockQuant 指标；版本、commit、source_hash 随响应返回", "完成（合成联调）"],
            ["QUANT-02 可复现；不足返回 NA", "输入哈希 + 固定口径 + 60/252 门槛 + na_reason", "完成"],
            ["QUANT-03 影响排序 + H4 消融", "rank 同时输出量化排序、A4 基线与 rank_changes", "管道完成，真实 H4 待跑"],
            ["QUANT-04 三类风险分开、不承诺收益", "官方等级/统计档/流动性分离，免责声明固定返回", "完成"],
            ["S3-02 比较与硬约束排序", "硬过滤先于打分；费用、期限、到账、数据日期可展示", "完成（展示由 A 接）"],
        ],
        widths=[4.6, 9.0, 3.6],
    )

    add_heading(doc, "八、需要团队确认")
    add_bullets(doc, [
        "真实净值来源与授权：比赛环境只能用合成产品，界面必须标注；接真实产品需补数据授权与披露口径。",
        "量化在 UI 的位置：放在比较页风险列与“为什么这样排序”，不放在收益宣传位。",
        "硬门槛归属：若银行要求指标不达标即不可申购，由 B 的订单前置条件消费 RiskGate，不由模型临时决定。",
    ])

    add_heading(doc, "九、文件与复现")
    add_table(
        doc,
        ["路径", "内容"],
        [
            ["stockquant_bank/", "指标、NAV 校验、报告、排序、压力、HTTP、审计"],
            ["finpilot_adapter/", "放进 FinPilot 的客户端与降级策略"],
            ["docs/", "决策备忘、方法卡、C03 集成手册、证据说明"],
            ["evidence/", "实际调用输出与 SHA-256 索引"],
            ["contracts/openapi.yaml", "端点与 Schema"],
        ],
        widths=[6.6, 9.0],
    )
    add_code(doc, '''export STOCKQUANT_ROOT=/path/to/StockQuant_Backup_2026-10-28
export PYTHONPATH="$STOCKQUANT_ROOT:."
python -m pytest                       # 60 passed
python scripts/run_evidence.py         # 证据写入 evidence/''')

    add_footer(section)

    doc.save(OUT)
    print(OUT)


if __name__ == "__main__":
    main()