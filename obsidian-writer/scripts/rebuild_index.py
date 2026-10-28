#!/usr/bin/env python3
"""从 vault 笔记的 frontmatter 重建 import-index.json（索引损坏/丢失时的兜底）。

只读取，不修改笔记。默认 --mode check 仅输出报告；--mode apply 才覆写索引
（覆写前自动生成 .bak）。

用法：
  rebuild_index.py --vault-root V [--index I] [--bridge-root R] [--mode check|apply]
"""

import argparse
import hashlib
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1.0"
DEFAULT_BRIDGE_ROOT = "/Users/spirit/.workbuddy/data/obsidian-bridge"
DEFAULT_INBOX_REL = "000inbox/note"
CST = timezone(timedelta(hours=8))

# 旧版（ima-obsidian-bridge）字段名，兼容读取
LEGACY = {
    "note_id": "ima_note_id",
    "note_key": "ima_group_key",
    "input_fingerprint": "ima_input_fingerprint",
    "revision": "ima_revision",
    "job_id": "ima_job_id",
    "source_ids": "ima_media_ids",
    "source_titles": "ima_source_titles",
    "source_hashes": "ima_content_hashes",
}


def now_iso():
    return datetime.now(CST).isoformat()


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def source_key_of(origin, locator):
    return hashlib.sha256(("%s\0%s" % (origin, locator)).encode("utf-8")).hexdigest()


def _unquote(v):
    if len(v) >= 2 and v[0] == '"' and v[-1] == '"':
        try:
            return json.loads(v)
        except Exception:
            return v[1:-1]
    if v == "null":
        return None
    if v == "true":
        return True
    if v == "false":
        return False
    return v


def parse_frontmatter(text):
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    fields = {}
    key = None
    for raw in text[3:end].split("\n"):
        if not raw.strip():
            continue
        if raw.startswith("  - ") and key:
            fields.setdefault(key, [])
            if not isinstance(fields[key], list):
                fields[key] = []
            fields[key].append(_unquote(raw[4:].strip()))
            continue
        if ":" in raw:
            k, v = raw.split(":", 1)
            k, v = k.strip(), v.strip()
            if v == "":
                fields[k] = []
                key = k
            else:
                fields[k] = _unquote(v)
                key = k
    return fields


def _get(fm, name):
    """按通用名取字段，取不到回退旧版 ima_* 字段名。"""
    v = fm.get(name)
    if v in (None, "", []) and LEGACY.get(name):
        v = fm.get(LEGACY[name])
    return v


def main():
    ap = argparse.ArgumentParser(description="从 frontmatter 重建导入索引")
    ap.add_argument("--vault-root", required=True)
    ap.add_argument("--inbox-rel", default=DEFAULT_INBOX_REL)
    ap.add_argument("--bridge-root", default=DEFAULT_BRIDGE_ROOT)
    ap.add_argument("--index", default=None)
    ap.add_argument("--mode", choices=["check", "apply"], default="check")
    args = ap.parse_args()

    vault_root = os.path.abspath(args.vault_root)
    inbox = os.path.join(vault_root, args.inbox_rel)
    if not os.path.isdir(inbox):
        raise SystemExit("inbox 目录不存在: %s" % inbox)

    index = {"schema_version": SCHEMA_VERSION, "updated_at": None,
             "vault_root": vault_root, "sources": {}, "groups": {}}
    problems = []
    seen_note_ids = {}

    for name in sorted(os.listdir(inbox)):
        if not name.endswith(".md"):
            continue
        path = os.path.join(inbox, name)
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        fm = parse_frontmatter(text)
        note_id = _get(fm, "note_id")
        gkey = _get(fm, "note_key")
        if not note_id or not gkey:
            continue  # 非本流程产出的笔记，跳过
        if note_id in seen_note_ids:
            problems.append("重复 note_id: %s（%s 与 %s）"
                            % (note_id, seen_note_ids[note_id], name))
        seen_note_ids[note_id] = name

        rel = os.path.relpath(path, vault_root).replace(os.sep, "/")
        rec = {
            "job_id": _get(fm, "job_id"),
            "note_id": note_id,
            "input_fingerprint": _get(fm, "input_fingerprint"),
            "note_uri": "vault://" + rel,
            "note_sha256": sha256_file(path),
            "revision": int(_get(fm, "revision") or 1),
            "status": "imported",
            "imported_at": fm.get("created") or None,
        }
        g = index["groups"].setdefault(gkey, {"revision": 0, "imports": []})
        g["imports"].append(rec)
        g["imports"].sort(key=lambda r: int(r.get("revision") or 0))
        latest = g["imports"][-1]
        g["latest_input_fingerprint"] = latest["input_fingerprint"]
        g["latest_note_id"] = latest["note_id"]
        g["latest_note_uri"] = latest["note_uri"]
        g["latest_note_sha256"] = latest["note_sha256"]
        g["revision"] = int(latest["revision"] or 1)

        origin = fm.get("source") or "unknown"
        ids = _get(fm, "source_ids") or []
        hashes = _get(fm, "source_hashes") or []
        titles = _get(fm, "source_titles") or []
        for i, loc in enumerate(ids):
            skey = source_key_of(origin, loc)
            e = index["sources"].setdefault(skey, {
                "source_system": origin,
                "locator": loc,
                "title": titles[i] if i < len(titles) else "",
                "imports": [],
            })
            raw = hashes[i] if i < len(hashes) else None
            e["imports"].append({
                "content_sha256": (str(raw)[7:] if raw and str(raw).startswith("sha256:")
                                   else raw),
                "completeness": None,
                "group_key": gkey,
                "job_id": rec["job_id"],
                "note_id": note_id,
                "note_uri": rec["note_uri"],
                "imported_at": rec["imported_at"],
                "status": "imported",
            })

    index["updated_at"] = now_iso()
    index_path = args.index or os.path.join(args.bridge_root, "index", "import-index.json")

    report = {
        "mode": args.mode,
        "inbox": inbox,
        "groups": len(index["groups"]),
        "sources": len(index["sources"]),
        "notes": sum(len(g["imports"]) for g in index["groups"].values()),
        "problems": problems,
        "index_path": index_path,
    }

    if args.mode == "apply":
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        if os.path.exists(index_path):
            shutil.copy2(index_path, index_path + ".bak")
        tmp = index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, index_path)
        report["written"] = True

    print(json.dumps(report, ensure_ascii=False, indent=2))
    if problems:
        sys.exit(1)


if __name__ == "__main__":
    main()
