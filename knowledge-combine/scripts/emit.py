#!/usr/bin/env python3
"""输出：local / lark / ima。Markdown 是唯一真源。"""
import argparse
import json
import os
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

LARK = os.environ.get("LARK_CLI", "lark-cli")

IMA_MIME = {
    ".pdf": "application/pdf", ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xls": "application/vnd.ms-excel",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".csv": "text/csv", ".md": "text/markdown", ".markdown": "text/markdown",
    ".txt": "text/plain", ".xmind": "application/x-xmind", ".png": "image/png",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
    ".mp3": "audio/mpeg", ".m4a": "audio/x-m4a", ".wav": "audio/wav",
    ".html": "text/html", ".epub": "application/epub+zip", ".aac": "audio/aac",
}


def _record_output(m, target, path_or_url, extra=None):
    rec = {"target": target, "location": path_or_url, "at": C.now_iso()}
    if target == "local" and os.path.exists(path_or_url):
        rec["bytes"] = os.path.getsize(path_or_url)
        rec["sha256"] = C.sha256_file(path_or_url)
    if extra:
        rec.update(extra)
    m.setdefault("outputs", []).append(rec)


def cmd_local(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    tgt_dir = m["output"].get("location") or a.dir
    if not tgt_dir:
        print(json.dumps({"error": "缺少输出目录"}, ensure_ascii=False))
        return
    os.makedirs(tgt_dir, exist_ok=True)
    base = a.name or os.path.splitext(os.path.basename(a.file))[0]
    dst = os.path.join(tgt_dir, base + ".md")
    if os.path.exists(dst) and not a.overwrite:
        dst = os.path.join(tgt_dir, "%s-%s.md" % (base, datetime.now().strftime("%Y%m%d-%H%M%S")))
    data = C.read_text(a.file)
    C.write_text(dst, data)
    _record_output(m, "local", dst, {"format": "md"})
    m["status"] = "emitted"
    C.save_manifest(mp, m)
    out = {"file": dst, "chars": len(data)}
    if a.as_docx:
        out["docx_hint"] = "把该 md 交给 tencent-docx 的 doc-formatter / doc-converter 转 docx，不要用 python-docx 手搓排版"
    print(json.dumps(out, ensure_ascii=False))


def _run(args, identity="user"):
    cmd = [LARK] + args
    if "--as" not in args:
        cmd += ["--as", identity]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    try:
        return json.loads(p.stdout.strip())
    except Exception:
        return {"ok": False, "error": {"message": (p.stdout or p.stderr)[:300]}}


def cmd_lark(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    title = a.title or os.path.splitext(os.path.basename(a.file))[0]
    if a.mode == "overwrite":
        if not a.token:
            print(json.dumps({"error": "overwrite 需要 --token（且必须先经用户确认）"}, ensure_ascii=False))
            return
        r = _run(["markdown", "+overwrite", "--token", a.token, "--content", "@" + a.file], a.identity)
        loc = a.token
    elif a.wiki_node:
        r = _run(["wiki", "+node-create", "--space-id", a.wiki_space or "my_library",
                  "--parent-node-token", a.wiki_node, "--obj-type", "docx", "--title", title], a.identity)
        loc = ((r.get("data") or r).get("node_token") or "")
    else:
        if not a.parent_token:
            print(json.dumps({"error": "create 需要 --parent-token 或 --wiki-node"}, ensure_ascii=False))
            return
        r = _run(["docs", "+create", "--doc-format", "markdown", "--content", "@" + a.file,
                  "--parent-token", a.parent_token, "--title", title], a.identity)
        d = r.get("data") or r
        loc = d.get("document_id") or d.get("url") or ""
    if r.get("ok") is False:
        C.add_issue(m, "output", "", "upload_failed",
                    "飞书写入失败：%s" % r.get("error", {}).get("message"), severity="error")
        C.save_manifest(mp, m)
        print(json.dumps({"ok": False, "error": r.get("error")}, ensure_ascii=False))
        return
    _record_output(m, "lark", loc, {"title": title, "mode": a.mode})
    m["status"] = "emitted"
    C.save_manifest(mp, m)
    print(json.dumps({"ok": True, "location": loc, "title": title}, ensure_ascii=False))


def cmd_ima(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    ext = os.path.splitext(a.file)[1].lower()
    if ext not in IMA_MIME:
        print(json.dumps({"error": "扩展名 %s 不在 ima 白名单内，拒绝上传" % ext,
                          "whitelist": sorted(IMA_MIME)}, ensure_ascii=False))
        return
    size = os.path.getsize(a.file)
    params = {
        "knowledge_base_id": a.kb_id,
        "file_name": os.path.basename(a.file),
        "file_ext": ext.lstrip("."),
        "content_type": IMA_MIME[ext],
        "file_size": size,
    }
    if a.creds:
        creds = json.loads(C.read_text(a.creds))
        ok, msg = _cos_upload(a.file, creds)
        if not ok:
            C.add_issue(m, "output", "", "upload_failed", "COS 上传失败：%s" % msg, severity="error")
            C.save_manifest(mp, m)
            print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False))
            return
        media_id = creds.get("media_id") or a.media_id
        print(json.dumps({"uploaded": True, "media_id": media_id,
                          "next": "由 agent 调用 mcp__ima-mcp__add_knowledge",
                          "add_knowledge_params": {
                              "knowledge_base_id": a.kb_id, "media_id": media_id,
                              "folder_id": a.folder_id,
                              "duplicate_name_strategy": a.duplicate_name_strategy}},
                         ensure_ascii=False))
        _record_output(m, "ima", "kb:%s" % a.kb_id,
                       {"file": a.file, "media_id": media_id, "state": "uploaded_pending_add"})
        m["status"] = "emitted"
        C.save_manifest(mp, m)
        return
    print(json.dumps({"next": "由 agent 调用 mcp__ima-mcp__create_media",
                      "create_media_params": params,
                      "then": "把返回的凭证写入 creds.json，再跑本命令加 --creds",
                      "add_knowledge_params": {
                          "knowledge_base_id": a.kb_id, "folder_id": a.folder_id,
                          "duplicate_name_strategy": a.duplicate_name_strategy}},
                     ensure_ascii=False))


def _cos_upload(path, creds):
    try:
        import requests
    except ImportError:
        return False, "缺少 requests"
    url = creds.get("url") or creds.get("upload_url") or creds.get("host")
    if not url:
        return False, "creds 中没有上传地址"
    headers = creds.get("headers") or creds.get("sign_headers") or {}
    if creds.get("authorization"):
        headers["Authorization"] = creds["authorization"]
    method = (creds.get("method") or "PUT").upper()
    try:
        if creds.get("fields"):
            r = requests.post(url, data=creds["fields"],
                              files={"file": (os.path.basename(path), open(path, "rb"))}, timeout=300)
        elif method == "PUT":
            r = requests.put(url, data=open(path, "rb"), headers=headers, timeout=300)
        else:
            r = requests.post(url, data=open(path, "rb"), headers=headers, timeout=300)
    except Exception as e:
        return False, str(e)[:200]
    if r.status_code < 300:
        return True, ""
    return False, "HTTP %s %s" % (r.status_code, r.text[:200])


def main():
    p = argparse.ArgumentParser(description="emit output")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("local")
    s.add_argument("--manifest", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--name")
    s.add_argument("--dir")
    s.add_argument("--overwrite", action="store_true")
    s.add_argument("--as-docx", action="store_true")
    s.set_defaults(fn=cmd_local)

    s = sub.add_parser("lark")
    s.add_argument("--manifest", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--parent-token")
    s.add_argument("--wiki-node")
    s.add_argument("--wiki-space")
    s.add_argument("--title")
    s.add_argument("--mode", default="create", choices=["create", "overwrite"])
    s.add_argument("--token")
    s.add_argument("--identity", default="user")
    s.set_defaults(fn=cmd_lark)

    s = sub.add_parser("ima")
    s.add_argument("--manifest", required=True)
    s.add_argument("--file", required=True)
    s.add_argument("--kb-id", required=True)
    s.add_argument("--folder-id")
    s.add_argument("--media-id")
    s.add_argument("--creds")
    s.add_argument("--duplicate-name-strategy", default="DUPLICATE_NAME_STRATEGY_SAVE")
    s.set_defaults(fn=cmd_ima)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
