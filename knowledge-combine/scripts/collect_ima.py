#!/usr/bin/env python3
"""ima 源登记。

MCP 只能由 agent 调用，本脚本负责把 agent 取回的正文登记成 IR：
    mcp__ima-mcp__search_knowledge_base → 定位库
    mcp__ima-mcp__get_knowledge_list    → 逐层递归目录（禁止 filter_out FOLDER）
    mcp__ima-mcp__fetch_media_content   → 取正文（长 PDF 约 10.5 万字符静默截断）
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

IMA_TRUNCATION_CHAR = 100000  # fetch_media_content 实际上限约 10.5 万字符


def guess_completeness(item, chars):
    given = item.get("completeness")
    if given in ("no_truncation_detected", "suspected_truncated", "parse_failed", "empty"):
        return given
    if item.get("truncated") is True:
        return "suspected_truncated"
    if chars >= IMA_TRUNCATION_CHAR:
        return "suspected_truncated"
    if chars == 0:
        return "parse_failed"
    if chars < 50:
        return "empty"
    return "no_truncation_detected"


def cmd_record(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    jd = m["job_dir"]
    items = json.loads(C.read_text(a.items))
    records = []
    for it in items:
        sid = C.next_sid(jd)
        text = C.read_text(it["content_file"]) if it.get("content_file") and os.path.exists(it["content_file"]) else ""
        chars = len(text)
        comp = guess_completeness(it, chars)
        rec = C.make_record(
            jd, sid, "ima", it.get("title"), it.get("media_id") or it.get("path_or_url", ""),
            text, it.get("media_type", "OTHER"),
            breadcrumb=it.get("folder_path", []),
            modified_at=it.get("modified_at", ""),
            completeness=comp,
            parse_note="ima 源；字符数 %d" % chars,
            extra={"media_id": it.get("media_id"), "kb_id": it.get("kb_id", m["source"].get("kb_id"))})
        if comp == "parse_failed":
            C.add_issue(m, "source", sid, "parse_failed", "正文为空或过短：%s" % it.get("title"))
        elif comp == "suspected_truncated":
            C.add_issue(m, "source", sid, "suspected_truncated",
                        "字符数 %d，疑似被截断：%s" % (chars, it.get("title")))
        records.append(rec)
        C.append_ir(jd, [rec])
    m["status"] = "collected"
    C.save_manifest(mp, m)
    print(json.dumps({"recorded": len(records),
                      "suspected_truncated": sum(1 for r in records if r["completeness"] == "suspected_truncated"),
                      "parse_failed": sum(1 for r in records if r["completeness"] == "parse_failed")},
                     ensure_ascii=False))


def cmd_check_truncation(a):
    """把目录/TOC 关键词与正文比对，判断末章是否缺失。"""
    text = C.read_text(a.file)
    tails = [t.strip() for t in a.tail_keywords.split(",") if t.strip()]
    missing = [t for t in tails if t not in text]
    print(json.dumps({"chars": len(text), "missing_tails": missing,
                      "suspected_truncated": bool(missing) or len(text) >= IMA_TRUNCATION_CHAR},
                     ensure_ascii=False))


def main():
    p = argparse.ArgumentParser(description="record ima items")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("record")
    s.add_argument("--manifest", required=True)
    s.add_argument("--items", required=True, help="items.json 路径")
    s.set_defaults(fn=cmd_record)

    s = sub.add_parser("check-truncation")
    s.add_argument("--file", required=True)
    s.add_argument("--tail-keywords", default="", help="末章/附录关键词，逗号分隔")
    s.set_defaults(fn=cmd_check_truncation)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
