"""生成《FinPilot 融策智能体 · 项目详细说明与功能报告》.docx

数据来源：本仓库实测结果（测试收集数、live E2E、迁移、评测产出），
以及 docs/ 下同一轮交付的技术文档、证据索引与限制清单。
所有数字均可在仓库内复核，脚本不臆造任何指标。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

OUT = Path(r"G:\比赛\finpilot\docs\FinPilot项目详细说明与功能报告.docx")

CN_BODY = "等线"
CN_HEAD = "微软雅黑"
MONO = "Consolas"
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
MUTED = RGBColor(0x59, 0x59, 0x59)


# --------------------------------------------------------------------------
# 基础排版工具
# --------------------------------------------------------------------------
def _set_run_font(run, *, name=CN_BODY, size=10.5, bold=False, color=None, mono=False):
    run.font.name = MONO if mono else name
    run.font.size = Pt(size)
    run.bold = bold
    if color is not None:
        run.font.color.rgb = color
    rpr = run._element.get_or_add_rPr()
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.append(fonts)
    east = MONO if mono else name
    fonts.set(qn("w:eastAsia"), east)
    fonts.set(qn("w:ascii"), MONO if mono else name)
    fonts.set(qn("w:hAnsi"), MONO if mono else name)


def _shade(paragraph_or_cell, hex_fill: str) -> None:
    element = paragraph_or_cell._element
    pr = element.get_or_add_pPr() if hasattr(element, "get_or_add_pPr") else element.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_fill)
    pr.append(shd)


def _borders(cell, color="BFBFBF", sz="4") -> None:
    tc_pr = cell._element.get_or_add_tcPr()
    borders = OxmlElement("w:tcBorders")
    for edge in ("top", "left", "bottom", "right"):
        tag = OxmlElement(f"w:{edge}")
        tag.set(qn("w:val"), "single")
        tag.set(qn("w:sz"), sz)
        tag.set(qn("w:space"), "0")
        tag.set(qn("w:color"), color)
        borders.append(tag)
    tc_pr.append(borders)


def setup_document() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.4)
    section.right_margin = Cm(2.2)

    normal = doc.styles["Normal"]
    normal.font.name = CN_BODY
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CN_BODY)
    normal.paragraph_format.line_spacing = 1.32
    normal.paragraph_format.space_after = Pt(4)

    for name, size, color in (("Heading 1", 16, ACCENT), ("Heading 2", 13, ACCENT), ("Heading 3", 11.5, RGBColor(0x33, 0x33, 0x33))):
        style = doc.styles[name]
        style.font.name = CN_HEAD
        style.font.size = Pt(size)
        style.font.bold = True
        style.font.color.rgb = color
        style.element.rPr.rFonts.set(qn("w:eastAsia"), CN_HEAD)
        style.paragraph_format.space_before = Pt(14 if name == "Heading 1" else 10)
        style.paragraph_format.space_after = Pt(6)
        style.paragraph_format.keep_with_next = True
    return doc


def h1(doc, text):
    p = doc.add_heading(text, level=1)
    for run in p.runs:
        _set_run_font(run, name=CN_HEAD, size=16, bold=True, color=ACCENT)
    return p


def h2(doc, text):
    p = doc.add_heading(text, level=2)
    for run in p.runs:
        _set_run_font(run, name=CN_HEAD, size=13, bold=True, color=ACCENT)
    return p


def h3(doc, text):
    p = doc.add_heading(text, level=3)
    for run in p.runs:
        _set_run_font(run, name=CN_HEAD, size=11.5, bold=True, color=RGBColor(0x33, 0x33, 0x33))
    return p


def para(doc, text, *, size=10.5, bold=False, color=None, align=None, space_after=4, indent=None, italic=False):
    p = doc.add_paragraph()
    if align is not None:
        p.alignment = align
    p.paragraph_format.space_after = Pt(space_after)
    if indent is not None:
        p.paragraph_format.left_indent = Cm(indent)
    run = p.add_run(text)
    _set_run_font(run, size=size, bold=bold, color=color)
    run.italic = italic
    return p


def rich(doc, parts, *, size=10.5, space_after=4, indent=None):
    """parts: [(text, bold)]"""
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(space_after)
    if indent is not None:
        p.paragraph_format.left_indent = Cm(indent)
    for text, bold in parts:
        run = p.add_run(text)
        _set_run_font(run, size=size, bold=bold)
    return p


def bullets(doc, items, *, level=0, size=10.5):
    for item in items:
        p = doc.add_paragraph(style="List Bullet" if level == 0 else "List Bullet 2")
        p.paragraph_format.space_after = Pt(2)
        p.paragraph_format.line_spacing = 1.25
        if isinstance(item, tuple):
            bold_head, rest = item
            r1 = p.add_run(bold_head)
            _set_run_font(r1, size=size, bold=True)
            r2 = p.add_run(rest)
            _set_run_font(r2, size=size)
        else:
            run = p.add_run(item)
            _set_run_font(run, size=size)


def numbered(doc, items, *, size=10.5):
    for index, item in enumerate(items, start=1):
        p = doc.add_paragraph()
        p.paragraph_format.left_indent = Cm(0.75)
        p.paragraph_format.first_line_indent = Cm(-0.55)
        p.paragraph_format.space_after = Pt(2)
        run = p.add_run(f"{index}. {item}")
        _set_run_font(run, size=size)


def _display_width(text: str) -> int:
    """估算等宽渲染宽度：CJK/全角字符按 2 列计。"""
    width = 0
    for ch in text:
        width += 2 if ord(ch) > 0x2E7F or ch in "　、。，；：“”（）《》【】…—" else 1
    return width


WIDE_LINES: list[tuple[int, str]] = []


def code_block(doc, lines):
    for line in lines:
        width = _display_width(line)
        if width > 96:
            WIDE_LINES.append((width, line))
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.line_spacing = 1.02
        p.paragraph_format.left_indent = Cm(0.3)
        _shade(p, "F4F6F8")
        run = p.add_run(line if line else " ")
        _set_run_font(run, size=8.0, mono=True, color=RGBColor(0x24, 0x29, 0x2E))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def _table_row_flags(row, *, header: bool = False) -> None:
    """表头行跨页重复；所有行禁止被从中间切断。"""
    tr_pr = row._tr.get_or_add_trPr()
    if header:
        tag = OxmlElement("w:tblHeader")
        tag.set(qn("w:val"), "true")
        tr_pr.append(tag)
    cant_split = OxmlElement("w:cantSplit")
    tr_pr.append(cant_split)


def table(doc, headers, rows, *, widths=None, font_size=9, header_fill="DCE6F1", first_col_bold=False, align_center_cols=()):
    # 版心宽度 = 21.0 − 2.4 − 2.2 = 16.4cm；超宽时按比例压缩，避免表格溢出右边界
    if widths:
        total = sum(widths)
        limit = 16.2
        if total > limit:
            widths = [w * limit / total for w in widths]
    tbl = doc.add_table(rows=1, cols=len(headers))
    tbl.style = "Table Grid"
    tbl.alignment = WD_TABLE_ALIGNMENT.CENTER
    tbl.autofit = False
    _table_row_flags(tbl.rows[0], header=True)
    header_cells = tbl.rows[0].cells
    for index, text in enumerate(headers):
        cell = header_cells[index]
        cell.text = ""
        p = cell.paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(1)
        p.paragraph_format.keep_with_next = True
        run = p.add_run(text)
        _set_run_font(run, size=font_size, bold=True)
        _shade(cell, header_fill)
        _borders(cell)
        if widths:
            cell.width = Cm(widths[index])
    for row in rows:
        new_row = tbl.add_row()
        _table_row_flags(new_row)
        cells = new_row.cells
        for index, value in enumerate(row):
            cell = cells[index]
            cell.text = ""
            p = cell.paragraphs[0]
            p.paragraph_format.space_after = Pt(1)
            p.paragraph_format.line_spacing = 1.16
            if index in align_center_cols:
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            run = p.add_run(str(value))
            _set_run_font(run, size=font_size)
            if first_col_bold and index == 0:
                run.bold = True
            _borders(cell)
            if widths:
                cell.width = Cm(widths[index])
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return tbl


def note_box(doc, label, text, *, fill="FFF4E5"):
    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Cm(0.2)
    p.paragraph_format.space_before = Pt(4)
    p.paragraph_format.space_after = Pt(6)
    _shade(p, fill)
    r1 = p.add_run(f"{label}  ")
    _set_run_font(r1, size=10, bold=True, color=RGBColor(0x8A, 0x4B, 0x08))
    r2 = p.add_run(text)
    _set_run_font(r2, size=10)


def caption(doc, text):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(8)
    run = p.add_run(text)
    _set_run_font(run, size=9, color=MUTED)


def add_page_number_footer(doc, label: str) -> None:
    footer = doc.sections[0].footer
    p = footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run(f"{label}    第 ")
    _set_run_font(run, size=8.5, color=MUTED)
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = " PAGE "
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run2 = p.add_run()
    _set_run_font(run2, size=8.5, color=MUTED)
    run2._element.append(fld_begin)
    run2._element.append(instr)
    run2._element.append(fld_end)
    run3 = p.add_run(" 页")
    _set_run_font(run3, size=8.5, color=MUTED)


def page_break(doc):
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)