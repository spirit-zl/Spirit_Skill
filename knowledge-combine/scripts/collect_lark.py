#!/usr/bin/env python3
"""飞书源采集：inspect → 展开 → 导出为本地文件 → 复用本地解析器写 IR。

正文一律先落到 <job_dir>/lark_raw/，再用 parsers.py 转成 Markdown，
不直接解析 DocxXML。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C
import parsers

LARK = os.environ.get("LARK_CLI", "lark-cli")
FEISHU_URL_RE = re.compile(r"https://[\w.-]*\.(?:feishu|larksuite)\.cn/(?:docx|docs|wiki|sheet|base|slides|file|folder|mindnote)/[\w?=&]+")

EXPORT_EXT = {"docx": "markdown", "doc": "markdown", "wiki": "markdown",
              "sheet": "xlsx", "bitable": "csv", "slides": "pptx"}


def _run(args, as_identity="user"):
    cmd = [LARK] + args
    if "--as" not in args:
        cmd += ["--as", as_identity]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    except FileNotFoundError:
        return {"ok": False, "error": {"message": "lark-cli 未找到"}}
    out = p.stdout.strip()
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "error": {"message": (out or p.stderr)[:300]}}


def _inspect(url, identity):
    r = _run(["drive", "+inspect", "--url", url], identity)
    if r.get("ok") is False:
        return None, r.get("error", {}).get("message", "inspect 失败")
    return r.get("data", r), None


def _wiki_space_id(node_token, identity, hint=""):
    """wiki 节点的 space_id。inspect 常常不给，需用 node-get 补。
    没有 space_id 时 --parent-node-token 列不出子节点，会静默漏掉整棵树。"""
    if hint:
        return hint
    r = _run(["wiki", "+node-get", "--node-token", node_token], identity)
    d = r.get("data") or r
    node = d.get("node") or d
    return node.get("space_id") or d.get("space_id") or ""


def _expand_wiki(token, space_id, identity, depth, visited):
    items = []
    if depth <= 0 or token in visited:
        return items
    visited.add(token)
    # --page-limit 0 = 不限页；给小值会静默截断节点列表，看起来抓全了其实漏了
    args = ["wiki", "+node-list", "--space-id", space_id,
            "--parent-node-token", token, "--page-all", "--page-limit", "0", "--page-size", "50"]
    r = _run(args, identity)
    data = r.get("data") or {}
    nodes = data.get("nodes") or data.get("items") or r.get("items") or []
    for n in nodes:
        node_token = n.get("node_token")
        # 递归用 node_token；导出正文要用 obj_token，两者不是一回事
        obj_token = n.get("obj_token") or node_token
        obj_type = n.get("obj_type") or "docx"
        if (n.get("has_child") or n.get("node_type") == "origin") and node_token:
            items += _expand_wiki(node_token, space_id, identity, depth - 1, visited)
        if obj_token:
            items.append({"token": obj_token, "type": obj_type, "title": n.get("title", ""),
                          "node_token": node_token})
    return items


def _expand_folder(token, identity, depth, visited):
    items = []
    if depth <= 0 or token in visited:
        return items
    visited.add(token)
    page_token = None
    while True:
        args = ["drive", "files", "list", "--folder-token", token, "--page-size", "200"]
        if page_token:
            args += ["--page-token", page_token]
        r = _run(args, identity)
        data = r.get("data") or r
        files = data.get("files") or data.get("items") or []
        for f in files:
            ftype = f.get("type")
            ftoken = f.get("token")
            if not ftoken:
                continue
            if ftype == "folder":
                items += _expand_folder(ftoken, identity, depth - 1, visited)
            else:
                items.append({"token": ftoken, "type": ftype, "title": f.get("name", "")})
        page_token = data.get("next_page_token")
        if not page_token or data.get("has_more") is False:
            break
    return items


def _export(item, out_dir, identity):
    dtype = item["type"]
    ext = EXPORT_EXT.get(dtype)
    if not ext:
        return None, "不支持导出的类型：%s" % dtype
    args = ["drive", "+export", "--token", item["token"], "--doc-type", dtype,
            "--file-extension", ext, "--output-dir", out_dir, "--overwrite"]
    r = _run(args, identity)
    data = r.get("data") or r
    if r.get("ok") is False:
        return None, r.get("error", {}).get("message", "export 失败")
    fp = data.get("file_path") or data.get("file") or data.get("path")
    if not fp and data.get("task_id"):
        tr = _run(["drive", "+task_result", "--token", data["task_id"]], identity)
        td = tr.get("data") or tr
        fp = td.get("file_path") or td.get("file")
    if fp and os.path.exists(fp):
        return fp, None
    # 回退：docs +fetch
    fr = _run(["docs", "+fetch", "--doc", item["token"], "--doc-format", "markdown"], identity)
    if fr.get("ok") is False:
        return None, "导出与 fetch 均失败：%s" % item["token"]
    text = ((fr.get("data") or fr).get("content") or "").strip()
    if not text:
        return None, "空文档或无权限：%s" % item["token"]
    fp = os.path.join(out_dir, "%s.md" % C.slugify(item.get("title") or item["token"]))
    C.write_text(fp, text)
    return fp, None


def cmd_inspect(a):
    data, err = _inspect(a.url, a.identity)
    print(json.dumps(data if data else {"error": err}, ensure_ascii=False, indent=2))


def cmd_collect(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    raw_dir = os.path.join(jd, "lark_raw")
    os.makedirs(raw_dir, exist_ok=True)
    img_dir = os.path.join(jd, "img")
    identity = a.identity
    depth = a.depth or 10

    info, err = _inspect(a.url, identity)
    if not info:
        C.add_issue(m, "source", "", "no_permission", "入口解析失败：%s" % err, severity="error")
        C.save_manifest(mp, m)
        print(json.dumps({"ok": False, "error": err}, ensure_ascii=False))
        return
    etype = info.get("type") or info.get("doc_type") or ""
    token = info.get("token") or info.get("obj_token") or ""
    space_id = info.get("space_id") or ""

    items = []
    visited = set()
    if a.expand in ("auto", "links", "depth"):
        if etype == "wiki":
            space_id = _wiki_space_id(token, identity, space_id)
            if not space_id:
                C.add_issue(m, "source", "", "no_permission",
                            "无法解析 wiki 节点的 space_id，子节点树可能不完整",
                            severity="error")
            items = _expand_wiki(token, space_id, identity, depth, visited)
        elif etype == "folder":
            items = _expand_folder(token, identity, depth, visited)
        else:
            items = [{"token": token, "type": etype, "title": info.get("title", "")}]
    else:
        items = [{"token": token, "type": etype, "title": info.get("title", "")}]

    if not items:
        items = [{"token": token, "type": etype, "title": info.get("title", "")}]

    max_docs = a.max_docs or m["policy"].get("max_docs", 200)
    items = items[:max_docs]
    recover = m["policy"].get("image_policy") == "recover"

    records = []
    queue = list(items)
    seen_tokens = set()
    while queue:
        item = queue.pop(0)
        if item["token"] in seen_tokens:
            continue
        seen_tokens.add(item["token"])
        fp, err = _export(item, raw_dir, identity)
        if err:
            sid = C.next_sid(jd)
            rec = C.make_record(jd, sid, "lark", item.get("title") or item["token"],
                                item["token"], "", "DOCX_FEISHU",
                                completeness="parse_failed", parse_note=err)
            C.add_issue(m, "source", sid, "no_permission" if "权限" in err else "parse_failed",
                        "%s：%s" % (item.get("title"), err))
            records.append(rec)
            C.append_ir(jd, [rec])
            continue
        sid = C.next_sid(jd)
        ext = os.path.splitext(fp)[1].lower()
        try:
            if ext in (".md", ".markdown"):
                text = C.read_text(fp)
                imgs = []
            else:
                text, imgs, meta = parsers.parse_file(
                    fp, img_dir if recover else None, sid, m["policy"].get("use_ocr", True))
            rec = C.make_record(jd, sid, "lark", item.get("title") or os.path.basename(fp),
                                item["token"], text,
                                {"xlsx": "SHEET", "csv": "SHEET", "pptx": "PPTX"}.get(ext, "DOCX_FEISHU"),
                                breadcrumb=[info.get("title", "")], images=imgs,
                                extra={"export_file": fp})
        except Exception as e:
            rec = C.make_record(jd, sid, "lark", item.get("title") or os.path.basename(fp),
                                item["token"], "", completeness="parse_failed", parse_note=str(e)[:200])
            C.add_issue(m, "source", sid, "parse_failed", str(e)[:200])
        records.append(rec)
        C.append_ir(jd, [rec])
        if a.expand == "links" and ext in (".md", ".markdown"):
            for u in FEISHU_URL_RE.findall(text or ""):
                sub, serr = _inspect(u, identity)
                if sub:
                    queue.append({"token": sub.get("token") or sub.get("obj_token"),
                                  "type": sub.get("type"), "title": sub.get("title", "")})

    for r in records:
        for im in r.get("images", []):
            m.setdefault("images", []).append(
                {"source_id": r["source_id"], "id": im["id"], "file": im["file"],
                 "page": im.get("page"), "status": im.get("status", "ok")})
    m["status"] = "collected"
    C.save_manifest(mp, m)
    print(json.dumps({"collected": len(records),
                      "ok": sum(1 for r in records if r["completeness"] != "parse_failed"),
                      "failed": sum(1 for r in records if r["completeness"] == "parse_failed")},
                     ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="collect lark docs")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("inspect")
    s.add_argument("--url", required=True)
    s.add_argument("--identity", default="user")
    s.set_defaults(fn=cmd_inspect)

    s = sub.add_parser("collect")
    s.add_argument("--manifest", required=True)
    s.add_argument("--url", required=True)
    s.add_argument("--expand", default="auto", choices=["auto", "none", "links"])
    s.add_argument("--depth", type=int, default=10)
    s.add_argument("--max-docs", type=int)
    s.add_argument("--identity", default="user")
    s.set_defaults(fn=cmd_collect)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
