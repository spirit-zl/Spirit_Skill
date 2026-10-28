#!/usr/bin/env python3
"""manifest 生命周期：init / authorize / show / add-issue / validate / finalize / report"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common as C

DEFAULT_POLICY = {
    "max_chars_per_chunk": 12000,
    "chunk_overlap": 300,
    "max_docs": 200,
    "confirm_threshold": 50,
    "allow_partial": True,
    "image_policy": "recover",
    "use_ocr": True,
    "profile_version": "1.0",
}


def cmd_init(a):
    job_id = C.new_job_id(a.source_type)
    jd = os.path.join(C.ROOT, job_id)
    os.makedirs(os.path.join(jd, "raw"), exist_ok=True)
    os.makedirs(os.path.join(jd, "img"), exist_ok=True)
    os.makedirs(os.path.join(jd, "final"), exist_ok=True)
    policy = dict(DEFAULT_POLICY)
    if a.policy_json:
        policy.update(json.loads(a.policy_json))
    m = {
        "job_id": job_id,
        "job_dir": jd,
        "created_at": C.now_iso(),
        "source": {
            "type": a.source_type,
            "selection": json.loads(a.selection_json) if a.selection_json else {},
            "filters": json.loads(a.filters_json) if a.filters_json else {},
        },
        "output": json.loads(a.output_json) if a.output_json else {},
        "policy": policy,
        "intent": json.loads(a.intent_json) if a.intent_json else {},
        "authorized_scopes": [],
        "sources": [],
        "images": [],
        "chunks": [],
        "summaries": {"source": {}, "group": {}, "global": None},
        "outline": None,
        "outputs": [],
        "issues": [],
        "status": "draft",
    }
    mp = os.path.join(jd, "manifest.json")
    C.save_manifest(mp, m)
    print(json.dumps({"manifest_path": mp, "job_dir": jd}, ensure_ascii=False))


def cmd_authorize(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    if a.scope not in m["authorized_scopes"]:
        m["authorized_scopes"].append(a.scope)
        m.setdefault("policy", {})["_last_mode"] = a.mode
        C.save_manifest(mp, m)
    print(json.dumps({"authorized_scopes": m["authorized_scopes"],
                      "already": a.scope in m["authorized_scopes"]}, ensure_ascii=False))


def cmd_show(a):
    m = C.load_manifest(os.path.abspath(a.manifest))
    if a.field:
        print(json.dumps(m.get(a.field), ensure_ascii=False, indent=2))
    else:
        print(json.dumps(m, ensure_ascii=False, indent=2))


def cmd_add_issue(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    C.add_issue(m, a.level, a.ref or "", a.kind, a.message, a.severity)
    C.save_manifest(mp, m)
    print(json.dumps({"issues": len(m["issues"])}, ensure_ascii=False))


def cmd_validate(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    errors = []
    ir = C.read_ir(m["job_dir"])
    included = [r for r in ir if r.get("duplicate_of") is None
                and r.get("completeness") != "parse_failed"]
    if not included:
        errors.append("没有可用资料（全部解析失败或重复）")
    if not m.get("outputs"):
        errors.append("没有任何已落盘的输出")
    for i in m.get("issues", []):
        if i.get("severity") == "error":
            errors.append("[%s] %s %s" % (i.get("kind"), i.get("ref"), i.get("message")))
    print(json.dumps({"ok": not errors, "errors": errors,
                      "included": len(included)}, ensure_ascii=False, indent=2))
    return 0 if not errors else 2


def cmd_finalize(a):
    mp = os.path.abspath(a.manifest)
    m = C.load_manifest(mp)
    ir = C.read_ir(m["job_dir"])
    skipped = [r for r in ir if r.get("completeness") == "parse_failed" or r.get("duplicate_of")]
    truncation = [r for r in ir if r.get("completeness") == "suspected_truncated"]
    failed_out = [i for i in m.get("issues", [])
                  if i.get("severity") == "error" or i.get("kind") == "upload_failed"]
    if not m.get("outputs"):
        status = "failed"
    elif failed_out or skipped or truncation:
        status = "partial"
    else:
        status = "ready"
    m["status"] = status
    m["finished_at"] = C.now_iso()
    C.save_manifest(mp, m)
    print(json.dumps({"status": status, "outputs": len(m["outputs"]),
                      "skipped": len(skipped), "truncated": len(truncation)},
                     ensure_ascii=False))


def cmd_report(a):
    m = C.load_manifest(os.path.abspath(a.manifest))
    ir = C.read_ir(m["job_dir"])
    rows = {
        "job_id": m["job_id"],
        "status": m["status"],
        "source": m["source"],
        "output": m["output"],
        "included": [r["source_id"] for r in ir
                     if not r.get("duplicate_of") and r.get("completeness") != "parse_failed"],
        "duplicates": [r["source_id"] for r in ir if r.get("duplicate_of")],
        "parse_failed": [r["source_id"] for r in ir if r.get("completeness") == "parse_failed"],
        "thin": [r["source_id"] for r in ir if r.get("completeness") == "empty"],
        "suspected_truncated": [r["source_id"] for r in ir
                                if r.get("completeness") == "suspected_truncated"],
        "images": {"ok": sum(1 for x in m.get("images", []) if x.get("status") == "ok"),
                   "degraded": sum(1 for x in m.get("images", []) if x.get("status") == "degraded"),
                   "failed": sum(1 for x in m.get("images", []) if x.get("status") == "failed")},
        "outputs": m.get("outputs", []),
        "issues": m.get("issues", []),
    }
    print(json.dumps(rows, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description="knowledge-combine job manifest")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init")
    s.add_argument("--source-type", required=True, choices=["ima", "lark", "local"])
    s.add_argument("--selection-json")
    s.add_argument("--filters-json")
    s.add_argument("--output-json")
    s.add_argument("--intent-json")
    s.add_argument("--policy-json")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("authorize")
    s.add_argument("--manifest", required=True)
    s.add_argument("--scope", required=True)
    s.add_argument("--mode", default="create", choices=["create", "overwrite"])
    s.set_defaults(fn=cmd_authorize)

    s = sub.add_parser("show")
    s.add_argument("--manifest", required=True)
    s.add_argument("--field")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("add-issue")
    s.add_argument("--manifest", required=True)
    s.add_argument("--level", default="source")
    s.add_argument("--ref")
    s.add_argument("--kind", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--severity", default="warning", choices=["warning", "error"])
    s.set_defaults(fn=cmd_add_issue)

    s = sub.add_parser("validate")
    s.add_argument("--manifest", required=True)
    s.set_defaults(fn=cmd_validate)

    s = sub.add_parser("finalize")
    s.add_argument("--manifest", required=True)
    s.set_defaults(fn=cmd_finalize)

    s = sub.add_parser("report")
    s.add_argument("--manifest", required=True)
    s.set_defaults(fn=cmd_report)

    a = p.parse_args()
    sys.exit(a.fn(a) or 0)


if __name__ == "__main__":
    main()
