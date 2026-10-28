#!/usr/bin/env python3
"""把 knowledge-synthesis 的作业产出转成 Obsidian note plan。

读取 knowledge-synthesis 的 job 目录（manifest.json + ir.jsonl + final/*.md），
为每一篇成文生成一条 note（草稿 + 溯源信息），产出 plan.json 供
import_notes.py 消费。本脚本**不写 vault**。

关键设计：
- group_key = `ks:<sha256(规范化标题)[:16]>`，即「笔记身份 = 标题」。
  标题不变、来源内容变了 → 同一 group，导入时判为新修订；改了标题 → 视为新笔记。
- source_key = sha256(origin + "\\0" + locator)，标识「来源文档」而非「来源内容」，
  内容更新时 source_key 不变、content_sha256 变 → input_fingerprint 变 → 触发修订。
- 成文正文里的 [S<n>] 锚点决定该笔记引用哪些来源；没有锚点时退化为全部纳入来源。

子命令：
  build   从 KS manifest 生成 plan
  refresh 草稿被人工编辑后重算 draft_sha256
  show    汇总打印一份 plan
"""

import argparse
import hashlib
import json
import os
import re
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

CST = timezone(timedelta(hours=8))
SCHEMA_VERSION = "1.0"
SUMMARY_PROFILE_VERSION = "1.0"
DEFAULT_BRIDGE_ROOT = "/Users/spirit/.workbuddy/data/obsidian-bridge"

ANCHOR_RE = re.compile(r"\[S(\d+)\]")
H1_RE = re.compile(r"(?m)^#\s+(.+?)\s*$")


def now_iso():
    return datetime.now(CST).isoformat(timespec="seconds")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_ir(job_dir):
    p = os.path.join(job_dir, "ir.jsonl")
    out = []
    if not os.path.isfile(p):
        return out
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def norm_title(title):
    s = unicodedata.normalize("NFC", title or "")
    return re.sub(r"\s+", "", s)


def slugify(name, maxlen=60):
    s = unicodedata.normalize("NFC", name or "untitled")
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", s).strip("-")
    while "--" in s:
        s = s.replace("--", "-")
    return s[:maxlen] or "untitled"


def source_key_of(origin, locator):
    return hashlib.sha256(("%s\0%s" % (origin, locator)).encode("utf-8")).hexdigest()


def group_key_of(title):
    return "ks:" + hashlib.sha256(norm_title(title).encode("utf-8")).hexdigest()[:16]


def input_fingerprint_of(group_key, sources, draft_sha256,
                         profile_version=SUMMARY_PROFILE_VERSION):
    """幂等指纹。

    必须含 draft_sha256：否则「补草稿后重新导入」会被判为已导入而跳过，
    人工改的内容永远进不了 vault。含进来之后，任何草稿改动都会自然生成修订版——
    与「永不覆盖 + 修订」的语义一致。
    """
    payload = "\n".join(
        [group_key, profile_version, draft_sha256 or ""] +
        ["%s:%s:%s" % (s["source_key"], s["content_sha256"], s.get("completeness") or "")
         for s in sorted(sources, key=lambda x: x["source_key"])]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def doc_title(body, fallback):
    m = H1_RE.search(body or "")
    if not m:
        return fallback
    t = m.group(1).strip().strip("*").strip()
    return t or fallback


def _ym_of(plan_id):
    """从 plan_id 前缀推出 YYYY/MM；不是日期前缀就用当天。"""
    if re.match(r"^\d{8}", plan_id or ""):
        return "%s/%s" % (plan_id[:4], plan_id[4:6])
    return datetime.now(CST).strftime("%Y/%m")


def derive_scope(manifest):
    """从 KS manifest 推出 (source_system, scope_id, scope_name)。"""
    src = manifest.get("source", {}) or {}
    stype = src.get("type") or "unknown"
    sel = src.get("selection", {}) or {}
    locator = sel.get("path") or sel.get("url") or sel.get("locator") or ""
    if stype == "local":
        return stype, (os.path.abspath(locator) if locator else None), \
            (os.path.basename(os.path.abspath(locator)) if locator else None)
    if stype == "lark":
        return stype, locator or None, sel.get("title") or locator or None
    if stype == "ima":
        return stype, sel.get("knowledge_base_id") or None, sel.get("knowledge_base_name") or None
    return stype, locator or None, None


def build(args):
    mpath = os.path.abspath(args.manifest)
    if not os.path.isfile(mpath):
        raise SystemExit("manifest 不存在: %s" % mpath)
    job_dir = os.path.dirname(mpath)
    manifest = json.loads(read_text(mpath))
    ir = read_ir(job_dir)

    included = [r for r in ir
                if not r.get("duplicate_of") and r.get("completeness") != "parse_failed"]
    if not included:
        raise SystemExit("job 里没有可用来源（全部解析失败或重复）")
    by_sid = {r["source_id"]: r for r in included}

    # 成文文档：优先 manifest.outputs[]，其次 <job_dir>/final/*.md
    docs = []
    for o in manifest.get("outputs", []) or []:
        loc = o.get("location") or ""
        if o.get("target") == "local" and loc.lower().endswith(".md") and os.path.isfile(loc):
            docs.append(loc)
    if not docs:
        fdir = os.path.join(job_dir, "final")
        if os.path.isdir(fdir):
            docs = [os.path.join(fdir, n) for n in sorted(os.listdir(fdir))
                    if n.endswith(".md") and not n.startswith("_")]
    if not docs:
        raise SystemExit("找不到成文文档：先在 knowledge-synthesis 里 emit 到本地，"
                         "或把成文 md 放进 %s/final/" % job_dir)

    stype, scope_id, scope_name = derive_scope(manifest)
    # 默认沿用上游 job_id 作为 plan_id：同一个 KS job 反复 build 落在同一目录，
    # 「批准 partial 后重跑」不会散落出多个 plan。
    plan_id = args.plan_id or (manifest.get("job_id")
                              or (datetime.now(CST).strftime("%Y%m%d-%H%M%S") + "-" + stype))
    out_root = args.out_root or DEFAULT_BRIDGE_ROOT
    plan_dir = os.path.join(out_root, "plans", _ym_of(plan_id), plan_id)
    drafts_dir = os.path.join(plan_dir, "drafts")
    if os.path.isdir(drafts_dir):
        for old in os.listdir(drafts_dir):     # 清掉上一次 build 留下的草稿
            p = os.path.join(drafts_dir, old)
            if os.path.isfile(p):
                os.remove(p)

    issues = manifest.get("issues", []) or []
    partial = bool(
        manifest.get("status") == "partial"
        or [i for i in issues if i.get("severity") == "error" or i.get("kind") == "upload_failed"]
        or [r for r in ir if r.get("completeness") in ("parse_failed", "suspected_truncated")]
        or [r for r in ir if r.get("duplicate_of")]
    )

    # 汇总 plan 级 sources（全部纳入来源，供 note 引用）
    plan_sources = {}
    for r in included:
        locator = r.get("path_or_url") or r.get("source_id")
        sk = source_key_of(r.get("origin") or stype, locator)
        plan_sources[sk] = {
            "source_key": sk,
            "source_id": r.get("source_id"),
            "title": r.get("title") or "",
            "origin": r.get("origin") or stype,
            "locator": locator,
            "breadcrumb": r.get("breadcrumb") or [],
            "content_sha256": r.get("content_sha256") or "",
            "chars": r.get("content_chars", 0),
            "completeness": r.get("completeness") or "",
        }

    notes = []
    unresolved = []
    for doc in docs:
        body = read_text(doc).rstrip("\n")
        fallback = os.path.splitext(os.path.basename(doc))[0]
        title = doc_title(body, fallback)
        refs = sorted({int(x) for x in ANCHOR_RE.findall(body)})
        ref_sources = []
        for n in refs:
            r = by_sid.get("S%d" % n)
            if r:
                sk = source_key_of(r.get("origin") or stype, r.get("path_or_url") or r["source_id"])
                ref_sources.append(plan_sources[sk])
            else:
                unresolved.append({"doc": os.path.basename(doc), "anchor": "S%d" % n,
                                   "reason": "锚点在上游 job 里不存在（可能指向已去重或解析失败的来源）"})
        if not ref_sources:
            ref_sources = list(plan_sources.values())

        if "## 溯源信息" not in body:
            lines = ["", "## 溯源信息", ""]
            for s in sorted(ref_sources, key=lambda x: x["source_id"]):
                loc = s["locator"]
                if len(loc) > 120:
                    loc = loc[:117] + "..."
                lines.append("- [%s] 《%s》；来源：%s；定位：`%s`；正文指纹：`sha256:%s`"
                             % (s["source_id"], s["title"], s["origin"], loc,
                                (s["content_sha256"] or "")[:16]))
            body = body + "\n" + "\n".join(lines) + "\n"

        note_id = str(uuid.uuid4())
        gkey = group_key_of(title)
        draft_name = "%s-%s.md" % (note_id[:8], slugify(title))
        draft_path = os.path.join(plan_dir, "drafts", draft_name)
        write_text(draft_path, body if body.endswith("\n") else body + "\n")
        dsha = sha256_file(draft_path)
        fp = input_fingerprint_of(gkey, ref_sources, dsha)

        notes.append({
            "note_id": note_id,
            "group_key": gkey,
            "aggregation_mode": "single",
            "source_keys": sorted({s["source_key"] for s in ref_sources}),
            "input_fingerprint": fp,
            "proposed_title": title,
            "draft_uri": "job://drafts/%s" % draft_name,
            "draft_sha256": dsha,
            "status": "ready",
            "generated_at": now_iso(),
            "summary_profile_version": SUMMARY_PROFILE_VERSION,
            "source_doc": doc,
        })

    approved_at = now_iso() if args.approve_partial else None
    plan = {
        "schema_version": SCHEMA_VERSION,
        "plan": {
            "plan_id": plan_id,
            "status": "partial" if partial else "ready",
            "created_at": now_iso(),
            "producer": "knowledge-synthesis",
            "producer_job_id": manifest.get("job_id"),
            "producer_manifest": mpath,
        },
        "job_id": manifest.get("job_id"),
        "source_system": stype,
        "source_scope": {"id": scope_id, "name": scope_name},
        "policy": {
            "summary_profile": "knowledge-note-zh",
            "summary_profile_version": SUMMARY_PROFILE_VERSION,
            "allow_partial": bool(args.approve_partial) and partial,
            "partial_approved_at": approved_at if partial else None,
        },
        "sources": sorted(plan_sources.values(), key=lambda x: x["source_id"]),
        "notes": notes,
        "dedup": {"index_checked_at": None, "skipped_source_keys": [], "skipped_group_keys": []},
    }

    ppath = os.path.join(plan_dir, "plan.json")
    write_text(ppath, json.dumps(plan, ensure_ascii=False, indent=2))

    out = {
        "plan_path": ppath,
        "plan_dir": plan_dir,
        "plan_status": plan["plan"]["status"],
        "sources": len(plan["sources"]),
        "notes": len(plan["notes"]),
        "titles": [n["proposed_title"] for n in plan["notes"]],
        "unresolved_anchors": unresolved,
        "partial_reasons": ([
            "有解析失败/疑似截断/重复来源被跳过"
        ] if partial else []),
        "next": ("需要用户批准不完整内容后加 --approve-partial 重跑"
                 if partial and not args.approve_partial
                 else "python3 scripts/import_notes.py --plan %s --vault-root <V> --mode check" % ppath),
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


def refresh(args):
    """草稿被人工编辑后，重算 draft_sha256 **并同步重算 input_fingerprint**。

    指纹里含 draft_sha256，只改哈希不改指纹的话，编辑过的草稿会被判为「已导入」而跳过。
    """
    ppath = os.path.abspath(args.plan)
    plan_dir = os.path.dirname(ppath)
    plan = json.loads(read_text(ppath))
    by_key = {s["source_key"]: s for s in plan.get("sources", [])}
    changed = []
    for n in plan.get("notes", []):
        rel = n["draft_uri"][len("job://"):]
        p = os.path.join(plan_dir, rel)
        if not os.path.isfile(p):
            raise SystemExit("草稿不存在: %s" % p)
        new = sha256_file(p)
        if new == n.get("draft_sha256"):
            continue
        refs = [by_key[k] for k in n.get("source_keys", []) if k in by_key]
        old_fp = n.get("input_fingerprint")
        n["draft_sha256"] = new
        n["input_fingerprint"] = input_fingerprint_of(n["group_key"], refs, new)
        changed.append({"note_id": n["note_id"], "old": old_fp, "new": n["input_fingerprint"]})
    write_text(ppath, json.dumps(plan, ensure_ascii=False, indent=2))
    print(json.dumps({"stage": "refresh", "plan_path": ppath,
                      "refreshed": len(changed),
                      "note": "指纹已随草稿重算，重新导入会生成修订版", "changed": changed},
                     ensure_ascii=False, indent=2))


def show(args):
    plan = json.loads(read_text(os.path.abspath(args.plan)))
    print(json.dumps({
        "plan_id": plan["plan"]["plan_id"],
        "status": plan["plan"]["status"],
        "source_system": plan.get("source_system"),
        "source_scope": plan.get("source_scope"),
        "policy": plan.get("policy"),
        "sources": [{"source_id": s["source_id"], "title": s["title"],
                     "origin": s["origin"], "chars": s.get("chars"),
                     "completeness": s.get("completeness")} for s in plan.get("sources", [])],
        "notes": [{"note_id": n["note_id"], "title": n["proposed_title"],
                   "group_key": n["group_key"], "sources": len(n["source_keys"]),
                   "revision_scope": "同标题再次导入即为修订"} for n in plan.get("notes", [])],
    }, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description="knowledge-synthesis → Obsidian note plan")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("build", help="从 KS manifest 生成 plan")
    s.add_argument("--manifest", required=True, help="knowledge-synthesis 的 manifest.json 绝对路径")
    s.add_argument("--out-root", default=DEFAULT_BRIDGE_ROOT)
    s.add_argument("--plan-id")
    s.add_argument("--approve-partial", action="store_true",
                   help="用户已明确批准以不完整内容导入时才加")
    s.set_defaults(fn=build)

    s = sub.add_parser("refresh", help="草稿被人工编辑后重算 draft_sha256")
    s.add_argument("--plan", required=True)
    s.set_defaults(fn=refresh)

    s = sub.add_parser("show", help="汇总打印一份 plan")
    s.add_argument("--plan", required=True)
    s.set_defaults(fn=show)

    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
