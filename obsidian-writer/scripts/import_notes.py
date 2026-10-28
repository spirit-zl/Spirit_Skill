#!/usr/bin/env python3
"""Obsidian 笔记导入器（来源无关）。

消费 note plan（plan.json），把已经成文的草稿按 Obsidian 规范写入 vault 的
000inbox/note，并维护 vault 之外的去重索引。

plan 由 plan_from_synthesis.py 从 knowledge-synthesis 的作业产出生成，
也可手工编写（契约见 references/plan-contract.md）。

硬约束：
- 只消费 plan，不调用任何外部数据源、不重新总结正文
- 不执行任何 git 写操作（add/commit/push）
- 永不覆盖已存在的笔记文件
- 校验失败一律不写 vault

用法：
  import_notes.py --plan P --vault-root V --mode check
  import_notes.py --plan P --vault-root V --mode apply
"""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

SCHEMA_VERSION = "1.0"
DEFAULT_BRIDGE_ROOT = "/Users/spirit/.workbuddy/data/obsidian-bridge"
DEFAULT_INBOX_REL = "000inbox/note"
CST = timezone(timedelta(hours=8))
SUMMARY_PROFILE_VERSION = "1.0"

# 旧版（ima-obsidian-bridge）字段名，仅用于读取历史笔记，新写入一律用通用名
LEGACY_NOTE_ID = "ima_note_id"
LEGACY_FINGERPRINT = "ima_input_fingerprint"
LEGACY_REVISION = "ima_revision"

FORBIDDEN_PATTERNS = [
    (re.compile(r"^```+\s*dataview", re.I | re.M), "dataview 代码块"),
    (re.compile(r"^```+\s*dataviewjs", re.I | re.M), "dataviewjs 代码块"),
    (re.compile(r"<%.*?%>", re.S), "Templater 语法"),
    (re.compile(r"<script", re.I), "HTML script 标签"),
]
REQUIRED_SECTIONS = ["## 摘要", "## 关键结论", "## 溯源信息"]


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

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


def atomic_write_text(path, text):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def atomic_write_json(path, obj, backup=True):
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if backup and os.path.exists(path):
        shutil.copy2(path, path + ".bak")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def resolve_job_uri(job_dir, uri):
    if not isinstance(uri, str) or not uri.startswith("job://"):
        raise ValueError("非法 URI（必须以 job:// 开头）: %r" % uri)
    rel = uri[len("job://"):]
    if rel.startswith("/") or ".." in rel.split("/"):
        raise ValueError("非法 URI（绝对路径或含 ..）: %r" % uri)
    target = os.path.realpath(os.path.join(job_dir, rel))
    root = os.path.realpath(job_dir)
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("URI 逃逸出 plan 目录: %r" % uri)
    if not os.path.isfile(target):
        raise ValueError("URI 指向的文件不存在: %r" % uri)
    return target


def sanitize_filename(title, max_len=120):
    s = unicodedata.normalize("NFC", title or "")
    out = []
    for ch in s:
        if ch in '/\\:*?"<>|' or ord(ch) < 32:
            out.append("-")
        elif ch.isspace():
            out.append("-")
        else:
            out.append(ch)
    s = "".join(out)
    while "--" in s:
        s = s.replace("--", "-")
    s = s.strip(" .-")
    if not s:
        s = "note-" + datetime.now(CST).strftime("%Y%m%d-%H%M%S")
    return s[:max_len]


def yaml_scalar(v):
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(str(v), ensure_ascii=False)


def render_frontmatter(fields):
    """按固定字段顺序渲染 YAML frontmatter（不依赖 PyYAML）。"""
    order = [
        "title", "created", "updated", "source", "tags",
        "source_scope", "source_scope_name",
        "source_ids", "source_titles", "source_hashes",
        "job_id", "note_id", "note_key", "input_fingerprint",
        "aggregation", "revision", "partial", "supersedes",
    ]
    lines = ["---"]
    for k in order:
        if k not in fields:
            continue
        v = fields[k]
        if isinstance(v, list):
            if not v:
                lines.append("%s: []" % k)
            else:
                lines.append("%s:" % k)
                for item in v:
                    lines.append("  - %s" % yaml_scalar(item))
        else:
            lines.append("%s: %s" % (k, yaml_scalar(v)))
    for k in sorted(fields):
        if k in order:
            continue
        v = fields[k]
        if isinstance(v, list):
            lines.append("%s:" % k)
            for item in v:
                lines.append("  - %s" % yaml_scalar(item))
        else:
            lines.append("%s: %s" % (k, yaml_scalar(v)))
    lines.append("---")
    return "\n".join(lines) + "\n"


def parse_frontmatter(text):
    """从笔记文本中解析 frontmatter（仅支持本脚本生成的扁平结构）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end < 0:
        return {}
    block = text[3:end]
    fields = {}
    key = None
    for raw in block.split("\n"):
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
            k = k.strip()
            v = v.strip()
            if v == "":
                fields[k] = []
                key = k
            else:
                fields[k] = _unquote(v)
                key = k
    return fields


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


def fm_note_id(fm):
    """读取笔记标识，兼容旧版 ima_* 字段。"""
    return fm.get("note_id") or fm.get(LEGACY_NOTE_ID)


def fm_fingerprint(fm):
    return fm.get("input_fingerprint") or fm.get(LEGACY_FINGERPRINT)


def fm_revision(fm):
    return int(fm.get("revision") or fm.get(LEGACY_REVISION) or 1)


def rule_check(body):
    """正文规则检查。返回 (errors, warnings)。"""
    errors, warnings = [], []
    h1 = [l for l in body.split("\n") if re.match(r"^#\s+\S", l)]
    if len(h1) == 0:
        errors.append("正文缺少一级标题（# ）")
    elif len(h1) > 1:
        errors.append("正文存在多个一级标题：%s" % " / ".join(h1[:3]))
    for pat, name in FORBIDDEN_PATTERNS:
        if pat.search(body):
            errors.append("检测到 vault 未装插件不支持的语法：%s" % name)
    for sec in REQUIRED_SECTIONS:
        if sec not in body:
            warnings.append("缺少建议章节：%s" % sec)
    return errors, warnings


# --------------------------------------------------------------------------
# 锁
# --------------------------------------------------------------------------

class Lock:
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, "w")
        try:
            fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.fh.close()
            raise SystemExit("另一个导入进程正在运行（锁：%s）" % self.path)
        self.fh.write("%d\n" % os.getpid())
        self.fh.flush()
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fh, fcntl.LOCK_UN)
        finally:
            self.fh.close()


# --------------------------------------------------------------------------
# 校验
# --------------------------------------------------------------------------

def validate_plan(m, plan_dir):
    errors = []
    if str(m.get("schema_version", "")).split(".")[0] != SCHEMA_VERSION.split(".")[0]:
        errors.append("schema 主版本不匹配: %s" % m.get("schema_version"))

    plan = m.get("plan", {}) or {}
    status = plan.get("status")
    if status not in ("ready", "ready_noop", "partial"):
        errors.append("plan.status 不允许导入: %s" % status)
    if status == "partial":
        policy = m.get("policy", {}) or {}
        if not policy.get("allow_partial") or not policy.get("partial_approved_at"):
            errors.append("partial 状态未获批准（policy.allow_partial / partial_approved_at）")

    if not m.get("source_system"):
        errors.append("source_system 为空")

    by_key = {}
    for i, s in enumerate(m.get("sources", [])):
        for k in ("source_key", "origin", "locator", "content_sha256"):
            if not s.get(k):
                errors.append("sources[%d] 缺少 %s" % (i, k))
        by_key[s.get("source_key")] = s

    for i, n in enumerate(m.get("notes", [])):
        for k in ("note_id", "group_key", "source_keys", "input_fingerprint",
                  "proposed_title", "draft_uri", "status"):
            if not n.get(k):
                errors.append("notes[%d] 缺少 %s" % (i, k))
        for sk in n.get("source_keys", []):
            if sk not in by_key:
                errors.append("notes[%d] 引用不存在的 source_key %s" % (i, sk))
        try:
            p = resolve_job_uri(plan_dir, n.get("draft_uri", ""))
            if n.get("draft_sha256") and sha256_file(p) != n.get("draft_sha256"):
                errors.append("notes[%d] draft_sha256 与文件不符（草稿被改动过？先跑 "
                              "plan_from_synthesis.py refresh）" % i)
        except Exception as e:
            errors.append("notes[%d] draft URI 校验失败: %s" % (i, e))
    return errors


def scan_claimable(inbox_dir, note_id, fingerprint):
    """崩溃恢复：找出已写入但索引未登记的笔记文件。"""
    if not os.path.isdir(inbox_dir):
        return None
    for name in sorted(os.listdir(inbox_dir)):
        if not name.endswith(".md"):
            continue
        p = os.path.join(inbox_dir, name)
        if not os.path.isfile(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
        except Exception:
            continue
        fm = parse_frontmatter(text)
        if fm_note_id(fm) == note_id and fm_fingerprint(fm) == fingerprint:
            return p
    return None


def plan_filename(inbox_dir, title, note_id, is_revision):
    base = sanitize_filename(title)
    cand = os.path.join(inbox_dir, base + ".md")
    if not os.path.exists(cand):
        return cand
    cand2 = os.path.join(inbox_dir, "%s--%s.md" % (base, note_id[:8]))
    if not os.path.exists(cand2):
        return cand2
    suffix = "updated-" + datetime.now(CST).strftime("%Y%m%d-%H%M%S") \
        if is_revision else note_id[:8] + "-" + datetime.now(CST).strftime("%H%M%S")
    return os.path.join(inbox_dir, "%s--%s.md" % (base, suffix))


def build_note_text(m, note, sources_by_key, revision, supersedes_basename):
    plan = m.get("plan", {}) or {}
    origin = m.get("source_system", "")
    refs = [sources_by_key[k] for k in note["source_keys"] if k in sources_by_key]
    partial = plan.get("status") == "partial"
    scope = m.get("source_scope", {}) or {}
    fields = {
        "title": note["proposed_title"],
        "created": now_iso(),
        "updated": now_iso(),
        "source": origin,
        "tags": ["ai-summary", origin] if origin else ["ai-summary"],
        "source_scope": scope.get("id") or None,
        "source_scope_name": scope.get("name") or None,
        "source_ids": [s.get("locator", "") for s in refs],
        "source_titles": [s.get("title", "") for s in refs],
        "source_hashes": ["sha256:" + s.get("content_sha256", "") for s in refs],
        "job_id": m.get("job_id") or plan.get("producer_job_id") or "",
        "note_id": note["note_id"],
        "note_key": note["group_key"],
        "input_fingerprint": note["input_fingerprint"],
        "aggregation": note.get("aggregation_mode", "single"),
        "revision": revision,
        "partial": partial,
        "supersedes": ("[[%s|旧版笔记]]" % supersedes_basename) if supersedes_basename else None,
    }
    draft_path = resolve_job_uri(os.path.dirname(os.path.abspath(m["_plan_path"])), note["draft_uri"])
    with open(draft_path, "r", encoding="utf-8") as f:
        body = f.read()
    if not body.endswith("\n"):
        body += "\n"
    return render_frontmatter(fields) + "\n" + body


def run(args):
    ppath = os.path.abspath(args.plan)
    if not os.path.isfile(ppath):
        raise SystemExit("plan 不存在: %s" % ppath)
    plan_dir = os.path.dirname(ppath)
    with open(ppath, "r", encoding="utf-8") as f:
        m = json.load(f)
    m["_plan_path"] = ppath

    errors = validate_plan(m, plan_dir)
    if errors:
        print(json.dumps({"stage": "validate", "ok": False, "errors": errors},
                         ensure_ascii=False, indent=2))
        sys.exit(1)

    vault_root = os.path.abspath(args.vault_root)
    inbox = os.path.realpath(os.path.join(vault_root, args.inbox_rel))
    if not inbox.startswith(os.path.realpath(vault_root) + os.sep):
        raise SystemExit("inbox 路径逃逸出 vault: %s" % inbox)
    os.makedirs(inbox, exist_ok=True)

    index_path = args.index or os.path.join(args.bridge_root, "index", "import-index.json")
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    index = {"schema_version": SCHEMA_VERSION, "updated_at": None,
             "vault_root": vault_root, "sources": {}, "groups": {}}
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            index = json.load(f)
    index.setdefault("sources", {})
    index.setdefault("groups", {})

    sources_by_key = {s["source_key"]: s for s in m.get("sources", [])}
    notes = [n for n in m.get("notes", []) if n.get("status") == "ready"]

    with Lock(os.path.join(args.bridge_root, "locks", "import.lock")):
        plans = []
        for note in notes:
            gkey = note["group_key"]
            fp = note["input_fingerprint"]
            group = index["groups"].get(gkey)

            # 1) 索引命中且目标文件仍在 → 整篇跳过（最快路径）
            if group and group.get("latest_input_fingerprint") == fp and not args.force:
                latest_name = (group.get("latest_note_uri", "") or "").split("/")[-1]
                if latest_name and os.path.isfile(os.path.join(inbox, latest_name)):
                    plans.append({"note_id": note["note_id"], "status": "skipped_already_imported",
                                  "reason": "同 group 同 input_fingerprint 已导入",
                                  "target": os.path.join(inbox, latest_name)})
                    continue

            # 2) 崩溃恢复：文件已写入但索引未登记 → 认领（并在 apply 时补登索引）
            claimed = scan_claimable(inbox, note["note_id"], fp)
            if claimed:
                plans.append({"note_id": note["note_id"], "status": "claimed",
                              "reason": "文件已存在且指纹一致，认领为已完成",
                              "target": claimed, "_claim_path": claimed})
                continue

            # 3) 新建或修订
            if group:
                revision = int(group.get("revision", 1)) + 1
                prev_name = group.get("latest_note_uri", "").split("/")[-1]
                supersedes = prev_name[:-3] if prev_name.endswith(".md") else (prev_name or None)
            else:
                revision = 1
                supersedes = None

            target = plan_filename(inbox, note["proposed_title"], note["note_id"], revision > 1)
            text = build_note_text(m, note, sources_by_key, revision, supersedes)
            body = text.split("\n---\n", 1)[-1]
            rerr, rwarn = rule_check(body)
            plans.append({
                "note_id": note["note_id"],
                "status": "blocked" if rerr else "create",
                "revision": revision,
                "supersedes": supersedes,
                "target": target,
                "note_sha256": sha256_text(text),
                "rule_errors": rerr,
                "rule_warnings": rwarn,
                "_text": text,
            })

        blocked = [p for p in plans if p["status"] == "blocked"]
        is_partial = m.get("plan", {}).get("status") == "partial"
        result = {
            "stage": "plan",
            "mode": args.mode,
            "plan_path": ppath,
            "job_id": m.get("plan", {}).get("producer_job_id"),
            "plan_status": m.get("plan", {}).get("status"),
            "is_partial": is_partial,
            "inbox": inbox,
            "plans": [{k: v for k, v in p.items() if not k.startswith("_")} for p in plans],
            "blocked": len(blocked),
        }

        if blocked or args.mode == "check":
            result["next"] = "fix-then-apply" if blocked else "apply"
            print(json.dumps(result, ensure_ascii=False, indent=2))
            sys.exit(1 if blocked else 0)
        if is_partial:
            result["partial_notice"] = "已批准的 partial：笔记将带 partial: true 标记"

        def register_index(note, target_path, note_sha, revision):
            """把一次成功导入登记进索引（groups + sources）。"""
            gkey = note["group_key"]
            rel = os.path.relpath(target_path, vault_root).replace(os.sep, "/")
            rec = {
                "job_id": m.get("plan", {}).get("producer_job_id"),
                "note_id": note["note_id"],
                "input_fingerprint": note["input_fingerprint"],
                "note_uri": "vault://" + rel,
                "note_sha256": note_sha,
                "revision": revision,
                "status": "imported",
                "imported_at": now_iso(),
            }
            g = index["groups"].setdefault(gkey, {"revision": 0, "imports": []})
            g["latest_input_fingerprint"] = note["input_fingerprint"]
            g["latest_note_id"] = note["note_id"]
            g["latest_note_uri"] = rec["note_uri"]
            g["latest_note_sha256"] = note_sha
            g["revision"] = revision
            g["imports"].append(rec)
            for sk in note["source_keys"]:
                s = sources_by_key.get(sk, {})
                e = index["sources"].setdefault(sk, {
                    "source_system": m.get("source_system"),
                    "locator": s.get("locator"),
                    "title": s.get("title"),
                    "imports": [],
                })
                e["imports"].append({
                    "content_sha256": s.get("content_sha256"),
                    "completeness": s.get("completeness"),
                    "group_key": gkey,
                    "job_id": rec["job_id"],
                    "note_id": note["note_id"],
                    "note_uri": rec["note_uri"],
                    "imported_at": rec["imported_at"],
                    "status": "imported",
                })
            return rec

        # ---- apply ----
        attempt_id = str(uuid.uuid4())
        applied = []
        for p in plans:
            note = next(n for n in notes if n["note_id"] == p["note_id"])

            if p["status"] == "claimed":
                with open(p["_claim_path"], "r", encoding="utf-8") as f:
                    existing = f.read()
                revision = fm_revision(parse_frontmatter(existing))
                rec = register_index(note, p["_claim_path"], sha256_file(p["_claim_path"]), revision)
                p["vault_uri"] = rec["note_uri"]
                p["revision"] = revision
                applied.append({k: v for k, v in p.items() if not k.startswith("_")})
                continue

            if p["status"] != "create":
                applied.append({k: v for k, v in p.items() if not k.startswith("_")})
                continue
            target = p["target"]
            if os.path.exists(target):
                p["status"] = "conflict"
                p["reason"] = "目标文件在写入前已存在，拒绝覆盖"
                applied.append({k: v for k, v in p.items() if k != "_text"})
                continue
            atomic_write_text(target, p["_text"])
            rec = register_index(note, target, sha256_text(p["_text"]), p["revision"])
            p["vault_uri"] = rec["note_uri"]
            p["status"] = "created"
            applied.append({k: v for k, v in p.items() if not k.startswith("_")})

        index["updated_at"] = now_iso()
        atomic_write_json(index_path, index)

        receipt = {
            "attempt_id": attempt_id,
            "job_id": m.get("plan", {}).get("producer_job_id"),
            "plan_path": ppath,
            "plan_digest": sha256_file(ppath),
            "mode": "apply",
            "started_at": now_iso(),
            "finished_at": now_iso(),
            "status": "success" if all(p["status"] in ("created", "claimed",
                                                       "skipped_already_imported")
                                       for p in plans) else "partial_success",
            "notes": applied,
        }
        receipts_dir = os.path.join(plan_dir, "receipts")
        os.makedirs(receipts_dir, exist_ok=True)
        rpath = os.path.join(receipts_dir, "%s.json" % attempt_id)
        atomic_write_json(rpath, receipt, backup=False)

        out = {
            "stage": "apply",
            "is_partial": is_partial,
            "receipt_path": rpath,
            "index_path": index_path,
            "created": sum(1 for p in plans if p["status"] == "created"),
            "skipped": sum(1 for p in plans if p["status"] == "skipped_already_imported"),
            "claimed": sum(1 for p in plans if p["status"] == "claimed"),
            "conflict": sum(1 for p in plans if p["status"] == "conflict"),
            "notes": applied,
            "git": "未执行任何 git 写操作（add/commit/push）",
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))


def main():
    p = argparse.ArgumentParser(description="Obsidian 笔记导入器")
    p.add_argument("--plan", required=True)
    p.add_argument("--vault-root", required=True)
    p.add_argument("--inbox-rel", default=DEFAULT_INBOX_REL)
    p.add_argument("--bridge-root", default=DEFAULT_BRIDGE_ROOT)
    p.add_argument("--index", default=None)
    p.add_argument("--mode", choices=["check", "apply"], default="check")
    p.add_argument("--force", action="store_true", help="忽略 input_fingerprint 命中，强制重建（生成修订）")
    args = p.parse_args()
    run(args)


if __name__ == "__main__":
    main()
