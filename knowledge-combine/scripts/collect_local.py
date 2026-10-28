#!/usr/bin/env python3
"""本地源采集：扫描目录 → 解析 → 写 IR。"""
import argparse
import os
import re
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import parsers

MEDIA_BY_EXT = {
    ".md": "MD", ".markdown": "MD", ".txt": "TXT", ".html": "HTML", ".htm": "HTML",
    ".pdf": "PDF", ".docx": "DOCX", ".doc": "DOC", ".pptx": "PPTX",
    ".xlsx": "XLSX", ".xls": "XLSX", ".csv": "XLSX", ".json": "OTHER",
}


def _should_exclude(rel_path, parts, exclude):
    for p in parts:
        if p in exclude or p.startswith("."):
            return True
    if os.path.basename(rel_path).startswith("~$"):
        return True
    return False


def _scan(root, recursive, include, exclude, follow_links, max_docs, updated_after):
    hits = []
    seen = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=follow_links):
        real = os.path.realpath(dirpath)
        if real in seen:
            dirnames[:] = []
            continue
        seen.add(real)
        rel_dir = os.path.relpath(dirpath, root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        dirnames[:] = [d for d in dirnames if not _should_exclude(d, [d], exclude)]
        if not recursive:
            dirnames[:] = []
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext not in C.TEXT_EXT:
                continue
            if include and ext not in include:
                continue
            if _should_exclude(fn, parts, exclude):
                continue
            fp = os.path.join(dirpath, fn)
            st = os.stat(fp)
            if updated_after:
                mt = datetime.fromtimestamp(st.st_mtime)
                if mt < updated_after:
                    continue
            hits.append((fp, st.st_mtime, parts))
    hits.sort(key=lambda x: -x[1])
    truncated = False
    if max_docs and len(hits) > max_docs:
        hits = hits[:max_docs]
        truncated = True
    return hits, truncated


def cmd_collect(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    exclude = set(C.DEFAULT_EXCLUDE)
    if a.exclude:
        exclude |= {x.strip() for x in a.exclude.split(",") if x.strip()}
    include = None
    if a.include:
        include = {x.strip().lower() for x in a.include.split(",") if x.strip()}
    updated_after = None
    if a.updated_after:
        updated_after = datetime.strptime(a.updated_after, "%Y-%m-%d")
    max_docs = a.max_docs or m["policy"].get("max_docs", 200)
    recover = m["policy"].get("image_policy") == "recover"
    img_dir = os.path.join(jd, "img")

    hits, truncated = _scan(os.path.abspath(a.path), not a.no_recursive,
                            include, exclude, a.follow_links, max_docs, updated_after)
    records = []
    for fp, mtime, parts in hits:
        sid = C.next_sid(jd)
        try:
            text, imgs, meta = parsers.parse_file(
                fp, img_dir if recover else None, sid, m["policy"].get("use_ocr", True))
            note = ""
            if meta.get("scanned") and meta.get("ocr"):
                note = "扫描件 OCR"
            elif meta.get("scanned"):
                note = "扫描件，OCR 未启用"
            if meta.get("oversize"):
                note = (note + "; " if note else "") + "文件 > %dMB" % parsers.MAX_FILE_MB
            rec = C.make_record(jd, sid, "local", os.path.basename(fp), fp, text,
                                MEDIA_BY_EXT.get(os.path.splitext(fp)[1].lower(), "OTHER"),
                                breadcrumb=parts,
                                modified_at=datetime.fromtimestamp(mtime).isoformat(timespec="seconds"),
                                images=imgs, parse_note=note, extra=meta)
        except Exception as e:
            rec = C.make_record(jd, sid, "local", os.path.basename(fp), fp, "",
                                MEDIA_BY_EXT.get(os.path.splitext(fp)[1].lower(), "OTHER"),
                                breadcrumb=parts, completeness="parse_failed",
                                parse_note=str(e)[:200])
            C.add_issue(m, "source", sid, "parse_failed", "%s：%s" % (fp, e))
        records.append(rec)
        C.append_ir(jd, [rec])
    for r in records:
        for im in r.get("images", []):
            m.setdefault("images", []).append(
                {"source_id": r["source_id"], "id": im["id"], "file": im["file"],
                 "page": im.get("page"), "status": im.get("status", "ok")})
    if truncated:
        C.add_issue(m, "source", "", "max_docs", "采集数量超过上限 %d，按修改时间保留最新的一批" % max_docs)
    m["status"] = "collected"
    C.save_manifest(mp, m)
    print(json_dumps(len(records), records))


def json_dumps(n, records):
    import json
    return json.dumps({
        "collected": n,
        "ok": sum(1 for r in records if r["completeness"] != "parse_failed"),
        "failed": sum(1 for r in records if r["completeness"] == "parse_failed"),
        "truncated_suspect": sum(1 for r in records if r["completeness"] == "suspected_truncated"),
    }, ensure_ascii=False)


def _parse_pages(spec):
    """解析页范围，支持 `1-10` / `3,5,7` / `1-3,8`；空或非法返回 None（表示全部页）。"""
    out = set()
    for tok in re.split(r"[,\s]+", (spec or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"(\d+)\s*-\s*(\d+)", tok)
        if m:
            a, b = int(m.group(1)), int(m.group(2))
            out.update(range(min(a, b), max(a, b) + 1))
        elif tok.isdigit():
            out.add(int(tok))
    return out or None


def cmd_ocr(a):
    out = a.out or (a.pdf + ".md")
    pages = _parse_pages(a.pages)
    text = parsers.ocr_pdf(a.pdf, os.path.dirname(os.path.abspath(out)), "ocr", pages=pages)
    C.write_text(out, text)
    print(out)


def main():
    p = argparse.ArgumentParser(description="collect local files")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("collect")
    s.add_argument("--manifest", required=True)
    s.add_argument("--path", required=True)
    s.add_argument("--no-recursive", action="store_true")
    s.add_argument("--include", help="逗号分隔扩展名白名单，如 .md,.pdf")
    s.add_argument("--exclude", help="追加排除目录名")
    s.add_argument("--max-docs", type=int)
    s.add_argument("--updated-after", help="YYYY-MM-DD")
    s.add_argument("--follow-links", action="store_true")
    s.set_defaults(fn=cmd_collect)

    s = sub.add_parser("ocr")
    s.add_argument("--pdf", required=True)
    s.add_argument("--out")
    s.add_argument("--pages", help="只 OCR 指定页，支持区间与列表：1-10 / 3,5,7 / 1-3,8；省略为全部")
    s.set_defaults(fn=cmd_ocr)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
