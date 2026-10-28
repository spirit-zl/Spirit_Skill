#!/usr/bin/env python3
"""配图还原（best effort）。

- 本地 PDF/DOCX/PPTX：采集时已顺带抽图，这里提供单独抽取
- ima 源：图片是 zip 内相对路径，私有桶直连 403；只能拿本机同名原件还原，
  再用 OCR + 单调 DP 把裁剪小图对齐到页号
"""
import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import parsers


def _open_pdf(path):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    return pymupdf.open(path)


def cmd_extract(a):
    os.makedirs(a.out_dir, exist_ok=True)
    doc = _open_pdf(a.pdf)
    imgs = parsers._pdf_images(doc, a.out_dir, a.sid or "img")
    doc.close()
    print(json.dumps({"images": len(imgs), "files": [i["file"] for i in imgs]}, ensure_ascii=False))


def _ngrams(text, n=4):
    s = re.sub(r"\s+", "", text or "")
    return set(s[i:i + n] for i in range(max(len(s) - n + 1, 0)))


def _align(pdf_path, alts, out_dir, sid):
    """alts: [裁剪图 OCR 文本]；返回 [(page_no, image_file)]，用单调 DP 保证页号非递减。"""
    doc = _open_pdf(pdf_path)
    npages = len(doc)
    page_imgs, page_grams = [], []
    for i, page in enumerate(doc):
        text = page.get_text() or ""
        if len(text.strip()) < 20:
            text = parsers._ocr_image(_render_page(doc, i, out_dir))
        page_grams.append(_ngrams(text))
        pix = page.get_pixmap(dpi=150)
        fp = os.path.join(out_dir, "%s_p%d.png" % (sid, i + 1))
        pix.save(fp)
        page_imgs.append(fp)
    doc.close()
    alt_grams = [_ngrams(a) for a in alts]
    n, m = len(alts), npages
    score = [[len(alt_grams[i] & page_grams[j]) / max(len(alt_grams[i]), 1)
              for j in range(m)] for i in range(n)]
    INF = float("-inf")
    dp = [[score[0][j] for j in range(m)]]
    back = [[-1] * m]
    for i in range(1, n):
        row, bk = [], []
        running, arg = INF, -1
        for j in range(m):
            if dp[i - 1][j] > running:
                running, arg = dp[i - 1][j], j
            row.append(running + score[i][j])
            bk.append(arg)
        dp.append(row)
        back.append(bk)
    last = max(range(m), key=lambda j: dp[n - 1][j])
    assign = [0] * n
    j = last
    for i in range(n - 1, -1, -1):
        assign[i] = j
        j = back[i][j] if back[i][j] >= 0 else j
    return [(assign[i] + 1, page_imgs[assign[i]]) for i in range(n)]


def _render_page(doc, idx, out_dir):
    pix = doc[idx].get_pixmap(dpi=200)
    fp = os.path.join(out_dir, "_tmp_p%d.png" % (idx + 1))
    pix.save(fp)
    return fp


def cmd_recover(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    img_dir = os.path.join(jd, "img")
    os.makedirs(img_dir, exist_ok=True)
    records = C.read_ir(jd)          # 必须复用同一份内存对象再回写，否则改动会丢
    recovered, failed = 0, 0
    for r in records:
        if r.get("images"):
            continue
        if r.get("origin") != "ima":
            continue
        if not a.search_dir:
            continue
        title = r.get("title") or ""
        cand = None
        for dirpath, _dn, filenames in os.walk(a.search_dir):
            for fn in filenames:
                if os.path.splitext(fn)[0] in title or title in fn:
                    cand = os.path.join(dirpath, fn)
                    break
            if cand:
                break
        if not cand or os.path.splitext(cand)[1].lower() != ".pdf":
            failed += 1
            m.setdefault("images", []).append(
                {"source_id": r["source_id"], "id": "I0", "file": "", "status": "failed",
                 "page": None})
            continue
        doc = _open_pdf(cand)
        imgs = parsers._pdf_images(doc, img_dir, r["source_id"])
        doc.close()
        r["images"] = imgs
        for im in imgs:
            m.setdefault("images", []).append(
                {"source_id": r["source_id"], "id": im["id"], "file": im["file"],
                 "page": im.get("page"), "status": "ok"})
        recovered += len(imgs)
    C.rewrite_ir(jd, records)
    C.save_manifest(mp, m)
    print(json.dumps({"recovered": recovered, "failed": failed}, ensure_ascii=False))


def cmd_align(a):
    alts = [x for x in C.read_text(a.alts_file).split("\n") if x.strip()]
    pairs = _align(a.pdf, alts, a.out_dir, a.sid or "img")
    print(json.dumps([{"alt": alts[i][:40], "page": p, "file": f}
                      for i, (p, f) in enumerate(pairs)], ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="image recovery")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("extract")
    s.add_argument("--pdf", required=True)
    s.add_argument("--out-dir", required=True)
    s.add_argument("--sid")
    s.set_defaults(fn=cmd_extract)

    s = sub.add_parser("recover")
    s.add_argument("--manifest", required=True)
    s.add_argument("--search-dir", help="本机原件搜索根目录（ima 源必需）")
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("align")
    s.add_argument("--pdf", required=True)
    s.add_argument("--alts-file", required=True, help="每行一条裁剪图的 OCR 文本")
    s.add_argument("--out-dir", required=True)
    s.add_argument("--sid")
    s.set_defaults(fn=cmd_align)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
