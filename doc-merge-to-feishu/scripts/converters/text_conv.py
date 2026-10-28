"""Markdown / 纯文本 → Markdown。

.md：原样保留结构，只把本地图片收进 assets 并换成占位符；
.txt：按空行分段，块内断行按中英文规则拼接（不改写内容）。
"""
from __future__ import annotations

import os
import re
from urllib.parse import unquote

from .base import DocResult, ImageSink
from .common import collapse_blanks, looks_like_markdown, read_text_file, smart_join

IMG_RE = re.compile(r"!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+[\"'][^\"']*[\"'])?\s*\)")


def _dir(path: str) -> str:
    return os.path.dirname(os.path.abspath(path))


def _load_local(base_dir: str, src: str) -> str | None:
    src = src.strip()
    if src.startswith("<") and src.endswith(">"):
        src = src[1:-1]
    src = unquote(src.split("#")[0])
    if src.startswith(("http://", "https://", "data:")):
        return None
    candidate = os.path.normpath(os.path.join(base_dir, src))
    return candidate if os.path.isfile(candidate) else ""


def convert(path: str, sink: ImageSink, force_text: bool = False) -> DocResult:
    result = DocResult()
    text, enc = read_text_file(path)
    result.meta["encoding"] = enc
    base_dir = _dir(path)
    missing = []

    is_md = path.lower().endswith((".md", ".markdown")) or looks_like_markdown(text)

    if not is_md or force_text:
        stem = os.path.splitext(os.path.basename(path))[0]
        body = text.replace("\r\n", "\n").replace("\r", "\n")
        raw_lines = body.split("\n")
        # 纯文本常见：文件第一行就是标题，与文件名重复，去掉避免 H1 下面再出现一行同名
        for i, line in enumerate(raw_lines):
            if not line.strip():
                continue
            if re.sub(r"\s+", "", line) == re.sub(r"\s+", "", stem):
                raw_lines = raw_lines[i + 1:]
            break
        body = "\n".join(raw_lines)
        paragraphs = []
        for chunk in re.split(r"\n\s*\n", body):
            lines = [l for l in chunk.split("\n") if l.strip()]
            if not lines:
                continue
            if len(lines) == 1:
                paragraphs.append(lines[0].strip())
            elif sum(len(l) for l in lines) / len(lines) > 30:
                paragraphs.append(smart_join(lines))
            else:
                paragraphs.extend(l.strip() for l in lines)
        # 纯文本里行首的 # 会被 Markdown 当成标题，转义掉
        paragraphs = [re.sub(r"^(\s*)#", r"\1\\#", p) for p in paragraphs]
        result.markdown = collapse_blanks("\n\n".join(paragraphs))
        result.meta["kind"] = "text"
        return result

    def repl(m: re.Match) -> str:
        alt, src = m.group(1), m.group(2)
        if src.strip().startswith(("http://", "https://")):
            return m.group(0)
        local = _load_local(base_dir, src)
        if local is None:
            return m.group(0)
        if local == "":
            missing.append(src)
            return ""
        ext = os.path.splitext(local)[1].lstrip(".") or "png"
        try:
            with open(local, "rb") as f:
                blob = f.read()
        except OSError:
            missing.append(src)
            return ""
        token = sink.save(blob, ext, alt)
        return token or ""

    md = IMG_RE.sub(repl, text)

    # 去掉 YAML frontmatter（Obsidian 等工具的元数据，不属于正文）
    fm = re.match(r"\A---\s*\n(.*?)\n---\s*\n", md, re.S)
    if fm and ":" in fm.group(1):
        md = md[fm.end():]

    # 图片在独立行时清掉行首尾多余空白
    md = re.sub(r"\n{3,}", "\n\n", md)
    result.markdown = collapse_blanks(md)
    result.meta["kind"] = "markdown"
    if missing:
        preview = "、".join(missing[:3])
        result.warnings.append(f"{len(missing)} 张本地图片找不到，已略过（{preview}…）")
    headings = []
    fence = None
    for line in md.split("\n"):
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
    if not headings:
        result.warnings.append("原文件没有 Markdown 标题")
    return result
