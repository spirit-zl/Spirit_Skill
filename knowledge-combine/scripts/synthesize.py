#!/usr/bin/env python3
"""分层综合调度：分块、生成摘要任务包、合并分组/全局结果。

脚本只负责切分与装配，真正的摘要与成文由模型完成。
"""
import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

BOUNDARY = re.compile(r"(?m)^(#{1,6}\s|\s*```|<!--\s*page\s+\d+\s*-->|\s*$)")


def _included(job_dir):
    return [r for r in C.read_ir(job_dir)
            if not r.get("duplicate_of") and r.get("completeness") != "parse_failed"]


def _split(text, size, overlap):
    if len(text) <= size:
        return [text]
    cuts = [m.start() for m in BOUNDARY.finditer(text)]
    chunks, start = [], 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cand = [c for c in cuts if start < c <= end]
            end = cand[-1] if cand else end
        chunks.append(text[start:end])
        if end >= len(text):
            break
        nxt = [c for c in cuts if c > end - overlap]
        start = nxt[0] if nxt else end
        if start >= end:
            start = end
    return [c for c in chunks if c.strip()]


def cmd_plan(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    inc = _included(m["job_dir"])
    chars = sum(r.get("content_chars", 0) for r in inc)
    tokens = C.est_tokens(chars)
    nchunk = 0
    for r in inc:
        text = C.read_text(r["content_file"]) if os.path.exists(r["content_file"]) else ""
        nchunk += len(_split(text, m["policy"]["max_chars_per_chunk"], m["policy"]["chunk_overlap"]))
    need_confirm = len(inc) > m["policy"].get("confirm_threshold", 50) or tokens > 400000
    print(json.dumps({
        "docs": len(inc), "chars": chars, "est_tokens": tokens,
        "chunks": nchunk, "need_confirm": need_confirm,
        "confirm_threshold": m["policy"].get("confirm_threshold", 50),
        "strategy": "map-reduce 三层" if nchunk > len(inc) else "单层（每篇一块）",
    }, ensure_ascii=False, indent=2))


def cmd_chunk(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    cdir = os.path.join(jd, "chunks")
    os.makedirs(cdir, exist_ok=True)
    chunks = []
    for r in _included(jd):
        text = C.read_text(r["content_file"]) if os.path.exists(r["content_file"]) else ""
        parts = _split(text, m["policy"]["max_chars_per_chunk"], m["policy"]["chunk_overlap"])
        for i, part in enumerate(parts, 1):
            cid = "%s_%d" % (r["source_id"], i)
            fp = os.path.join(cdir, cid + ".md")
            C.write_text(fp, "# %s（第 %d/%d 块）\n\n%s" % (r.get("title"), i, len(parts), part))
            anchor = ""
            mh = re.search(r"(?m)^<!--\s*page\s+(\d+)\s*-->", part)
            if mh:
                anchor = "p" + mh.group(1)
            chunks.append({"chunk_id": cid, "source_id": r["source_id"],
                           "title": r.get("title"), "index": i, "total": len(parts),
                           "file": fp, "chars": len(part), "anchor": anchor,
                           "completeness": r.get("completeness"),
                           "source_key": r.get("source_key"),
                           "content_sha256": r.get("content_sha256")})
    m["chunks"] = chunks
    m["status"] = "chunked"
    C.save_manifest(mp, m)
    print(json.dumps({"chunks": len(chunks), "dir": cdir}, ensure_ascii=False))


def _cache_hit(m, key, sha):
    p = C.cache_path(key, sha, m.get("policy", {}).get("profile_version",
                                                       C.DEFAULT_PROFILE_VERSION))
    if os.path.exists(p):
        try:
            return json.loads(C.read_text(p)).get("summary")
        except Exception:
            return None
    return None


def cmd_tasks(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    if not m.get("chunks"):
        print(json.dumps({"error": "先跑 chunk"}, ensure_ascii=False))
        return
    tasks = []
    for ch in m["chunks"]:
        cached = _cache_hit(m, ch.get("source_key", ""), ch.get("content_sha256", ""))
        if cached:
            tasks.append({"chunk_id": ch["chunk_id"], "cached": True, "summary": cached})
            continue
        tasks.append({
            "chunk_id": ch["chunk_id"], "source_id": ch["source_id"], "cached": False,
            "file": ch["file"], "chars": ch["chars"], "anchor": ch.get("anchor"),
            "completeness": ch.get("completeness"),
            "instruction": (
                "按 synthesis-protocol.md 的 Layer 1 格式输出摘要：核心结论 / 关键数据（保留原文措辞）/ "
                "适用条件 / 存疑点。模型推断必须以「推断：」开头。完整性=%s，若为 suspected_truncated "
                "则在存疑点注明来源可能不完整。篇幅 ≤ 600 字。" % ch.get("completeness")),
        })
    out = tasks[:a.limit] if a.limit else tasks
    if a.out:
        C.write_text(a.out, json.dumps(out, ensure_ascii=False, indent=2))
        print(json.dumps({"tasks": len(out), "file": a.out}, ensure_ascii=False))
    else:
        print(json.dumps(out, ensure_ascii=False, indent=2))


def cmd_cache_summary(a):
    """把某块的摘要写入缓存。"""
    p = C.cache_path(a.source_key, a.content_sha256, a.profile_version)
    C.write_text(p, json.dumps({"source_key": a.source_key,
                                "content_sha256": a.content_sha256,
                                "profile_version": a.profile_version,
                                "summary": C.read_text(a.summary_file),
                                "at": C.now_iso()}, ensure_ascii=False))
    print(json.dumps({"cached": True, "path": p}, ensure_ascii=False))


def cmd_merge(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    data = C.read_text(a.input)
    if a.level == "group":
        m["summaries"]["group"][a.name or ("group%d" % (len(m["summaries"]["group"]) + 1))] = data
    else:
        m["summaries"]["global"] = data
    m["status"] = "synthesized"
    C.save_manifest(mp, m)
    print(json.dumps({"level": a.level, "chars": len(data)}, ensure_ascii=False))


def cmd_set_outline(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    m["outline"] = C.read_text(a.file)
    C.save_manifest(mp, m)
    print(json.dumps({"outline_chars": len(m["outline"])}, ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="synthesize planner")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("plan")
    s.add_argument("--manifest", required=True)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("chunk")
    s.add_argument("--manifest", required=True)
    s.set_defaults(fn=cmd_chunk)

    s = sub.add_parser("tasks")
    s.add_argument("--manifest", required=True)
    s.add_argument("--limit", type=int)
    s.add_argument("--out")
    s.set_defaults(fn=cmd_tasks)

    s = sub.add_parser("cache-summary")
    s.add_argument("--source-key", required=True)
    s.add_argument("--content-sha256", required=True)
    s.add_argument("--summary-file", required=True)
    s.add_argument("--profile-version", default=C.DEFAULT_PROFILE_VERSION)
    s.set_defaults(fn=cmd_cache_summary)

    s = sub.add_parser("merge")
    s.add_argument("--manifest", required=True)
    s.add_argument("--level", required=True, choices=["group", "global"])
    s.add_argument("--input", required=True)
    s.add_argument("--name")
    s.set_defaults(fn=cmd_merge)

    s = sub.add_parser("set-outline")
    s.add_argument("--manifest", required=True)
    s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_set_outline)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
