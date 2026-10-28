"""共享工具：标题降级、图片占位符、文本拼接、编码读取。"""
from __future__ import annotations

import re
import unicodedata

IMG_TOKEN = "@@DMIMG:{:04d}@@"
IMG_TOKEN_RE = re.compile(r"@@DMIMG:(\d{4})@@")

MD_ESCAPE_CHARS = "\\*_`[]$~<>"


def escape_md(text: str) -> str:
    """转义会破坏 Markdown 语法的字符。

    只转义真正会造成解析歧义的行内符号；换行、空白不动。
    """
    if not text:
        return ""
    out = []
    for ch in text:
        if ch in MD_ESCAPE_CHARS:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def escape_table_cell(text: str) -> str:
    text = text.replace("\n", " ")
    text = text.replace("|", "\\|")
    return text


def shift_headings(md: str, delta: int = 1) -> str:
    """把 markdown 里所有 ATX 标题整体降 delta 级，最多到 H6。

    代码围栏（``` / ~~~）内的内容不做处理。
    """
    if delta == 0:
        return md
    out = []
    fence = None
    for line in md.split("\n"):
        stripped = line.lstrip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            out.append(line)
            continue
        if fence is None:
            m2 = re.match(r"^(#{1,6})(\s+\S.*)$", line)
            if m2:
                level = min(6, len(m2.group(1)) + delta)
                out.append("#" * level + m2.group(2))
                continue
        out.append(line)
    return "\n".join(out)


def demote_to_start_at_h1(md: str) -> str:
    """把文档内最高一级标题提升/降级到 H1 起，保持相对层级。

    例：全篇只有 ####  → 变成 #；同时 H1 与 H3 并存 → 按最小级别对齐到 1。
    """
    levels = []
    fence = None
    for line in md.split("\n"):
        stripped = line.lstrip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            continue
        if fence is None:
            m2 = re.match(r"^(#{1,6})\s+\S", line)
            if m2:
                levels.append(len(m2.group(1)))
    if not levels:
        return md
    top = min(levels)
    return shift_headings(md, 1 - top)


CJK_RE = re.compile(r"[\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]")


def _ends_cjk(s: str) -> bool:
    return bool(s) and bool(CJK_RE.match(s[-1]))


def _starts_cjk(s: str) -> bool:
    return bool(s) and bool(CJK_RE.match(s[0]))


def smart_join(parts: list[str]) -> str:
    """把多行拼成一段：中文之间不插空格，英文之间插一个空格。"""
    out = ""
    for raw in parts:
        piece = raw.strip()
        if not piece:
            continue
        if not out:
            out = piece
            continue
        if out.endswith("-") and piece[:1].islower():
            out = out[:-1] + piece
        elif _ends_cjk(out) or _starts_cjk(piece):
            out += piece
        else:
            out += " " + piece
    return out


def collapse_blanks(md: str, max_blank: int = 1) -> str:
    lines = md.split("\n")
    out = []
    blanks = 0
    for line in lines:
        if line.strip() == "":
            blanks += 1
            if blanks > max_blank:
                continue
            out.append("")
        else:
            blanks = 0
            out.append(line.rstrip())
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out)


def read_text_file(path: str) -> tuple[str, str]:
    """读文本文件，返回 (文本, 编码)。优先 utf-8 / utf-8-sig，再试 charset_normalizer。"""
    with open(path, "rb") as f:
        raw = f.read()
    for enc in ("utf-8-sig", "utf-8", "utf-16"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    try:
        from charset_normalizer import from_bytes

        best = from_bytes(raw).best()
        if best is not None:
            return str(best), best.encoding or "unknown"
    except Exception:
        pass
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def looks_like_markdown(text: str) -> bool:
    """粗略判断纯文本是不是 markdown：看 ATX 标题密度。"""
    lines = [l for l in text.split("\n") if l.strip()]
    if len(lines) < 3:
        return False
    heading = sum(1 for l in lines if re.match(r"^#{1,6}\s+\S", l.strip()))
    return heading >= max(2, len(lines) * 0.05)


def natural_key(name: str):
    """自然排序键：文件名里的数字按数值比较，其余按码点。"""
    name = unicodedata.normalize("NFC", name)
    parts = re.split(r"(\d+)", name.lower())
    key = []
    for i, p in enumerate(parts):
        if i % 2 == 1:
            key.append((0, int(p), ""))
        else:
            key.append((1, 0, p))
    return key


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024.0
    return f"{n:.1f}GB"
