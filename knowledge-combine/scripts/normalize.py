#!/usr/bin/env python3
"""归一化：补全指纹、去重、完整性统计、摘要缓存。"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

PRIORITY = {"local": 3, "lark": 2, "ima": 1}


def _ensure_fingerprint(job_dir, r):
    cf = r.get("content_file")
    if cf and os.path.exists(cf):
        text = C.read_text(cf)
    else:
        text = ""
    if not r.get("content_sha256"):
        r["content_sha256"] = C.sha256_text(text)
    r["content_chars"] = len(text)
    r.setdefault("source_key", r["content_sha256"][:16])
    return text


def cmd_run(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    ir = C.read_ir(jd)
    for r in ir:
        _ensure_fingerprint(jd, r)
    if a.dedupe:
        by_fp = {}
        for r in sorted(ir, key=lambda x: -PRIORITY.get(x.get("origin"), 0)):
            fp = r["content_sha256"]
            if fp in by_fp:
                r["duplicate_of"] = by_fp[fp]["source_id"]
            else:
                by_fp[fp] = r
    C.rewrite_ir(jd, ir)
    m["sources"] = [{"source_id": r["source_id"], "title": r.get("title"),
                     "origin": r.get("origin"), "chars": r.get("content_chars", 0),
                     "completeness": r.get("completeness"),
                     "duplicate_of": r.get("duplicate_of")} for r in ir]
    for r in ir:
        if r.get("completeness") == "parse_failed":
            C.add_issue(m, "source", r["source_id"], "parse_failed",
                        "解析失败，已跳过：%s" % (r.get("parse_note") or r.get("title")))
        elif r.get("completeness") == "empty":
            C.add_issue(m, "source", r["source_id"], "thin_content",
                        "内容过短，仍纳入但信息量有限：%s" % r.get("title"))
        elif r.get("completeness") == "suspected_truncated":
            C.add_issue(m, "source", r["source_id"], "suspected_truncated",
                        "疑似截断，引用时加脚注：%s" % r.get("title"))
        elif r.get("duplicate_of"):
            C.add_issue(m, "source", r["source_id"], "duplicate",
                        "与 %s 内容重复，已去重" % r["duplicate_of"])
    m["status"] = "normalized"
    C.save_manifest(mp, m)
    inc = [r for r in ir if not r.get("duplicate_of") and r.get("completeness") != "parse_failed"]
    print(json.dumps({"total": len(ir), "included": len(inc),
                      "chars": sum(r.get("content_chars", 0) for r in inc),
                      "parse_failed": sum(1 for r in ir if r.get("completeness") == "parse_failed"),
                      "duplicates": sum(1 for r in ir if r.get("duplicate_of"))},
                     ensure_ascii=False))


def _cache_path(key, sha, profile_version=C.DEFAULT_PROFILE_VERSION):
    return C.cache_path(key, sha, profile_version)


def cmd_cache_get(a):
    """命中时直接打印摘要正文，便于原样复用；未命中打印 {"hit": false}。"""
    p = _cache_path(a.source_key, a.content_sha256, a.profile_version)
    if os.path.exists(p):
        try:
            print(json.loads(C.read_text(p)).get("summary", ""))
        except Exception:
            print(C.read_text(p))
    else:
        print(json.dumps({"hit": False}, ensure_ascii=False))


def cmd_cache_put(a):
    data = C.read_text(a.summary_file)
    rec = json.dumps({"hit": True, "source_key": a.source_key,
                      "content_sha256": a.content_sha256,
                      "profile_version": a.profile_version, "summary": data,
                      "at": C.now_iso()}, ensure_ascii=False)
    C.write_text(_cache_path(a.source_key, a.content_sha256, a.profile_version), rec)
    print(json.dumps({"cached": True, "profile_version": a.profile_version},
                     ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="normalize IR")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run")
    s.add_argument("--manifest", required=True)
    s.add_argument("--dedupe", action="store_true")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("cache-get")
    s.add_argument("--source-key", required=True)
    s.add_argument("--content-sha256", required=True)
    s.add_argument("--profile-version", default=C.DEFAULT_PROFILE_VERSION)
    s.set_defaults(fn=cmd_cache_get)

    s = sub.add_parser("cache-put")
    s.add_argument("--source-key", required=True)
    s.add_argument("--content-sha256", required=True)
    s.add_argument("--summary-file", required=True)
    s.add_argument("--profile-version", default=C.DEFAULT_PROFILE_VERSION)
    s.set_defaults(fn=cmd_cache_put)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
