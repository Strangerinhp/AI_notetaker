"""Export editable meeting Markdown as a PTC1-style Word report."""

from __future__ import annotations

import re
from datetime import date, datetime
from io import BytesIO
from pathlib import Path
from typing import BinaryIO

from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, Twips
from lxml import etree

BASE_DIR = Path(__file__).resolve().parent
REPORT_TEMPLATE_PATH = BASE_DIR / "templates" / "ptc1_report_template.docx"

FONT_NAME = "Times New Roman"
BODY_SIZE = 14
PAGE_WIDTH_CM = 21.0
PAGE_HEIGHT_CM = 29.7
CONTENT_WIDTH_CM = 16.0
HEADER_TABLE_WIDTHS_DXA = (4321, 5779)
SIGNOFF_TABLE_WIDTHS_DXA = (4619, 4452)

WP_NS = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WPS_NS = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
LIST_RE = re.compile(r"^(\s*)(?:[-+*]|\d+[.)])\s+(.+?)\s*$")
TABLE_RULE_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")
METADATA_RE = re.compile(
    r"^\*\*(?:Thời gian|Địa điểm|Chủ trì|Thành phần tham dự)\s*:\*\*",
    re.IGNORECASE,
)
INLINE_RE = re.compile(
    r"\*\*\*(.+?)\*\*\*|"
    r"\*\*(.+?)\*\*|"
    r"__(.+?)__|"
    r"(?<!\*)\*([^*\n]+?)\*|"
    r"(?<!_)_([^_\n]+?)_|"
    r"`([^`\n]+)`|"
    r"\[([^]\n]+)\]\(([^)\n]+)\)"
)


def _set_run_font(
    run,
    *,
    size: float = BODY_SIZE,
    bold: bool | None = None,
    italic: bool | None = None,
) -> None:
    run.font.name = FONT_NAME
    run.font.size = Pt(size)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    run_properties = run._element.get_or_add_rPr()
    run_fonts = run_properties.get_or_add_rFonts()
    for font_key in ("ascii", "hAnsi", "eastAsia", "cs"):
        run_fonts.set(qn(f"w:{font_key}"), FONT_NAME)


def _clear_document_body(document) -> None:
    body = document._element.body
    for child in list(body):
        if child.tag != qn("w:sectPr"):
            body.remove(child)


def _clear_story_container(container) -> None:
    root = container._element
    for child in list(root):
        root.remove(child)
    root.append(OxmlElement("w:p"))


def _set_cell_shading(cell, fill: str) -> None:
    properties = cell._tc.get_or_add_tcPr()
    shading = properties.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        properties.append(shading)
    shading.set(qn("w:fill"), fill)


def _set_cell_margins(cell, *, top=0, start=0, bottom=0, end=0) -> None:
    properties = cell._tc.get_or_add_tcPr()
    margins = properties.first_child_found_in("w:tcMar")
    if margins is None:
        margins = OxmlElement("w:tcMar")
        properties.append(margins)
    for edge, value in (("top", top), ("start", start), ("bottom", bottom), ("end", end)):
        node = margins.find(qn(f"w:{edge}"))
        if node is None:
            node = OxmlElement(f"w:{edge}")
            margins.append(node)
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")


def _set_table_geometry_dxa(
    table,
    widths_dxa: tuple[int, ...] | list[int],
    *,
    alignment=WD_TABLE_ALIGNMENT.CENTER,
    zero_cell_margins: bool = True,
) -> None:
    table.alignment = alignment
    table.autofit = False
    total_width = sum(widths_dxa)
    properties = table._tbl.tblPr

    table_width = properties.first_child_found_in("w:tblW")
    if table_width is None:
        table_width = OxmlElement("w:tblW")
        properties.append(table_width)
    table_width.set(qn("w:w"), str(total_width))
    table_width.set(qn("w:type"), "dxa")

    layout = properties.first_child_found_in("w:tblLayout")
    if layout is None:
        layout = OxmlElement("w:tblLayout")
        properties.append(layout)
    layout.set(qn("w:type"), "fixed")

    grid_columns = table._tbl.tblGrid.gridCol_lst
    for index, width_dxa in enumerate(widths_dxa):
        width = Twips(width_dxa)
        if index < len(grid_columns):
            grid_columns[index].set(qn("w:w"), str(width_dxa))
        for row in table.rows:
            cell = row.cells[index]
            cell.width = width
            cell_properties = cell._tc.get_or_add_tcPr()
            cell_width = cell_properties.first_child_found_in("w:tcW")
            if cell_width is None:
                cell_width = OxmlElement("w:tcW")
                cell_properties.append(cell_width)
            cell_width.set(qn("w:w"), str(width_dxa))
            cell_width.set(qn("w:type"), "dxa")
            if zero_cell_margins:
                _set_cell_margins(cell)


def _set_table_geometry(table, widths_cm: list[float]) -> None:
    _set_table_geometry_dxa(
        table,
        [Cm(width).twips for width in widths_cm],
    )


def _set_reference_header_position(table) -> None:
    """Match the source's floating authority table placement exactly."""
    properties = table._tbl.tblPr
    alignment = properties.find(qn("w:jc"))
    if alignment is not None:
        properties.remove(alignment)

    positioning = properties.find(qn("w:tblpPr"))
    if positioning is None:
        positioning = OxmlElement("w:tblpPr")
        table_width = properties.find(qn("w:tblW"))
        properties.insert(
            properties.index(table_width) if table_width is not None else 0,
            positioning,
        )
    for key, value in {
        "leftFromText": "180",
        "rightFromText": "180",
        "vertAnchor": "text",
        "horzAnchor": "margin",
        "tblpXSpec": "center",
        "tblpY": "-130",
    }.items():
        positioning.set(qn(f"w:{key}"), value)


def _set_reference_cell_margins(table) -> None:
    for row in table.rows:
        for cell in row.cells:
            properties = cell._tc.get_or_add_tcPr()
            margins = properties.first_child_found_in("w:tcMar")
            if margins is None:
                margins = OxmlElement("w:tcMar")
                properties.append(margins)
            for edge in ("left", "right"):
                node = margins.find(qn(f"w:{edge}"))
                if node is None:
                    node = OxmlElement(f"w:{edge}")
                    margins.append(node)
                node.set(qn("w:w"), "115")
                node.set(qn("w:type"), "dxa")


def _set_row_height_dxa(row, height_dxa: int) -> None:
    properties = row._tr.get_or_add_trPr()
    height = properties.find(qn("w:trHeight"))
    if height is None:
        height = OxmlElement("w:trHeight")
        properties.append(height)
    height.set(qn("w:val"), str(height_dxa))
    height.attrib.pop(qn("w:hRule"), None)


def _drawing_element(namespace: str, tag: str):
    return etree.Element(f"{{{namespace}}}{tag}")


def _add_floating_rule(
    paragraph,
    *,
    width_emu: int,
    horizontal_offset_emu: int,
    vertical_offset_emu: int,
    shape_id: int,
) -> None:
    """Add the same independent 0.75 pt rule used by the source DOCX."""
    run = paragraph.add_run()
    _set_run_font(run, size=BODY_SIZE)
    run_properties = run._element.get_or_add_rPr()
    run_properties.append(OxmlElement("w:noProof"))

    drawing = OxmlElement("w:drawing")
    anchor = _drawing_element(WP_NS, "anchor")
    for key, value in {
        "distT": "0",
        "distB": "0",
        "distL": "114300",
        "distR": "114300",
        "simplePos": "0",
        "relativeHeight": str(251663360 + shape_id * 1024),
        "behindDoc": "0",
        "locked": "0",
        "layoutInCell": "1",
        "hidden": "0",
        "allowOverlap": "1",
    }.items():
        anchor.set(key, value)

    simple_position = _drawing_element(WP_NS, "simplePos")
    simple_position.set("x", "0")
    simple_position.set("y", "0")
    anchor.append(simple_position)

    horizontal = _drawing_element(WP_NS, "positionH")
    horizontal.set("relativeFrom", "column")
    horizontal_offset = _drawing_element(WP_NS, "posOffset")
    horizontal_offset.text = str(horizontal_offset_emu)
    horizontal.append(horizontal_offset)
    anchor.append(horizontal)

    vertical = _drawing_element(WP_NS, "positionV")
    vertical.set("relativeFrom", "paragraph")
    vertical_offset = _drawing_element(WP_NS, "posOffset")
    vertical_offset.text = str(vertical_offset_emu)
    vertical.append(vertical_offset)
    anchor.append(vertical)

    extent = _drawing_element(WP_NS, "extent")
    extent.set("cx", str(width_emu))
    extent.set("cy", "12700")
    anchor.append(extent)

    effect_extent = _drawing_element(WP_NS, "effectExtent")
    for edge in ("l", "t", "r", "b"):
        effect_extent.set(edge, "0")
    anchor.append(effect_extent)
    anchor.append(_drawing_element(WP_NS, "wrapNone"))

    document_properties = _drawing_element(WP_NS, "docPr")
    document_properties.set("id", str(100 + shape_id))
    document_properties.set("name", f"PTC1 rule {shape_id}")
    anchor.append(document_properties)
    anchor.append(_drawing_element(WP_NS, "cNvGraphicFramePr"))

    graphic = _drawing_element(A_NS, "graphic")
    graphic_data = _drawing_element(A_NS, "graphicData")
    graphic_data.set(
        "uri",
        "http://schemas.microsoft.com/office/word/2010/wordprocessingShape",
    )
    shape = _drawing_element(WPS_NS, "wsp")
    shape.append(_drawing_element(WPS_NS, "cNvCnPr"))
    shape_properties = _drawing_element(WPS_NS, "spPr")
    transform = _drawing_element(A_NS, "xfrm")
    offset = _drawing_element(A_NS, "off")
    offset.set("x", "0")
    offset.set("y", "0")
    transform.append(offset)
    transform_extent = _drawing_element(A_NS, "ext")
    transform_extent.set("cx", str(width_emu))
    transform_extent.set("cy", "12700")
    transform.append(transform_extent)
    shape_properties.append(transform)
    geometry = _drawing_element(A_NS, "prstGeom")
    geometry.set("prst", "straightConnector1")
    geometry.append(_drawing_element(A_NS, "avLst"))
    shape_properties.append(geometry)
    shape_properties.append(_drawing_element(A_NS, "noFill"))
    line = _drawing_element(A_NS, "ln")
    line.set("w", "9525")
    line.set("cap", "flat")
    line.set("cmpd", "sng")
    solid_fill = _drawing_element(A_NS, "solidFill")
    color = _drawing_element(A_NS, "srgbClr")
    color.set("val", "000000")
    solid_fill.append(color)
    line.append(solid_fill)
    dash = _drawing_element(A_NS, "prstDash")
    dash.set("val", "solid")
    line.append(dash)
    line.append(_drawing_element(A_NS, "round"))
    shape_properties.append(line)
    shape.append(shape_properties)
    shape.append(_drawing_element(WPS_NS, "bodyPr"))
    graphic_data.append(shape)
    graphic.append(graphic_data)
    anchor.append(graphic)
    drawing.append(anchor)
    run._r.append(drawing)


def _remove_table_borders(table) -> None:
    properties = table._tbl.tblPr
    borders = properties.first_child_found_in("w:tblBorders")
    if borders is None:
        borders = OxmlElement("w:tblBorders")
        properties.append(borders)
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        element = borders.find(qn(f"w:{edge}"))
        if element is None:
            element = OxmlElement(f"w:{edge}")
            borders.append(element)
        element.set(qn("w:val"), "nil")


def _add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    run = paragraph.add_run()
    _set_run_font(run, size=12)
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instruction = OxmlElement("w:instrText")
    instruction.set(qn("xml:space"), "preserve")
    instruction.text = " PAGE "
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    display = OxmlElement("w:t")
    display.text = "2"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    run._r.extend((begin, instruction, separate, display, end))


def _configure_document(document) -> None:
    section = document.sections[0]
    section.start_type = WD_SECTION_START.NEW_PAGE
    section.page_width = Cm(PAGE_WIDTH_CM)
    section.page_height = Cm(PAGE_HEIGHT_CM)
    section.left_margin = Cm(3.0)
    section.right_margin = Cm(2.0)
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.header_distance = Cm(0.0)
    section.footer_distance = Cm(0.0)
    section.different_first_page_header_footer = True

    for header in (
        section.header,
        section.first_page_header,
        section.even_page_header,
    ):
        _clear_story_container(header)
    _add_page_number(section.header.paragraphs[0])
    for footer in (
        section.footer,
        section.first_page_footer,
        section.even_page_footer,
    ):
        _clear_story_container(footer)

    normal = document.styles["Normal"]
    normal.font.name = FONT_NAME
    normal.font.size = Pt(BODY_SIZE)
    normal_rpr = normal._element.get_or_add_rPr()
    normal_fonts = normal_rpr.get_or_add_rFonts()
    for font_key in ("ascii", "hAnsi", "eastAsia", "cs"):
        normal_fonts.set(qn(f"w:{font_key}"), FONT_NAME)
    normal.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    normal.paragraph_format.space_before = Pt(3)
    normal.paragraph_format.space_after = Pt(3)
    normal.paragraph_format.line_spacing = 1.0

    for level in range(1, 4):
        style_name = f"Meeting Heading {level}"
        if style_name in document.styles:
            style = document.styles[style_name]
        else:
            style = document.styles.add_style(style_name, WD_STYLE_TYPE.PARAGRAPH)
        style.font.name = FONT_NAME
        style.font.size = Pt(BODY_SIZE)
        style.font.bold = True
        style_rpr = style._element.get_or_add_rPr()
        style_fonts = style_rpr.get_or_add_rFonts()
        for font_key in ("ascii", "hAnsi", "eastAsia", "cs"):
            style_fonts.set(qn(f"w:{font_key}"), FONT_NAME)
        style.paragraph_format.keep_with_next = True
        style.paragraph_format.space_before = Pt(3)
        style.paragraph_format.space_after = Pt(3)
        style.paragraph_format.line_spacing = 1.0


def _add_cell_line(
    cell,
    text: str,
    *,
    size: float = BODY_SIZE,
    bold: bool = False,
    italic: bool = False,
    underline: bool = False,
    alignment=WD_ALIGN_PARAGRAPH.CENTER,
) -> None:
    paragraph = cell.paragraphs[0] if not cell.text else cell.add_paragraph()
    paragraph.alignment = alignment
    paragraph.paragraph_format.space_before = Pt(0)
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.line_spacing = 1.0
    run = paragraph.add_run(text)
    _set_run_font(run, size=size, bold=bold, italic=italic)
    run.underline = underline


def _add_institutional_header(document, report_date: date) -> None:
    table = document.add_table(rows=2, cols=2)
    _set_table_geometry_dxa(
        table,
        HEADER_TABLE_WIDTHS_DXA,
        alignment=None,
        zero_cell_margins=False,
    )
    _set_reference_header_position(table)
    _set_reference_cell_margins(table)
    _remove_table_borders(table)
    _set_row_height_dxa(table.rows[0], 1078)
    _set_row_height_dxa(table.rows[1], 506)

    left, right = table.rows[0].cells
    _add_cell_line(left, "TỔNG CÔNG TY", size=13)
    _add_cell_line(left, "TRUYỀN TẢI ĐIỆN QUỐC GIA", size=13)
    _add_cell_line(
        left,
        "CÔNG TY TRUYỀN TẢI ĐIỆN 1",
        size=13,
        bold=True,
    )
    _add_floating_rule(
        left.paragraphs[-1],
        width_emu=1682750,
        horizontal_offset_emu=508000,
        vertical_offset_emu=215900,
        shape_id=1,
    )
    _add_cell_line(
        right,
        "CỘNG HÒA XÃ HỘI CHỦ NGHĨA VIỆT NAM",
        size=13,
        bold=True,
    )
    right.paragraphs[-1].paragraph_format.line_spacing = 1.3333333333333333
    _add_cell_line(
        right,
        "Độc lập – Tự do – Hạnh phúc",
        size=14,
        bold=True,
    )
    right.paragraphs[-1].paragraph_format.line_spacing = 1.3333333333333333
    motto_rule = right.add_paragraph()
    motto_rule.alignment = WD_ALIGN_PARAGRAPH.CENTER
    motto_rule.paragraph_format.space_before = Pt(0)
    motto_rule.paragraph_format.space_after = Pt(0)
    motto_rule.paragraph_format.line_spacing = 1.3333333333333333
    _add_floating_rule(
        motto_rule,
        width_emu=1224280,
        horizontal_offset_emu=1092200,
        vertical_offset_emu=36830,
        shape_id=2,
    )

    number_cell, date_cell = table.rows[1].cells
    _add_cell_line(
        number_cell,
        "Số: [SỐ VĂN BẢN]/TB-PTC1",
        size=14,
        italic=True,
    )
    _add_cell_line(
        date_cell,
        (
            f"Hà Nội, ngày {report_date.day:02d} tháng {report_date.month:02d} "
            f"năm {report_date.year}"
        ),
        size=14,
        italic=True,
    )

    after = document.add_paragraph()
    after.alignment = WD_ALIGN_PARAGRAPH.CENTER
    after.paragraph_format.space_before = Pt(0)
    after.paragraph_format.space_after = Pt(0)
    after.paragraph_format.line_spacing = 1.0


def _add_formal_title(document, title: str) -> None:
    heading = document.add_paragraph()
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER
    heading.paragraph_format.space_before = Pt(0)
    heading.paragraph_format.space_after = Pt(0)
    heading.paragraph_format.line_spacing = 1.15
    heading.paragraph_format.keep_with_next = True
    _set_run_font(heading.add_run("THÔNG BÁO"), bold=True)

    subtitle = document.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_before = Pt(0)
    subtitle.paragraph_format.space_after = Pt(0)
    subtitle.paragraph_format.line_spacing = 1.15
    subtitle.paragraph_format.keep_with_next = True
    _set_run_font(subtitle.add_run(title), bold=True)

    rule = document.add_paragraph()
    rule.alignment = WD_ALIGN_PARAGRAPH.CENTER
    rule.paragraph_format.first_line_indent = Cm(1.0)
    rule.paragraph_format.space_before = Pt(3)
    rule.paragraph_format.space_after = Pt(3)
    rule.paragraph_format.line_spacing = 1.0
    rule.paragraph_format.keep_with_next = True
    _add_floating_rule(
        rule,
        width_emu=1677035,
        horizontal_offset_emu=1930400,
        vertical_offset_emu=105390,
        shape_id=3,
    )


def _add_inline_runs(paragraph, text: str, *, size: float = BODY_SIZE) -> None:
    cursor = 0
    for match in INLINE_RE.finditer(text):
        if match.start() > cursor:
            _set_run_font(paragraph.add_run(text[cursor : match.start()]), size=size)

        (
            bold_italic_text,
            bold_text,
            bold_underscore_text,
            italic_text,
            italic_underscore_text,
            code_text,
            link_text,
            link_url,
        ) = match.groups()
        if bold_italic_text is not None:
            _set_run_font(
                paragraph.add_run(bold_italic_text),
                size=size,
                bold=True,
                italic=True,
            )
        elif bold_text is not None or bold_underscore_text is not None:
            _set_run_font(
                paragraph.add_run(bold_text or bold_underscore_text),
                size=size,
                bold=True,
            )
        elif italic_text is not None or italic_underscore_text is not None:
            _set_run_font(
                paragraph.add_run(italic_text or italic_underscore_text),
                size=size,
                italic=True,
            )
        elif code_text is not None:
            run = paragraph.add_run(code_text)
            run.font.name = "Consolas"
            run.font.size = Pt(max(10, size - 2))
        else:
            label = link_text or link_url
            value = label if label == link_url else f"{label} ({link_url})"
            run = paragraph.add_run(value)
            _set_run_font(run, size=size)
            run.underline = True
        cursor = match.end()

    if cursor < len(text):
        _set_run_font(paragraph.add_run(text[cursor:]), size=size)


def _format_body_paragraph(paragraph, *, first_line: bool = True) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
    paragraph.paragraph_format.space_before = Pt(3)
    paragraph.paragraph_format.space_after = Pt(3)
    paragraph.paragraph_format.line_spacing = 1.0
    paragraph.paragraph_format.first_line_indent = Cm(1.0 if first_line else 0)


def _add_heading(document, text: str, level: int, *, title_slot: bool = False) -> None:
    paragraph = document.add_paragraph(style=f"Meeting Heading {min(level, 3)}")
    if level == 1 or title_slot:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_before = Pt(0)
        paragraph.paragraph_format.space_after = Pt(0 if level == 1 else 6)
    else:
        paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        paragraph.paragraph_format.space_before = Pt(3)
    _add_inline_runs(paragraph, text, size=BODY_SIZE)
    for run in paragraph.runs:
        run.bold = True


def _add_rule(document) -> None:
    paragraph = document.add_paragraph()
    properties = paragraph._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "4")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), "000000")
    borders.append(bottom)
    properties.append(borders)


def _split_table_row(line: str) -> list[str]:
    value = line.strip().strip("|")
    return [cell.strip() for cell in re.split(r"(?<!\\)\|", value)]


def _add_markdown_table(document, rows: list[list[str]]) -> None:
    if not rows:
        return
    column_count = max(len(row) for row in rows)
    table = document.add_table(rows=len(rows), cols=column_count)
    equal_width = CONTENT_WIDTH_CM / column_count
    _set_table_geometry(table, [equal_width] * column_count)
    table.style = "Table Grid"
    for row_index, values in enumerate(rows):
        for column_index in range(column_count):
            cell = table.cell(row_index, column_index)
            _set_cell_margins(cell, top=90, start=100, bottom=90, end=100)
            cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
            text = values[column_index] if column_index < len(values) else ""
            paragraph = cell.paragraphs[0]
            paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_before = Pt(0)
            paragraph.paragraph_format.space_after = Pt(0)
            _add_inline_runs(paragraph, text, size=12)
            if row_index == 0:
                _set_cell_shading(cell, "EDEDED")
                for run in paragraph.runs:
                    run.bold = True
    document.add_paragraph()


def _add_markdown(document, markdown: str) -> None:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    paragraph_buffer: list[str] = []
    index = 0
    section_started = False

    def flush_paragraph() -> None:
        if not paragraph_buffer:
            return
        text = " ".join(part.strip() for part in paragraph_buffer if part.strip())
        paragraph_buffer.clear()
        if not text:
            return
        paragraph = document.add_paragraph()
        _format_body_paragraph(
            paragraph,
            first_line=not section_started and not METADATA_RE.match(text),
        )
        _add_inline_runs(paragraph, text)

    while index < len(lines):
        line = lines[index].rstrip()
        stripped = line.strip()

        if not stripped:
            flush_paragraph()
            index += 1
            continue

        if stripped.startswith("```"):
            flush_paragraph()
            code_lines = []
            index += 1
            while index < len(lines) and not lines[index].strip().startswith("```"):
                code_lines.append(lines[index])
                index += 1
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(0.6)
            run = paragraph.add_run("\n".join(code_lines))
            run.font.name = "Consolas"
            run.font.size = Pt(10)
            index += 1
            continue

        heading = HEADING_RE.match(stripped)
        if heading:
            flush_paragraph()
            section_started = True
            _add_heading(
                document,
                heading.group(2).strip(" *_`"),
                len(heading.group(1)),
            )
            index += 1
            continue

        if stripped in {"---", "***", "___"}:
            flush_paragraph()
            _add_rule(document)
            index += 1
            continue

        if index + 1 < len(lines) and "|" in stripped and TABLE_RULE_RE.match(lines[index + 1]):
            flush_paragraph()
            rows = [_split_table_row(stripped)]
            index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                rows.append(_split_table_row(lines[index]))
                index += 1
            _add_markdown_table(document, rows)
            continue

        list_item = LIST_RE.match(line)
        if list_item:
            flush_paragraph()
            indent_level = min(4, len(list_item.group(1).replace("\t", "    ")) // 2)
            paragraph = document.add_paragraph()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            paragraph.paragraph_format.left_indent = Cm(0.8 + indent_level * 0.7)
            paragraph.paragraph_format.first_line_indent = Cm(-0.45)
            paragraph.paragraph_format.space_before = Pt(3)
            paragraph.paragraph_format.space_after = Pt(3)
            paragraph.paragraph_format.line_spacing = 1.0
            _set_run_font(paragraph.add_run("- "))
            _add_inline_runs(paragraph, list_item.group(2))
            index += 1
            continue

        if stripped.startswith(">"):
            flush_paragraph()
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Cm(1.0)
            paragraph.paragraph_format.right_indent = Cm(0.5)
            paragraph.paragraph_format.space_before = Pt(3)
            paragraph.paragraph_format.space_after = Pt(3)
            _add_inline_runs(paragraph, stripped.lstrip("> "), size=13)
            for run in paragraph.runs:
                run.italic = True
            index += 1
            continue

        if METADATA_RE.match(stripped):
            flush_paragraph()
            paragraph = document.add_paragraph()
            _format_body_paragraph(paragraph, first_line=False)
            _add_inline_runs(paragraph, stripped)
            index += 1
            continue

        paragraph_buffer.append(stripped)
        index += 1

    flush_paragraph()


def _strip_formal_markdown_title(markdown: str) -> str:
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is None:
        return ""
    match = HEADING_RE.match(lines[first].strip())
    if not match or len(match.group(1)) != 1 or match.group(2).strip().upper() != "THÔNG BÁO":
        return markdown

    del lines[first]
    second = next((index for index, line in enumerate(lines) if line.strip()), None)
    if second is not None:
        second_match = HEADING_RE.match(lines[second].strip())
        if second_match and len(second_match.group(1)) == 2:
            del lines[second]
    return "\n".join(lines).lstrip("\n")


def _add_closing_block(document) -> None:
    notification = document.add_paragraph()
    _format_body_paragraph(notification, first_line=True)
    notification.paragraph_format.line_spacing = 1.15
    notification.paragraph_format.keep_with_next = True
    _add_inline_runs(
        notification,
        (
            "Thừa lệnh Giám đốc, Văn phòng Công ty thông báo tới các Phòng chức năng "
            "trong Công ty, các đơn vị trực thuộc biết và thực hiện."
        ),
    )

    regards = document.add_paragraph()
    _format_body_paragraph(regards, first_line=True)
    regards.paragraph_format.line_spacing = 1.15
    regards.paragraph_format.keep_with_next = True
    _add_inline_runs(regards, "Trân trọng./.")

    table = document.add_table(rows=1, cols=2)
    _set_table_geometry_dxa(
        table,
        SIGNOFF_TABLE_WIDTHS_DXA,
        zero_cell_margins=False,
    )
    _set_reference_cell_margins(table)
    _remove_table_borders(table)
    left, right = table.rows[0].cells
    left.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP
    right.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.TOP

    _add_cell_line(
        left,
        "Nơi nhận:",
        size=14,
        bold=True,
        italic=True,
        alignment=WD_ALIGN_PARAGRAPH.LEFT,
    )
    for recipient in (
        "[NƠI NHẬN 1];",
        "[NƠI NHẬN 2];",
        "[NƠI NHẬN 3];",
        "[NƠI NHẬN 4];",
        "Lưu: VT,VP.",
    ):
        _add_cell_line(left, recipient, size=11, alignment=WD_ALIGN_PARAGRAPH.LEFT)
    _add_cell_line(left, " ", size=11, alignment=WD_ALIGN_PARAGRAPH.LEFT)

    _add_cell_line(right, "TL. GIÁM ĐỐC", bold=True)
    _add_cell_line(right, "[CHỨC DANH 1]", bold=True)
    _add_cell_line(right, "[CHỨC DANH 2]", bold=True)
    for _ in range(6):
        _add_cell_line(right, " ", size=11)
    _add_cell_line(right, "[HỌ VÀ TÊN]", bold=True)


def _add_transcript(document, markdown: str, title: str) -> None:
    _add_heading(document, "TRANSCRIPT CUỘC HỌP", 1, title_slot=True)
    if title:
        _add_heading(document, title, 2, title_slot=True)
    for raw_line in markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        paragraph = document.add_paragraph()
        _format_body_paragraph(paragraph, first_line=False)
        _add_inline_runs(paragraph, line, size=12)


def _new_document_from_template(template_path: Path | None = None):
    source = template_path or REPORT_TEMPLATE_PATH
    document = Document(source) if source and source.is_file() else Document()
    _clear_document_body(document)
    _configure_document(document)
    return document


def _set_clean_properties(document, title: str) -> None:
    properties = document.core_properties
    properties.title = title
    properties.subject = "Thông báo kết luận cuộc họp - PTC1"
    properties.author = "Công ty Truyền tải điện 1"
    properties.last_modified_by = "MeetNote"
    properties.comments = ""
    properties.keywords = ""


def export_markdown_to_docx(
    markdown: str,
    output: BinaryIO | BytesIO,
    *,
    title: str = "",
    document_type: str = "summary",
    report_date: date | datetime | None = None,
    template_path: Path | None = None,
) -> None:
    """Write summary/transcript Markdown as an editable `.docx` file."""
    normalized_title = title.strip() or "Kết luận cuộc họp"
    document = _new_document_from_template(template_path)
    _set_clean_properties(document, normalized_title)

    if document_type == "transcript":
        _add_transcript(document, markdown, normalized_title)
    else:
        effective_date = report_date.date() if isinstance(report_date, datetime) else report_date
        _add_institutional_header(document, effective_date or datetime.now().date())
        _add_formal_title(document, normalized_title)
        _add_markdown(document, _strip_formal_markdown_title(markdown))
        _add_closing_block(document)

    document.save(output)


def create_reusable_template(reference_path: str | Path, output_path: str | Path) -> None:
    """Create the clean reusable PTC1 template from the retained reference DOCX."""
    reference = Path(reference_path)
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as destination:
        export_markdown_to_docx(
            "[NỘI DUNG BIÊN BẢN ĐƯỢC CHÈN TẠI ĐÂY]",
            destination,
            title="[TÊN BÁO CÁO]",
            document_type="summary",
            template_path=reference,
        )
