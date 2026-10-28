"""把各类文件解析成 Markdown 文本。被 collect_local 与 collect_lark 共用。"""
import csv
import json
import os
import re
import zipfile

MAX_FILE_MB = 50


def _read_text_any(path):
    raw = open(path, "rb").read()
    for enc in ("utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _html_to_md(html):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        text = re.sub(r"<script.*?</script>", "", html, flags=re.S | re.I)
        text = re.sub(r"<style.*?</style>", "", text, flags=re.S | re.I)
        return re.sub(r"<[^>]+>", "\n", text)
    soup = BeautifulSoup(html, "lxml" if _has("lxml") else "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    out = []
    body = soup.body or soup
    for el in body.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "tr", "pre"]):
        name = el.name
        text = el.get_text(" ", strip=True)
        if not text:
            continue
        if name.startswith("h") and len(name) == 2:
            level = int(name[1])
            out.append("#" * level + " " + text)
        elif name == "li":
            out.append("- " + text)
        elif name == "tr":
            cells = [c.get_text(" ", strip=True) for c in el.find_all(["td", "th"])]
            out.append("| " + " | ".join(cells) + " |")
        elif name == "pre":
            out.append("```\n" + text + "\n```")
        else:
            out.append(text)
    return "\n\n".join(out)


def _has(mod):
    import importlib
    try:
        importlib.import_module(mod)
        return True
    except Exception:
        return False


def _table_to_md(rows):
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [list(r) + [""] * (width - len(r)) for r in rows]
    head = "| " + " | ".join(str(c) for c in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in range(width)) + " |"
    body = ["| " + " | ".join(str(c) for c in r) + " |" for r in rows[1:]]
    return "\n".join([head, sep] + body)


def _parse_docx(path, img_dir=None, sid=None):
    import docx  # python-docx
    d = docx.Document(path)
    out = []
    for p in d.paragraphs:
        text = p.text.strip()
        if not text:
            continue
        style = (p.style.name or "").lower()
        if style.startswith("heading"):
            m = re.search(r"(\d+)", style)
            level = int(m.group(1)) if m else 2
            out.append("#" * min(level, 6) + " " + text)
        else:
            out.append(text)
    for t in d.tables:
        rows = [[c.text.strip() for c in r.cells] for r in t.rows]
        md = _table_to_md(rows)
        if md:
            out.append(md)
    imgs = []
    if img_dir and sid:
        try:
            with zipfile.ZipFile(path) as z:
                for n in z.namelist():
                    if n.startswith("word/media/"):
                        data = z.read(n)
                        ext = os.path.splitext(n)[1] or ".png"
                        fp = os.path.join(img_dir, "%s_%d%s" % (sid, len(imgs), ext))
                        open(fp, "wb").write(data)
                        imgs.append({"id": "I%d" % (len(imgs) + 1), "file": fp, "page": None,
                                     "caption": "", "status": "ok"})
        except Exception:
            pass
    return "\n\n".join(out), imgs


def _parse_pdf(path, img_dir=None, sid=None, use_ocr=True):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    doc = pymupdf.open(path)
    pages = []
    for i, page in enumerate(doc):
        pages.append(page.get_text() or "")
    total_chars = sum(len(p.strip()) for p in pages)
    scanned = len(doc) > 0 and total_chars / max(len(doc), 1) < 20
    if scanned and use_ocr:
        ocr_text = ocr_pdf(path, img_dir, sid)
        if ocr_text:
            return ocr_text, [], {"scanned": True, "ocr": True}
    imgs = []
    if img_dir and sid:
        imgs = _pdf_images(doc, img_dir, sid)
    text = "\n\n".join("<!-- page %d -->\n%s" % (i + 1, p) for i, p in enumerate(pages))
    doc.close()
    return text, imgs, {"scanned": scanned, "ocr": False}


def _pdf_images(doc, img_dir, sid):
    imgs = []
    for i, page in enumerate(doc):
        try:
            for info in page.get_images(full=True):
                xref = info[0]
                pix = doc.extract_image(xref)
                ext = pix.get("ext", "png")
                fp = os.path.join(img_dir, "%s_p%d_%d.%s" % (sid, i + 1, len(imgs), ext))
                open(fp, "wb").write(pix["image"])
                imgs.append({"id": "I%d" % (len(imgs) + 1), "file": fp, "page": i + 1,
                             "caption": "", "status": "ok"})
            if not page.get_images(full=True):
                pix = page.get_pixmap(dpi=150)
                fp = os.path.join(img_dir, "%s_p%d.png" % (sid, i + 1))
                pix.save(fp)
                imgs.append({"id": "I%d" % (len(imgs) + 1), "file": fp, "page": i + 1,
                             "caption": "", "status": "ok"})
        except Exception:
            continue
    return imgs


def ocr_pdf(path, img_dir=None, sid=None, dpi=200, pages=None):
    """扫描件 PDF → 文本。用 macOS Vision；失败返回空串。

    pages：可选的页码集合（1 起），只 OCR 这些页；None 表示全部。
    """
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    try:
        import Vision
        from Foundation import NSURL
    except Exception:
        return ""
    doc = pymupdf.open(path)
    texts = []
    tmp_dir = img_dir or "/tmp"
    for i, page in enumerate(doc):
        if pages and (i + 1) not in pages:
            continue
        pix = page.get_pixmap(dpi=dpi)
        fp = os.path.join(tmp_dir, "_ocr_%s_p%d.png" % (sid or "x", i + 1))
        pix.save(fp)
        texts.append("<!-- page %d -->\n%s" % (i + 1, _ocr_image(fp)))
        try:
            os.remove(fp)
        except OSError:
            pass
    doc.close()
    return "\n\n".join(texts)


def _ocr_image(path):
    import Vision
    from Foundation import NSURL
    results = []

    def _handler(request, error):
        obs = request.results() or []
        for o in obs:
            cands = o.topCandidates_(1)
            if cands and len(cands):
                results.append(cands[0].string())

    url = NSURL.fileURLWithPath_(path)
    handler = Vision.VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    req = Vision.VNRecognizeTextRequest.alloc().initWithCompletionHandler_(_handler)
    try:
        req.setRecognitionLevel_(0)
        req.setUsesLanguageCorrection_(True)
        req.setRecognitionLanguages_(["zh-Hans", "en-US"])
    except Exception:
        pass
    handler.performRequests_error_([req], None)
    return "\n".join(results)


def _parse_pptx(path, img_dir=None, sid=None):
    from pptx import Presentation
    prs = Presentation(path)
    out = []
    for i, slide in enumerate(prs.slides, 1):
        buf = ["<!-- slide %d -->" % i]
        for shape in slide.shapes:
            if shape.has_text_frame:
                t = shape.text_frame.text.strip()
                if t:
                    buf.append(t)
            if shape.has_table:
                rows = [[c.text.strip() for c in r.cells] for r in shape.table.rows]
                md = _table_to_md(rows)
                if md:
                    buf.append(md)
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                buf.append("备注：" + notes)
        out.append("\n".join(buf))
    return "\n\n".join(out), []


def _parse_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path, data_only=True, read_only=True)
    out = []
    for ws in wb.worksheets:
        rows = []
        for r in ws.iter_rows(values_only=True):
            if any(c is not None and str(c).strip() != "" for c in r):
                rows.append(["" if c is None else str(c) for c in r])
        if rows:
            out.append("## sheet: %s\n%s" % (ws.title, _table_to_md(rows)))
    wb.close()
    return "\n\n".join(out), []


def _parse_csv(path):
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    rows = rows[:2000]
    return _table_to_md(rows), []


def _parse_json(path):
    try:
        data = json.loads(_read_text_any(path))
        return "```json\n" + json.dumps(data, ensure_ascii=False, indent=2)[:200000] + "\n```", []
    except Exception:
        return _read_text_any(path), []


def parse_file(path, img_dir=None, sid=None, use_ocr=True):
    """返回 (markdown, images, meta)。失败抛异常，由调用方登记 parse_failed。"""
    ext = os.path.splitext(path)[1].lower()
    size_mb = os.path.getsize(path) / (1 << 20)
    meta = {"size_mb": round(size_mb, 2), "scanned": False, "ocr": False}
    if size_mb > MAX_FILE_MB:
        meta["oversize"] = True

    if ext in (".md", ".markdown", ".txt"):
        text, imgs = _read_text_any(path), []
    elif ext in (".html", ".htm"):
        text, imgs = _html_to_md(_read_text_any(path)), []
    elif ext == ".docx":
        text, imgs = _parse_docx(path, img_dir, sid)
    elif ext == ".doc":
        raise RuntimeError("旧版 .doc 不支持，请先转 .docx")
    elif ext == ".pdf":
        text, imgs, pm = _parse_pdf(path, img_dir, sid, use_ocr)
        meta.update(pm)
    elif ext == ".pptx":
        text, imgs = _parse_pptx(path, img_dir, sid)
    elif ext in (".xlsx", ".xls"):
        text, imgs = _parse_xlsx(path)
    elif ext == ".csv":
        text, imgs = _parse_csv(path)
    elif ext == ".json":
        text, imgs = _parse_json(path)
    else:
        raise RuntimeError("不支持的扩展名：%s" % ext)
    return text, imgs, meta
