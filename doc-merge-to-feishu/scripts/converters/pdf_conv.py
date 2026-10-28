"""PDF → Markdown。

文本组装按「逻辑行」做，不是按 block：PDF 里编号列表的序号、行尾被挤出的字
常常各自成一个 block，按 block 拼会得到「正文 → 2. → 封。」这种错位。
这里先把同一水平位置的碎片按 x 拼成一行，再按行距合并成段落。

标题来源优先级：
1. PDF 内置书签（TOC）；
2. 字号启发式 —— 明显大于正文的短行视为标题，按字号分档映射层级。

页眉页脚（跨页重复出现的页边文本）和页码会被丢掉；扫描页交给 OCR。

表格：这些 PDF 大多由 Markdown 导出，PyMuPDF 的 find_tables 会把整页文本
误判成一张大表，因此改用「列对齐」自研检测——同一组列位置在连续多行里
重复出现（且不少于两列、且不是单纯的列表符号）即判定为表格，重建为
GFM 表格写进 Markdown，并把这部分区域从正文流里剔除，避免重复。
"""
from __future__ import annotations

import re
import statistics
import sys
from collections import Counter, defaultdict

import pymupdf

from .base import DocResult, ImageSink
from .common import collapse_blanks, escape_md, smart_join

CJK = r"\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
PAGE_NUM_RE = re.compile(
    r"^[\s\-—–·]*(?:(?:第\s*)?\d+\s*(?:页)?|\d+\s*/\s*\d+|page\s*\d+(\s*of\s*\d+)?)[\s\-—–·]*$",
    re.I,
)
LIST_MARKER_RE = re.compile(r"^(?:\d{1,3}[.、)]|[•·▪◦●○–—-])\s*$")
# 中英排版里「1.正文」「•正文」常常没有空格，必须容忍零空格；但要求符号后面确有内容，
# 否则单独的符号会被误当成列表开头。
LIST_START_RE = re.compile(r"^(?:\d{1,3}[.、)]\s*(?=\S)|[•·▪◦●○]\s*(?=\S))")
ORDERED_RE = re.compile(r"^(\d{1,3})[.、)]\s*(.*)$")
BULLET_RE = re.compile(r"^[•·▪◦●○]\s*(.+)$")
SENTENCE_END = "。！？；：.!?;:"
HEADING_TAIL = "。！？；.!?;，,"
HEADING_MAX_LEN = 40
TOC_TITLE_MIN = 2


def _norm(s: str) -> str:
    s = s.replace("\u3000", " ")
    s = re.sub(r"\s+", "", s)
    return s.strip().lower()


# ---------------------------------------------------------------- 采集


def _collect(doc, max_pages):
    """只收文本和图片位置，不解码图片字节（图片按 xref 惰性提取，扫描件才不占内存）。"""
    pages = []
    heights = []
    widths = []
    for pno in range(min(doc.page_count, max_pages or doc.page_count)):
        page = doc[pno]
        data = page.get_text("dict", flags=pymupdf.TEXTFLAGS_TEXT)
        blocks = list(data.get("blocks", []))
        try:
            for info in page.get_image_info(xrefs=True):
                blocks.append({"type": 1, "bbox": info.get("bbox"), "xref": info.get("xref")})
        except Exception:
            pass
        blocks.sort(key=lambda b: (b.get("bbox") or (0, 0, 0, 0))[1])
        pages.append(blocks)
        heights.append(page.rect.height)
        widths.append(page.rect.width)
    return pages, heights, widths


def _page_fragments(blocks) -> list[dict]:
    """每个文字 span 一条碎片，带精确 bbox，供列对齐表格检测用。"""
    frags = []
    for block in blocks:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                t = span.get("text", "").strip()
                if not t:
                    continue
                bbox = span.get("bbox")
                if not bbox:
                    continue
                frags.append({
                    "text": t,
                    "bbox": tuple(bbox),
                    "size": round(span.get("size", 0), 1),
                    "bold": bool(span.get("flags", 0) & 2 ** 4),
                })
    return frags


def _inside(rect, x0, y0, x1, y1) -> bool:
    cx = (x0 + x1) / 2
    cy = (y0 + y1) / 2
    return rect[0] <= cx <= rect[2] and rect[1] <= cy <= rect[3]


def _find_gutter(frags: list[dict], page_width: float) -> float | None:
    """找双栏排版的版心间隙：一条极宽的、几乎没有文字跨过的竖带。

    双栏页面如果按行左右读取，两栏内容会逐行交错（标题被粘成
    「误区一…误区二…」）。找到这条竖带，就能先读完左栏再读右栏。

    判定要严：封面大字、居中标题、宽表都会造成假间隙。除了「没人跨过」，
    还要求左右两侧在多数行里同时有字——这才是真正的双栏。
    """
    if len(frags) < 12 or not page_width:
        return None
    # 先按 y 聚行，供「两侧同时有字」的判定使用
    heights = sorted(f["bbox"][3] - f["bbox"][1] for f in frags)
    med_h = statistics.median(heights) or 10
    row_tol = max(2.0, med_h * 0.6)
    rows: list[list[dict]] = []
    cur: list[dict] = []
    cur_top = None
    for f in sorted(frags, key=lambda x: x["bbox"][1]):
        y0 = f["bbox"][1]
        if cur_top is None or y0 - cur_top <= row_tol:
            cur.append(f)
            cur_top = y0 if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur = [f]
            cur_top = y0
    if cur:
        rows.append(cur)
    if len(rows) < 4:
        return None

    band = 6.0
    best = None
    for x in range(int(page_width * 0.28), int(page_width * 0.72) + 1, 2):
        cross = sum(1 for f in frags if f["bbox"][0] < x + band and f["bbox"][2] > x - band)
        if cross > max(1, len(frags) * 0.01):
            continue
        left = [f for f in frags if f["bbox"][2] <= x - band]
        right = [f for f in frags if f["bbox"][0] >= x + band]
        if len(left) < max(8, len(frags) * 0.2) or len(right) < max(8, len(frags) * 0.2):
            continue
        both = 0
        for row in rows:
            has_l = any(f["bbox"][2] <= x - band for f in row)
            has_r = any(f["bbox"][0] >= x + band for f in row)
            if has_l and has_r:
                both += 1
        if both < len(rows) * 0.45:
            continue
        score = (both, -cross, min(len(left), len(right)))
        if best is None or score > best[0]:
            best = (score, float(x))
    return best[1] if best else None


def _page_lines(frags, page_width, exclude=None, _split: bool = True) -> list[dict]:
    """把碎片按 y 聚成逻辑行；同一行内按 x 聚成版块（双栏用竖线保留）。

    exclude 里矩形覆盖的碎片直接丢弃（表格区域已由表格处理器接管）。
    """
    if exclude is None:
        exclude = []
    if not frags:
        return []

    # 双栏：先切左右，再各自成行，最后按「左栏全部 → 右栏全部」拼接
    if _split:
        gutter = _find_gutter(frags, page_width)
        if gutter is not None:
            left = [f for f in frags if (f["bbox"][0] + f["bbox"][2]) / 2 < gutter]
            right = [f for f in frags if (f["bbox"][0] + f["bbox"][2]) / 2 >= gutter]
            if left and right:
                return (
                    _page_lines(left, page_width, exclude, _split=False)
                    + _page_lines(right, page_width, exclude, _split=False)
                )

    heights = [b[3] - b[1] for _, _, _, b in ((f["text"], 0, 0, f["bbox"]) for f in frags)]
    med_h = statistics.median(heights) if heights else 10
    row_tol = max(2.0, med_h * 0.6)

    # 按 y 聚行
    rows: list[list[dict]] = []
    cur: list[dict] = []
    cur_top = None
    for f in sorted(frags, key=lambda x: x["bbox"][1]):
        y0 = f["bbox"][1]
        if cur_top is None or y0 - cur_top <= row_tol:
            cur.append(f)
            cur_top = y0 if cur_top is None else cur_top
        else:
            rows.append(cur)
            cur = [f]
            cur_top = y0
    if cur:
        rows.append(cur)

    out: list[dict] = []
    two_zone_rows = 0
    counted_rows = 0
    for row in rows:
        # 跳过被表格覆盖的行
        if any(_inside(r, row[0]["bbox"][0], row[0]["bbox"][1],
                       max(x["bbox"][2] for x in row), max(x["bbox"][3] for x in row))
               for r in exclude):
            continue
        row.sort(key=lambda x: x["bbox"][0])
        # 按水平间隙切版块
        zones: list[list[dict]] = [[row[0]]]
        for prev, curf in zip(row, row[1:]):
            gap = curf["bbox"][0] - prev["bbox"][2]
            if page_width and gap > page_width * 0.30:
                zones.append([curf])
            else:
                zones[-1].append(curf)
        counted_rows += 1
        if len(zones) == 2:
            two_zone_rows += 1
        for zi, zone in enumerate(zones):
            texts = [z["text"].strip() for z in zone if z["text"].strip()]
            if not texts:
                continue
            # 仅当两个片段是「列表符号 + 正文」这种固定两栏时才特殊处理，
            # 其余一律 smart_join；真正的多列表格已由 _detect_tables 单独处理。
            if len(texts) == 2 and LIST_MARKER_RE.match(texts[0]):
                text = texts[0] + " " + texts[1]
            else:
                text = smart_join(texts)
            bbox = (
                min(z["bbox"][0] for z in zone),
                min(z["bbox"][1] for z in zone),
                max(z["bbox"][2] for z in zone),
                max(z["bbox"][3] for z in zone),
            )
            out.append({
                "text": text,
                "bbox": bbox,
                "size": max(z["size"] for z in zone),
                "bold": all(z["bold"] for z in zone),
            })

    return sorted(out, key=lambda l: (l["bbox"][1], l["bbox"][0]))


# ---------------------------------------------------------------- 表格检测

_ORDERED_CELL_RE = re.compile(r"^(\d{1,3})[.、)]\s*(.*)$")
_BULLET_CELL_RE = re.compile(r"^[•·▪◦●○]\s*(.+)$")


def _grid_from_geometry(t, frags: list[dict]):
    """按表格的「行框 + 列框」从原始文字碎片重建网格。

    find_tables 的 extract() 在单元格跨行回绕时会把内容摊错行（密集报告里尤其
    明显，整行被拼成一长段文字）。这里只用它给出的几何（每行的 y 区间、每列的
    x 区间），内容从 frags 按坐标重新归位，跨行回绕的单元格自然接回去。

    返回 (grid, used_idx)。
    """
    try:
        row_boxes = [tuple(r.bbox) for r in t.rows]
        best = max(t.rows, key=lambda r: len(r.cells or []))
        col_boxes = sorted([tuple(c) for c in best.cells if c], key=lambda b: b[0])
    except Exception:
        return [], []
    if len(row_boxes) < 2 or len(col_boxes) < 2:
        return [], []

    grid: list[list[str]] = []
    used: list[int] = []
    for rb in row_boxes:
        ry0, ry1 = rb[1], rb[3]
        cells: list[list[str]] = [[] for _ in col_boxes]
        for fi, f in enumerate(frags):
            cx = (f["bbox"][0] + f["bbox"][2]) / 2
            cy = (f["bbox"][1] + f["bbox"][3]) / 2
            if not (ry0 - 1 <= cy <= ry1 + 1):
                continue
            for ci, cb in enumerate(col_boxes):
                if cb[0] - 3 <= cx <= cb[2] + 3:
                    cells[ci].append(f["text"])
                    used.append(fi)
                    break
        grid.append([smart_join(parts) if parts else "" for parts in cells])
    return grid, used


def _merge_wrapped_rows(grid: list[list[str]]) -> list[list[str]]:
    """把「同一逻辑行被拆成的多条视觉行」合回一行。

    判据：相邻两行各自填了的列互不重叠（说明它们是同一行里不同列的折行），
    就合并，同列内容按顺序拼接。
    """
    if not grid:
        return grid
    ncol = len(grid[0])
    out = [grid[0][:]]
    for row in grid[1:]:
        prev = out[-1]
        prev_cols = {c for c in range(ncol) if prev[c].strip()}
        cur_cols = {c for c in range(ncol) if row[c].strip()}
        if cur_cols and prev_cols and not (prev_cols & cur_cols):
            for c in cur_cols:
                prev[c] = smart_join([prev[c], row[c]]) if prev[c] else row[c]
            continue
        out.append(row[:])
    return out


def _clean_grid(cells, page_h: float, rows_bbox=None):
    """把一个 find_tables 的网格清洗成若干「真·表格」子块。

    - 去掉全空列；
    - 只保留连续多行里每行都有 ≥2 个非空单元格的段落（单元格会跨行回绕，
      所以按单元格是否有内容判断，而不是按视觉行）；
    - 过滤掉正文式的伪表：单元格太长（像段落）、或两列且左列基本是列表符号
      （那其实是编号/项目列表，交给正文列表逻辑处理）。

    返回 [(grid, kept_row_indices)]；kept_row_indices 用于按行坐标认领文字碎片。
    """
    if not cells:
        return []
    grid = [[(x or "").strip() for x in r] for r in cells]
    return [(sub, run) for sub, run in _filter_grid(grid)]


def _filter_grid(grid: list[list[str]]):
    """对字符串网格做清洗：丢空列、切出连续的真实表格行、过滤伪表。

    返回 [(grid, kept_row_indices)]；kept_row_indices 用于按行坐标认领文字碎片。
    """
    if not grid:
        return []
    nrows = len(grid)
    ncols = len(grid[0]) if grid else 0
    if ncols < 2:
        return []
    nec = [c for c in range(ncols) if any(grid[r][c] for r in range(nrows))]
    if len(nec) < 2:
        return []
    grid = [[row[c] for c in nec] for row in grid]

    def _med_len(g):
        ls = [len(" ".join(r).strip()) for r in g if any(r)]
        return statistics.median(ls) if ls else 0

    runs: list[list[int]] = []
    cur: list[int] = []
    for r in range(len(grid)):
        ne = sum(1 for c in range(len(nec)) if grid[r][c])
        if ne >= 2:
            cur.append(r)
        else:
            if cur:
                runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    out = []
    for run in runs:
        if len(run) < 2:
            continue
        sub = [grid[r] for r in run]
        # 丢掉该子表内「多数行为空」的列（PyMuPDF 网格常带装饰性空列）
        nrow = len(sub)
        keep = [c for c in range(len(sub[0]))
                if sum(1 for r in range(nrow) if sub[r][c].strip()) > nrow * 0.5]
        if len(keep) < 2:
            keep = [c for c in range(len(sub[0])) if any(sub[r][c].strip() for r in range(nrow))]
        if len(keep) < 2:
            continue
        sub = [[row[c] for c in keep] for row in sub]
        ncol = len(sub[0])
        ml = _med_len(sub)
        if ncol == 2:
            # 两列是最容易误判的形态：正文（或列表）只有两栏。
            # 要求单元格短、且左列不是编号/项目符号。
            if ml > 38:
                continue
            left = [sub[r][0] for r in range(nrow)]
            mk = sum(1 for x in left if _ORDERED_CELL_RE.match(x) or _BULLET_CELL_RE.match(x) or len(x) <= 2)
            if mk >= 0.6 * len(left):
                continue
        else:
            # 三列以上基本都是真表格（密集排版报告里单元格会很长，不能按长度卡）
            if ml > 200:
                continue
            filled = sum(1 for r in range(nrow) for c in range(ncol) if sub[r][c])
            if filled < nrow * ncol * 0.5:
                continue
        out.append((sub, run))
    return out


def _build_md(grid: list[list[str]]) -> str:
    norm = [[c.replace("\n", " ").strip() for c in row] for row in grid]
    norm = [[escape_md(c).replace("|", "\\|") for c in row] for row in norm]
    norm = [row for row in norm if any(row)]
    if len(norm) < 2:
        return ""
    lines = [" | ".join(norm[0]), " | ".join(["---"] * len(norm[0]))]
    lines += [" | ".join(row) for row in norm[1:]]
    return "\n".join("| " + ln + " |" for ln in lines)


def _detect_tables(page, frags: list[dict], page_w: float, page_h: float) -> list[dict]:
    """表格检测：借助 PyMuPDF 的 find_tables（同时用「线条」与「文本」两种策略，
    边框表与无框表都能抓到），清洗掉伪表后，把真实表格重建为 GFM 表格。

    认领方式用「行坐标包围盒」而不是文字互含匹配：表格里常有单字单元格
    （稿 / 取 / 索 这种被拆开的字），按文字匹配会漏掉它们，漏掉的字就会
    从正文流里漏出来变成一行行碎片。

    返回每张表的 {y0,y1,x0,x1, md, frag_idx}，frag_idx 是已被表格占用的
    文字碎片下标，正文流据此剔除，避免重复。
    """
    if not frags or not page_w or not page_h:
        return []
    extract_cands = []   # [(grid, rows_bbox)]  文本网格路线
    geo_cands = []       # [(grid, used_idx)]   几何重建路线
    for strat in ("lines", "text"):
        try:
            finder = page.find_tables(horizontal_strategy=strat, vertical_strategy=strat)
        except Exception:
            continue
        for t in finder.tables:
            try:
                cells = t.extract()
            except Exception:
                cells = None
            try:
                row_boxes = [tuple(r.bbox) for r in t.rows]
            except Exception:
                row_boxes = None
            if cells:
                for grid, run in _clean_grid(cells, page_h):
                    boxes = None
                    if row_boxes and len(row_boxes) >= len(grid):
                        picked = [row_boxes[i] for i in run if i < len(row_boxes)]
                        if len(picked) == len(run):
                            boxes = picked
                    extract_cands.append((grid, boxes))
            # 几何重建只在后面判定「文本网格塌了」时才会用上
            geo_grid, geo_used = _grid_from_geometry(t, frags)
            if not geo_grid:
                continue
            for grid, _run in _filter_grid(_merge_wrapped_rows(geo_grid)):
                if len(grid[0]) < 3:
                    continue
                ys = [frags[i]["bbox"][1] for i in geo_used]
                ye = [frags[i]["bbox"][3] for i in geo_used]
                xs = [frags[i]["bbox"][0] for i in geo_used]
                xe = [frags[i]["bbox"][2] for i in geo_used]
                if (max(ye) - min(ys)) > page_h * 0.72 or (max(xe) - min(xs)) > page_w * 0.88:
                    continue
                geo_cands.append((grid, geo_used))

    def _worst_cell(grid) -> int:
        return max((len(c) for row in grid for c in row), default=0)

    # 文本网格若出现超长单元格，说明跨行回绕把内容摊错行了；此时改用几何重建
    use_geo = bool(geo_cands) and any(_worst_cell(g) > 120 for g, _ in extract_cands)
    candidates = (
        [(g, None, u) for g, u in geo_cands] if use_geo
        else [(g, b, None) for g, b in extract_cands]
    )

    # 去重：文本完全包含在内的子表丢弃（线条 / 文本策略可能各出一份）
    candidates.sort(key=lambda c: -len(" ".join(" ".join(r) for r in c[0])))
    unique = []
    for grid, boxes, used_idx in candidates:
        gt = "\n".join(" | ".join(r) for r in grid)
        if any(gt in "\n".join(" | ".join(r) for r in u[0]) for u in unique):
            continue
        unique.append((grid, boxes, used_idx))

    out: list[dict] = []
    used: set[int] = set()
    for grid, boxes, used_idx in unique:
        md = _build_md(grid)
        if not md:
            continue
        if used_idx:
            idxs = list(dict.fromkeys(used_idx))
            x0 = min(frags[i]["bbox"][0] for i in idxs)
            y0 = min(frags[i]["bbox"][1] for i in idxs)
            x1 = max(frags[i]["bbox"][2] for i in idxs)
            y1 = max(frags[i]["bbox"][3] for i in idxs)
        elif boxes:
            x0 = min(b[0] for b in boxes)
            y0 = min(b[1] for b in boxes)
            x1 = max(b[2] for b in boxes)
            y1 = max(b[3] for b in boxes)
            pad = 2.0
            idxs = [
                i for i, f in enumerate(frags)
                if x0 - pad <= (f["bbox"][0] + f["bbox"][2]) / 2 <= x1 + pad
                and y0 - pad <= (f["bbox"][1] + f["bbox"][3]) / 2 <= y1 + pad
            ]
        else:
            cell_texts = [c for row in grid for c in row if c]
            idxs = [
                i for i, f in enumerate(frags)
                if any(f["text"].strip() and (f["text"].strip() in ct or ct in f["text"].strip())
                       for ct in cell_texts)
            ]
            if idxs:
                x0 = min(frags[i]["bbox"][0] for i in idxs)
                y0 = min(frags[i]["bbox"][1] for i in idxs)
                x1 = max(frags[i]["bbox"][2] for i in idxs)
                y1 = max(frags[i]["bbox"][3] for i in idxs)
        idxs = [i for i in idxs if i not in used]
        if not idxs:
            continue
        used.update(idxs)
        out.append({"y0": y0, "y1": y1, "x0": x0, "x1": x1, "md": md, "frag_idx": idxs})
    out.sort(key=lambda t: t["y0"])
    return out


# ---------------------------------------------------------------- 统计


def _furniture(lines_by_page, heights) -> set[str]:
    """跨页重复出现的页边文本（页眉页脚）。"""
    top: dict[str, set] = defaultdict(set)
    bottom: dict[str, set] = defaultdict(set)
    for pno, lines in enumerate(lines_by_page):
        h = heights[pno] or 1
        for line in lines:
            key = _norm(line["text"])
            if len(key) < 2:
                continue
            y0, y1 = line["bbox"][1], line["bbox"][3]
            if y1 < h * 0.10:
                top[key].add(pno)
            elif y0 > h * 0.90:
                bottom[key].add(pno)
    threshold = max(3, int(len(lines_by_page) * 0.6))
    drop = set()
    for key, pageset in list(top.items()) + list(bottom.items()):
        if len(pageset) >= threshold:
            drop.add(key)
    return drop


def _body_size(lines_by_page) -> float:
    counter: Counter = Counter()
    for lines in lines_by_page:
        for line in lines:
            if line["size"]:
                counter[line["size"]] += len(line["text"])
    if not counter:
        return 0.0
    return counter.most_common(1)[0][0]


def _toc_map(doc) -> dict[int, list[tuple[int, str]]]:
    out: dict[int, list[tuple[int, str]]] = defaultdict(list)
    try:
        toc = doc.get_toc(simple=True)
    except Exception:
        return out
    for row in toc:
        if len(row) < 3:
            continue
        level, title, page = row[0], row[1], row[2]
        if not isinstance(page, int) or page <= 0:
            continue
        out[page].append((int(level), _norm(title)))
    return out


def _size_levels(sizes: list[float]) -> dict[float, int]:
    uniq = sorted(set(sizes), reverse=True)
    mapping = {}
    for i, size in enumerate(uniq[:3]):
        mapping[size] = i + 1
    for size in uniq[3:]:
        mapping[size] = 3
    return mapping


# ---------------------------------------------------------------- 组装


def _normalize_list_text(text: str) -> str:
    m = ORDERED_RE.match(text)
    if m:
        return f"{m.group(1)}. {m.group(2).strip()}"
    m = BULLET_RE.match(text)
    if m:
        return "- " + m.group(1).strip()
    return text


WRAP_RATIO = 1.9  # 行距 / 字号 小于此值即视为「同一段的换行」而非新段落


def _emit_paragraphs(flow: list[dict], level_of, drop: set, page_height: float) -> list[str]:
    """flow 里混着图片 / 文字行 / 表格，按顺序输出 Markdown 片段。

    可读性约定：
    - 标题、图片、表格各自独立成块（块间空一行）；
    - 连续的同类列表项紧凑成一段，项间只换行不空行；
    - 列表项被折成多行时，折下来的行要接回该项，不能甩成独立段落
      （否则会出现「…很难」/「封。」这种断句错位）；
    - 正文按行距合并成段落，段间空一行。
    """
    parts: list[str] = []
    buffer: list[str] = []          # 当前正文段落
    list_run: list[str] = []        # 当前列表连续项
    list_type: str | None = None
    list_x0: float | None = None    # 当前列表项标记的起始 x，用来判断续行缩进
    prev: dict | None = None
    prev_text = ""

    def flush_prose():
        nonlocal buffer
        if buffer:
            parts.append(escape_md(smart_join(buffer)))
            buffer = []

    def flush_list():
        nonlocal list_run, list_type, list_x0
        if list_run:
            parts.append("\n".join(list_run))
            list_run = []
        list_type = None
        list_x0 = None

    for item in flow:
        kind = item["kind"]
        if kind == "image":
            flush_prose(); flush_list()
            if item.get("token"):
                parts.append(item["token"])
            prev = None; prev_text = ""
            continue
        if kind == "table":
            flush_prose(); flush_list()
            parts.append(item["md"])
            prev = None; prev_text = ""
            continue

        line = item["line"]
        text = line["text"].strip()
        if not text:
            continue
        if _norm(text) in drop:
            continue
        if PAGE_NUM_RE.match(text):
            y0, y1 = line["bbox"][1], line["bbox"][3]
            if y0 > page_height * 0.88 or y1 < page_height * 0.12:
                continue

        level = level_of(line)
        if level:
            flush_prose(); flush_list()
            plain = re.sub(r"\s+", " ", text).strip()
            parts.append("#" * min(level, 6) + " " + escape_md(plain))
            prev = None; prev_text = ""
            continue

        font = max(line.get("size") or 0, (prev or {}).get("size") or 0, 1.0)
        ratio = (line["bbox"][3] - prev["bbox"][3]) / font if prev else 99.0

        if LIST_START_RE.match(text):
            flush_prose()
            ttype = "ordered" if ORDERED_RE.match(text) else "bullet"
            if list_type != ttype:
                flush_list()
                list_type = ttype
            list_run.append(_normalize_list_text(text))
            list_x0 = line["bbox"][0]
            prev = line; prev_text = text
            continue

        # 纯数字的孤立短行多是版式上的大号章节编号（如「0 2」），不是正文
        if re.fullmatch(r"[\d\s]{1,5}", text):
            continue

        # 列表项的折行：行距紧、且相对标记位置有缩进 → 接回上一项
        if list_run and prev is not None:
            indented = list_x0 is not None and line["bbox"][0] >= list_x0 - 1.0
            if ratio < WRAP_RATIO and indented:
                list_run[-1] = smart_join([list_run[-1], text])
                prev = line; prev_text = text
                continue
            flush_list()

        # 正文
        flush_list()
        if prev is None:
            buffer = [text]
        else:
            same_para = ratio < WRAP_RATIO and not prev_text.endswith(tuple(SENTENCE_END))
            if same_para:
                buffer.append(text)
            else:
                flush_prose()
                buffer = [text]
        prev = line
        prev_text = text

    flush_prose(); flush_list()
    return parts


def convert(
    path: str,
    sink: ImageSink,
    max_pages: int | None = None,
    use_bold: bool = False,
    ocr: bool = True,
    ocr_headings: bool = False,
    ocr_dpi: int = 200,
    ocr_max_pages: int = 300,
    ocr_keep_image: bool = False,
    ocr_engine_name: str | None = None,
) -> DocResult:
    result = DocResult()
    doc = pymupdf.open(path)
    if doc.page_count == 0:
        result.warnings.append("PDF 没有页面")
        return result

    pages, heights, widths = _collect(doc, max_pages)
    drop = _furniture([_page_lines(_page_fragments(b), widths[i]) for i, b in enumerate(pages)], heights)
    body_size = _body_size([_page_lines(_page_fragments(b), widths[i]) for i, b in enumerate(pages)])
    toc = _toc_map(doc)
    ocr_engine = None
    if ocr:
        from .ocr import pick_engine

        ocr_engine = pick_engine(ocr_engine_name)

    # 标题候选统一分档
    candidates: list[float] = []
    if not toc:
        for i, b in enumerate(pages):
            for line in _page_lines(_page_fragments(b), widths[i]):
                text = line["text"].strip()
                if not text or len(text) > HEADING_MAX_LEN:
                    continue
                if text[-1] in HEADING_TAIL:
                    continue
                if body_size and line["size"] >= body_size * 1.12:
                    candidates.append(line["size"])
    size_map = _size_levels(candidates)

    def level_of(line: dict) -> int | None:
        text = line["text"]
        if toc:
            for lvl, title in toc.get(current_page[0] + 1, []):
                key = _norm(text)
                if key == title or (len(title) >= TOC_TITLE_MIN and key.startswith(title)):
                    return min(lvl, 6)
            return None
        if (
            len(text) <= HEADING_MAX_LEN
            and text[-1] not in HEADING_TAIL
            and line["size"] in size_map
        ):
            return size_map[line["size"]]
        return None

    current_page = [0]
    blocks_out: list[str] = []
    heading_count = 0
    scanned_pages = 0
    ocr_pages = 0
    ocr_lines = 0

    for pno in range(len(pages)):
        current_page[0] = pno
        page_width = widths[pno]
        page_height = heights[pno] or 1
        page = doc[pno]
        frags = _page_fragments(pages[pno])
        tables = _detect_tables(page, frags, page_width, page_height)
        # 表格占用的碎片下标，正文流据此剔除，避免重复
        consumed: set[int] = set()
        for t in tables:
            consumed.update(t.get("frag_idx", []))
        prose_frags = [f for i, f in enumerate(frags) if i not in consumed]

        image_start = sink.count
        flow: list[dict] = []
        image_area = 0.0

        for block in pages[pno]:
            if block.get("type") != 1:
                continue
            bbox = block.get("bbox") or (0, 0, 0, 0)
            image_area += max(0.0, bbox[2] - bbox[0]) * max(0.0, bbox[3] - bbox[1])
            blob = block.get("image")
            if not blob and block.get("xref"):
                try:
                    blob = doc.extract_image(block["xref"]).get("image")
                except Exception:
                    blob = None
            token = sink.save(blob, "png") if blob else None
            flow.append({"kind": "image", "y": bbox[1], "token": token})

        for line in _page_lines(prose_frags, page_width, []):
            flow.append({"kind": "text", "y": line["bbox"][1], "line": line})
        for t in tables:
            flow.append({"kind": "table", "y": t["y0"], "md": t["md"]})
        flow.sort(key=lambda item: (item["y"] or 0, 0 if item["kind"] == "text" else 1))

        kept = [item for item in flow if item["kind"] != "text" or _norm(item["line"]["text"]) not in drop]
        text_chars = sum(len(item["line"]["text"]) for item in kept if item["kind"] == "text")
        # 表格也算文字量，避免被误判为扫描页
        text_chars += sum(len(t["md"]) for t in tables)

        is_scanned = bool(
            page_width and image_area > 0 and text_chars < 100
            and image_area > page_width * page_height * 0.5
        )

        ocr_ok = False
        if is_scanned and ocr_engine is not None and ocr_pages < ocr_max_pages:
            try:
                from .ocr import ocr_page

                md, line_count = ocr_page(doc[pno], ocr_engine, dpi=ocr_dpi, headings=ocr_headings)
            except Exception as exc:  # noqa: BLE001
                md, line_count = "", 0
                result.warnings.append(f"第 {pno + 1} 页 OCR 失败：{exc}")
            if md.strip():
                if not ocr_keep_image:
                    sink.rollback_to(image_start)
                blocks_out.append(md)
                ocr_pages += 1
                ocr_ok = True
                ocr_lines += line_count
                if len(pages) > 8 and ocr_pages % 5 == 0:
                    print(f"  OCR 已处理 {ocr_pages}/{len(pages)} 页…", file=sys.stderr)
                for line in md.split("\n"):
                    m = re.match(r"^(#{1,6})\s+(.+)$", line)
                    if m:
                        result.headings.append((len(m.group(1)), m.group(2).strip()))
                        heading_count += 1
        if is_scanned:
            scanned_pages += 1

        if not ocr_ok or ocr_keep_image:
            parts = _emit_paragraphs(flow, level_of, drop, page_height)
            for part in parts:
                m = re.match(r"^(#{1,6})\s+(.+)$", part)
                if m:
                    heading_count += 1
                    result.headings.append((len(m.group(1)), m.group(2).strip()))
            if parts:
                blocks_out.append("\n\n".join(parts))

    result.markdown = collapse_blanks("\n\n".join(blocks_out))
    result.image_count = sink.count
    if heading_count == 0 and ocr_pages == 0 and scanned_pages == 0:
        result.warnings.append("没识别到任何标题（PDF 没有书签，字号也无明显层级），全篇按正文输出")
    if scanned_pages:
        if ocr_pages:
            extra = f"，{scanned_pages - ocr_pages} 页识别为空" if scanned_pages > ocr_pages else ""
            result.warnings.append(
                f"{ocr_pages}/{scanned_pages} 页扫描件已 OCR"
                f"（引擎：{ocr_engine.name if ocr_engine else '无'}，{ocr_lines} 行{extra}）"
            )
        elif not ocr:
            result.warnings.append(f"有 {scanned_pages} 页是扫描图，按 --no-ocr 跳过了识别，这部分只有图没有字")
        elif ocr_engine is None:
            result.warnings.append(f"有 {scanned_pages} 页是扫描图，但没找到可用的 OCR 引擎，这部分只有图没有字")
        else:
            result.warnings.append(f"有 {scanned_pages} 页是扫描图，未做 OCR（已关闭或超出页数上限）")
    if toc:
        result.warnings.append("标题层级取自 PDF 内置书签")
    result.meta["pages"] = doc.page_count
    if ocr_pages:
        result.meta["ocr_pages"] = ocr_pages
    doc.close()
    return result
