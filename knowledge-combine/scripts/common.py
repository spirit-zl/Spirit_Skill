"""共享工具：manifest 读写、IR 读写、指纹、issue 登记。"""
import hashlib
import json
import os
import re
from datetime import datetime, timezone

ROOT = os.path.expanduser("~/.workbuddy/data/knowledge-combine")
CACHE = os.path.join(ROOT, "_cache")

TEXT_EXT = {".md", ".markdown", ".txt", ".html", ".htm", ".pdf", ".docx", ".doc",
            ".pptx", ".xlsx", ".xls", ".csv", ".json"}
DEFAULT_EXCLUDE = {".git", ".svn", "node_modules", "__pycache__", ".venv", "venv",
                   ".idea", ".vscode", "dist", "build", "Library"}


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def new_job_id(source_type):
    return datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + source_type


def job_dir_of(manifest_path):
    return os.path.dirname(os.path.abspath(manifest_path))


def load_manifest(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_manifest(path, m):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=2)


def ir_path(job_dir):
    return os.path.join(job_dir, "ir.jsonl")


def read_ir(job_dir):
    p = ir_path(job_dir)
    if not os.path.exists(p):
        return []
    out = []
    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_ir(job_dir, records):
    os.makedirs(job_dir, exist_ok=True)
    with open(ir_path(job_dir), "a", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def rewrite_ir(job_dir, records):
    with open(ir_path(job_dir), "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def sha256_text(s):
    norm = re.sub(r"\s+", "", s or "")
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def sha256_file(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text(path, s):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(s)


def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def add_issue(m, level, ref, kind, message, severity="warning"):
    """登记一条 issue。同一 (level, ref, kind) 只保留首次，
    避免采集阶段与归一化阶段对同一问题重复记账。返回是否真的新增。"""
    issues = m.setdefault("issues", [])
    for i in issues:
        if i.get("level") == level and i.get("ref") == ref and i.get("kind") == kind:
            return False
    issues.append({
        "level": level, "ref": ref, "kind": kind, "severity": severity,
        "message": message, "at": now_iso(),
    })
    return True


def slugify(name, maxlen=60):
    s = re.sub(r"[\\/:*?\"<>|\s]+", "-", name or "untitled").strip("-")
    return s[:maxlen] or "untitled"


def source_key(source_id, content_sha):
    return "%s" % (content_sha[:16],)


DEFAULT_PROFILE_VERSION = "1.0"


def cache_path(source_key, content_sha256, profile_version=DEFAULT_PROFILE_VERSION):
    """摘要缓存路径。

    文件名带 profile 版本号：总结模板/口径改了要升版本号，
    否则会命中旧口径的摘要而复用出不一致的结果。
    """
    d = os.path.join(CACHE, (source_key or "x")[:2])
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.json" % ((content_sha256 or "x")[:16],
                                           profile_version or DEFAULT_PROFILE_VERSION))


def make_record(job_dir, sid, origin, title, path_or_url, text, media_type="OTHER",
                breadcrumb=None, modified_at="", images=None, completeness=None,
                parse_note="", extra=None):
    """写正文快照并返回一条 IR 记录。"""
    raw_dir = os.path.join(job_dir, "raw")
    os.makedirs(raw_dir, exist_ok=True)
    cf = os.path.join(raw_dir, "%s.md" % sid)
    write_text(cf, text or "")
    sha = sha256_text(text or "")
    if completeness is None:
        n = len((text or "").strip())
        if n == 0:
            completeness = "parse_failed"
            parse_note = (parse_note + "; " if parse_note else "") + "正文为空"
        elif n < 50:
            completeness = "empty"
            parse_note = (parse_note + "; " if parse_note else "") + "内容过短（%d 字符）" % n
        else:
            completeness = "no_truncation_detected"
    return {
        "source_id": sid,
        "source_key": sha[:16],
        "origin": origin,
        "title": title or os.path.basename(path_or_url or sid),
        "path_or_url": path_or_url,
        "breadcrumb": breadcrumb or [],
        "media_type": media_type,
        "modified_at": modified_at,
        "author": "",
        "content_file": cf,
        "content_chars": len(text or ""),
        "content_sha256": sha,
        "anchors": [],
        "completeness": completeness,
        "parse_note": parse_note,
        "images": images or [],
        "duplicate_of": None,
        "priority": {"local": 3, "lark": 2, "ima": 1}.get(origin, 0),
        "extra": extra or {},
    }


def next_sid(job_dir):
    return "S%d" % (len(read_ir(job_dir)) + 1)


def est_tokens(chars):
    """粗略估算：中文按 1.5 字符/token，英文按 4 字符/token，取保守值。"""
    return int(chars / 2.2)
