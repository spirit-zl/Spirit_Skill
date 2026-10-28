"""Word (.docx) → Markdown。

用 python-docx 直接读 body，保持段落/表格原始顺序；标题取自段落样式或 outlineLvl。
"""
from __future__ import annotations

import os
import re

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from .base import DocResult, ImageSink
from .common import collapse_blanks, escape_md

HEADING_STYLE_RE = re.compile(r"^(?:heading|标题)\s*([1-9])$", re.I)


def _style_heading_level(p: Paragraph) -> int | None:
    try:
        name = (p.style.name or "").strip()
    except Exception:
        name = ""
    m = HEADING_STYLE_RE.match(name.replace(" ", ""))
    if m:
        return int(m.group(1))
    if name.lower() in ("title", "标题"):
        return 1
    # 中文模板常见「标题 1」写法已被上面的正则覆盖；再看 outlineLvl
    ppr = p._p.pPr
    if ppr is not None:
        lvl = ppr.find(qn("w:outlineLvl"))
        if lvl is not None:
            try:
                val = int(lvl.get(qn("w:val")))
                if 0 <= val < 9:
                    return val + 1
            except (TypeError, ValueError):
                pass
    return None


def _run_md(run_el, part, sink: ImageSink) -> str:
    chunks = []
    for child in run_el.iterchildren():
        tag = child.tag
        if tag == qn("w:t"):
            chunks.append(escape_md(child.text or ""))
        elif tag == qn("w:tab"):
            chunks.append(" ")
        elif tag == qn("w:br"):
            chunks.append(" ")
        elif tag == qn("w:drawing"):
            for blip in child.iter(qn("a:blip")):
                rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
                token = _export_image(part, rid, sink)
                if token:
                    chunks.append("\n\n" + token + "\n\n")
    text = "".join(chunks)
    if not text.strip():
        return text
    # 加粗/斜体按 run 级属性判断
    rpr = run_el.find(qn("w:rPr"))
    bold = italic = False
    if rpr is not None:
        b = rpr.find(qn("w:b"))
        i = rpr.find(qn("w:i"))
        bold = b is not None and b.get(qn("w:val")) not in ("0", "false")
        italic = i is not None and i.get(qn("w:val")) not in ("0", "false")
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    tail = text[len(text.rstrip()):]
    if bold and italic:
        return f"{lead}***{stripped}***{tail}"
    if bold:
        return f"{lead}**{stripped}**{tail}"
    if italic:
        return f"{lead}*{stripped}*{tail}"
    return text


def _export_image(part, rid, sink: ImageSink) -> str | None:
    if not rid:
        return None
    try:
        image_part = part.related_parts[rid]
    except KeyError:
        return None
    ext = os.path.splitext(str(image_part.partname))[1]
    try:
        return sink.save(image_part.blob, ext)
    except Exception:
        return None


def _paragraph_md(p: Paragraph, part, sink: ImageSink) -> str:
    chunks = []
    for child in p._p.iterchildren():
        if child.tag == qn("w:r"):
            chunks.append(_run_md(child, part, sink))
        elif child.tag == qn("w:hyperlink"):
            text = "".join(t.text or "" for t in child.iter(qn("w:t")))
            rid = child.get(qn("r:id"))
            href = ""
            if rid:
                try:
                    href = part.rels[rid].target_ref
                except (KeyError, AttributeError):
                    href = ""
            if href.startswith(("http://", "https://")) and text.strip():
                chunks.append(f"[{escape_md(text.strip())}]({href})")
            else:
                chunks.append(escape_md(text))
        elif child.tag == qn("w:br"):
            chunks.append(" ")
    return "".join(chunks)


def _list_info(p: Paragraph):
    """返回 (层级, 是否有序)；不是列表项返回 None。"""
    level = None
    ppr = p._p.pPr
    if ppr is not None:
        numpr = ppr.find(qn("w:numPr"))
        if numpr is not None:
            ilvl = numpr.find(qn("w:ilvl"))
            level = 0
            if ilvl is not None:
                try:
                    level = int(ilvl.get(qn("w:val")))
                except (TypeError, ValueError):
                    level = 0
    try:
        style = (p.style.name or "").lower()
    except Exception:
        style = ""
    ordered = "list number" in style
    if "list bullet" in style or "list number" in style:
        return (level or 0, ordered)
    if level is not None:
        return (level, ordered)
    return None


def _table_md(table: Table, part, sink: ImageSink) -> str:
    rows: list[list[str]] = []
    for row in table.rows:
        cells = []
        seen = set()
        for cell in row.cells:
            # 合并单元格会重复出现同一 tc，跳过重复
            key = id(cell._tc)
            if key in seen:
                cells.append("")
                continue
            seen.add(key)
            text_parts = []
            for p in cell.paragraphs:
                t = _paragraph_md(p, part, sink).strip()
                if t:
                    text_parts.append(t)
            cells.append(re.sub(r"\s*\n+\s*", " ", " ".join(text_parts)).replace("|", "\\|"))
        if any(c.strip() for c in cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    for r in rows[1:]:
        lines.append("| " + " | ".join(r) + " |")
    return "\n".join(lines)


def convert(path: str, sink: ImageSink) -> DocResult:
    result = DocResult()
    doc = Document(path)
    part = doc.part
    body = doc.element.body
    blocks: list[str] = []

    for child in body.iterchildren():
        if child.tag == qn("w:p"):
            p = Paragraph(child, doc)
            level = _style_heading_level(p)
            md = _paragraph_md(p, part, sink).strip()
            md = re.sub(r"\n{3,}", "\n\n", md).strip()
            md = re.sub(r"(?<=\S)[ \t]{2,}(?=\S)", " ", md)
            if level and md:
                plain = re.sub(r"^\*\*|\*\*$", "", md).strip()
                result.headings.append((level, plain))
                blocks.append("#" * min(level, 6) + " " + plain)
            elif md:
                list_info = _list_info(p)
                if list_info is not None:
                    list_level, ordered = list_info
                    bullet = "1. " if ordered else "- "
                    blocks.append("  " * list_level + bullet + md)
                else:
                    blocks.append(md)
        elif child.tag == qn("w:tbl"):
            table = Table(child, doc)
            md = _table_md(table, part, sink)
            if md:
                blocks.append(md)

    result.markdown = collapse_blanks("\n\n".join(blocks))
    result.image_count = sink.count
    props = doc.core_properties
    result.title = (props.title or "").strip()
    if not result.title and result.headings:
        result.title = result.headings[0][1]
    if not result.headings:
        result.warnings.append("文档没有使用标题样式（Heading/标题 N），全篇按正文输出")
    if props.author:
        result.meta["author"] = props.author
    return result
