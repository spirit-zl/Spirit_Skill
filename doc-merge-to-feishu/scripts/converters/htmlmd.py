"""HTML / XHTML → Markdown 转换（Epub 章节、网页片段共用）。

不依赖 markdownify/html2text，只用 beautifulsoup4 + lxml。
"""
from __future__ import annotations

import re
import warnings

from bs4 import NavigableString, Tag

from .common import escape_md, escape_table_cell

try:  # bs4 把 XHTML 当 HTML 解析时的提示，对 epub 场景是预期行为
    from bs4 import XMLParsedAsHTMLWarning

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
except Exception:  # pragma: no cover
    pass

BLOCK_TAGS = {
    "address", "article", "aside", "blockquote", "body", "center", "dd", "details",
    "div", "dl", "dt", "fieldset", "figcaption", "figure", "footer", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav",
    "ol", "p", "pre", "section", "summary", "table", "tbody", "td", "tfoot",
    "th", "thead", "tr", "ul",
}

SKIP_TAGS = {"script", "style", "noscript", "svg", "head", "title", "meta", "link"}

INLINE_BLOCK_RE = re.compile(r"[ \t\r\n]+")
CJK = r"\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"


def tidy_inline(s: str) -> str:
    s = INLINE_BLOCK_RE.sub(" ", s)
    s = re.sub(rf"(?<=[{CJK}])\s+(?=[{CJK}])", "", s)
    s = re.sub(rf"(?<=[{CJK}])\s+(?=[，。；：、！？）》」』”’])", "", s)
    s = re.sub(rf"(?<=[（《「『“‘])\s+", "", s)
    return s.strip()


class HtmlToMd:
    """把 HTML 片段转成 Markdown。

    image_handler(src, alt) -> str | None：返回图片占位符；返回 None 表示丢弃该图。
    """

    def __init__(self, image_handler=None):
        self.image_handler = image_handler or (lambda src, alt: None)

    # ---------- 公共入口 ----------

    def convert(self, html: str) -> str:
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        for tag in soup.find_all(list(SKIP_TAGS)):
            tag.decompose()
        root = soup.body or soup
        blocks = self._blocks(root)
        text = "\n\n".join(b for b in blocks if b and b.strip())
        return text.strip()

    # ---------- 块级 ----------

    def _blocks(self, el) -> list[str]:
        out: list[str] = []
        buf: list = []

        def flush():
            if buf:
                text = tidy_inline(self._inline_nodes(buf))
                if text:
                    out.append(text)
                buf.clear()

        for child in el.children:
            if isinstance(child, NavigableString):
                if child.strip():
                    buf.append(child)
                continue
            name = child.name.lower() if child.name else ""
            if name in SKIP_TAGS:
                continue
            if name in BLOCK_TAGS or name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                flush()
                out.extend(self._block(child))
            else:
                buf.append(child)
        flush()
        return out

    def _block(self, tag: Tag) -> list[str]:
        name = tag.name.lower()
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            text = tidy_inline(self._inline_nodes(list(tag.children)))
            text = re.sub(r"\s*\n+\s*", " ", text).strip()
            if not text:
                return []
            return ["#" * level + " " + text]
        if name == "p":
            text = tidy_inline(self._inline_nodes(list(tag.children)))
            return [text] if text else []
        if name in ("ul", "ol"):
            return self._list(tag, 0)
        if name == "li":
            return self._list_item(tag, 0, False, 1)
        if name == "pre":
            code = tag.get_text()
            code = code.replace("\r\n", "\n").strip("\n")
            if not code.strip():
                return []
            lang = ""
            cls = " ".join(tag.get("class") or [])
            m = re.search(r"language-([\w+#-]+)", cls)
            if m:
                lang = m.group(1)
            return ["```" + lang + "\n" + code + "\n```"]
        if name == "blockquote":
            inner = self._blocks(tag)
            body = "\n\n".join(inner)
            if not body.strip():
                return []
            return ["\n".join("> " + line if line.strip() else ">" for line in body.split("\n"))]
        if name == "table":
            table = self._table(tag)
            return [table] if table else []
        if name == "hr":
            return ["---"]
        if name == "figure":
            parts = []
            for child in tag.children:
                if isinstance(child, Tag) and child.name.lower() == "img":
                    token = self._image(child)
                    if token:
                        parts.append(token)
                elif isinstance(child, Tag) and child.name.lower() == "figcaption":
                    cap = tidy_inline(self._inline_nodes(list(child.children)))
                    if cap:
                        parts.append(f"*{cap}*")
                elif isinstance(child, Tag):
                    parts.extend(self._block(child))
            return parts
        if name == "img":
            token = self._image(tag)
            return [token] if token else []
        if name == "br":
            return []
        if name in ("div", "section", "article", "main", "body", "center", "details",
                    "summary", "header", "footer", "form", "fieldset", "address"):
            return self._blocks(tag)
        if name in ("dl", "dt", "dd"):
            return self._blocks(tag)
        text = tidy_inline(self._inline_nodes(list(tag.children)))
        return [text] if text else []

    def _list(self, tag: Tag, depth: int) -> list[str]:
        ordered = tag.name.lower() == "ol"
        items = tag.find_all("li", recursive=False)
        out: list[str] = []
        idx = 1
        for li in items:
            out.extend(self._list_item(li, depth, ordered, idx))
            idx += 1
        return out

    def _list_item(self, li: Tag, depth: int, ordered: bool, idx: int) -> list[str]:
        # 把 li 内容拆成「行内正文」+「嵌套列表」
        inline_parts: list = []
        nested: list[Tag] = []
        for child in li.children:
            if isinstance(child, Tag) and child.name.lower() in ("ul", "ol"):
                nested.append(child)
            else:
                inline_parts.append(child)
        text = tidy_inline(self._inline_nodes(inline_parts))
        text = re.sub(r"\s*\n+\s*", " ", text).strip()
        indent = "  " * depth
        bullet = f"{idx}. " if ordered else "- "
        out = []
        if text:
            out.append(indent + bullet + text)
        elif nested:
            pass
        for sub in nested:
            out.extend(self._list(sub, depth + 1))
        return out

    def _table(self, tag: Tag) -> str:
        rows: list[list[str]] = []
        for tr in tag.find_all("tr"):
            # 跳过嵌套表格里的行
            parent = tr.find_parent("table")
            if parent is not tag:
                continue
            cells = []
            for cell in tr.find_all(["td", "th"], recursive=False):
                cells.append(escape_table_cell(tidy_inline(self._inline_nodes(list(cell.children)))))
            if cells:
                rows.append(cells)
        if not rows:
            return ""
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        header = rows[0]
        body = rows[1:]
        lines = ["| " + " | ".join(header) + " |", "|" + "|".join([" --- "] * width) + "|"]
        for r in body:
            lines.append("| " + " | ".join(r) + " |")
        return "\n".join(lines)

    # ---------- 行内 ----------

    def _inline_nodes(self, nodes) -> str:
        return "".join(self._inline(n) for n in nodes)

    def _inline(self, node) -> str:
        if isinstance(node, NavigableString):
            return escape_md(str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower() if node.name else ""
        if name in SKIP_TAGS:
            return ""
        if name == "br":
            return " "
        if name == "img":
            return self._image(node) or ""
        if name in ("strong", "b"):
            inner = self._inline_nodes(list(node.children))
            return f"**{inner}**" if inner.strip() else inner
        if name in ("em", "i"):
            inner = self._inline_nodes(list(node.children))
            return f"*{inner}*" if inner.strip() else inner
        if name in ("del", "s", "strike"):
            inner = self._inline_nodes(list(node.children))
            return f"~~{inner}~~" if inner.strip() else inner
        if name == "code":
            raw = node.get_text().replace("\n", " ")
            return "`" + raw + "`" if raw.strip() else ""
        if name == "a":
            href = (node.get("href") or "").strip()
            inner = self._inline_nodes(list(node.children)).strip()
            if href.startswith(("http://", "https://")) and inner:
                return f"[{inner}]({href})"
            return inner
        if name in ("sup", "sub", "span", "small", "u", "mark", "abbr", "cite", "q", "tt"):
            return self._inline_nodes(list(node.children))
        if name in ("p", "div"):
            inner = self._inline_nodes(list(node.children)).strip()
            return f" {inner} " if inner else ""
        return self._inline_nodes(list(node.children))

    def _image(self, tag: Tag) -> str | None:
        src = (tag.get("src") or tag.get("data-src") or "").strip()
        alt = (tag.get("alt") or "").strip()
        if not src:
            return None
        return self.image_handler(src, alt)
