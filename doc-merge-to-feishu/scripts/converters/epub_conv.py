"""Epub → Markdown。

只用标准库 zipfile + lxml 解析（不依赖 ebooklib）：
container.xml → .opf → spine 顺序 → 每个 XHTML 用 HtmlToMd 转。
"""
from __future__ import annotations

import posixpath
import re
import zipfile

from lxml import etree

from .base import DocResult, ImageSink
from .common import collapse_blanks
from .htmlmd import HtmlToMd

NS = {
    "c": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"}
SKIP_TITLE_WORDS = ("目录", "目錄", "contents", "cover", "封面", "titlepage", "nav")


def _read_xml(zf: zipfile.ZipFile, name: str):
    return etree.fromstring(zf.read(name))


def _join(base_dir: str, href: str) -> str:
    href = href.split("#")[0]
    if href.startswith("/"):
        return href.lstrip("/")
    return posixpath.normpath(posixpath.join(base_dir, href))


def convert(path: str, sink: ImageSink) -> DocResult:
    result = DocResult()
    warnings: list[str] = []
    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        try:
            container = _read_xml(zf, "META-INF/container.xml")
            rootfile = container.find(".//c:rootfile", NS)
            opf_path = rootfile.get("full-path") if rootfile is not None else None
        except Exception:
            opf_path = None
        if not opf_path:
            opf_candidates = [n for n in names if n.lower().endswith(".opf")]
            opf_path = opf_candidates[0] if opf_candidates else None
        if not opf_path or opf_path not in names:
            result.warnings.append("不是有效的 EPUB：找不到 package 文件")
            return result

        opf_dir = posixpath.dirname(opf_path)
        opf = _read_xml(zf, opf_path)

        title_el = opf.find(".//dc:title", NS)
        if title_el is not None and title_el.text:
            result.title = title_el.text.strip()

        manifest = {}
        for item in opf.findall(".//opf:manifest/opf:item", NS):
            item_id = item.get("id")
            if not item_id:
                continue
            manifest[item_id] = {
                "href": item.get("href", ""),
                "type": (item.get("media-type") or "").lower(),
                "props": (item.get("properties") or "").lower(),
            }

        spine_ids = [ref.get("idref") for ref in opf.findall(".//opf:spine/opf:itemref", NS) if ref.get("idref")]
        # linear="no" 的条目通常是封面/版权，跳过
        non_linear = {
            ref.get("idref")
            for ref in opf.findall(".//opf:spine/opf:itemref", NS)
            if (ref.get("linear") or "yes").lower() == "no"
        }

        order = []
        seen_href = set()
        for item_id in spine_ids:
            item = manifest.get(item_id)
            if not item or item_id in non_linear:
                continue
            if "nav" in item["props"]:
                continue
            if item["type"] not in ("application/xhtml+xml", "text/html", "application/html"):
                continue
            href = _join(opf_dir, item["href"])
            if href in seen_href or href not in names:
                continue
            if any(word in href.lower() for word in ("nav.xhtml", "toc.xhtml", "cover.xhtml")):
                continue
            seen_href.add(href)
            order.append(href)

        if not order:
            result.warnings.append("EPUB 的 spine 里没有可读章节")
            return result

        def make_handler(base_href: str):
            def handler(src: str, alt: str):
                target = _join(posixpath.dirname(base_href), src)
                if target not in names:
                    # 尝试同目录直接拼
                    alt_target = posixpath.normpath(posixpath.join(posixpath.dirname(base_href), src.lstrip("/")))
                    if alt_target in names:
                        target = alt_target
                    else:
                        return None
                ext = posixpath.splitext(target)[1].lstrip(".").lower()
                if ext not in IMAGE_EXT:
                    return None
                try:
                    return sink.save(zf.read(target), ext, alt)
                except Exception:
                    return None

            return handler

        sections: list[str] = []
        for href in order:
            try:
                raw = zf.read(href)
            except Exception:
                continue
            chapter = HtmlToMd(image_handler=make_handler(href)).convert(raw)
            if not chapter.strip():
                continue
            # 只有零星几个字的章节（封面文字之类）丢掉
            if len(chapter) < 20 and not chapter.lstrip().startswith("#"):
                continue
            sections.append(chapter)

    result.markdown = collapse_blanks("\n\n".join(sections))
    result.image_count = sink.count
    headings = []
    for section in sections:
        fence = None
        for line in section.split("\n"):
            s = line.lstrip()
            m = re.match(r"^(`{3,}|~{3,})", s)
            if m:
                marker = m.group(1)[0]
                if fence is None:
                    fence = marker
                elif fence == marker:
                    fence = None
                continue
            if fence is None:
                m2 = re.match(r"^(#{1,6})\s+(.+)$", line)
                if m2:
                    headings.append((len(m2.group(1)), m2.group(2).strip()))
    result.headings = headings
    total = len(sections)
    if total == 0:
        result.warnings.append("EPUB 章节都解析为空")
    if not headings:
        result.warnings.append("EPUB 章节里没有 h1–h6 标题，全篇按正文输出")
    result.meta["chapters"] = total
    result.warnings.extend(warnings)
    return result
