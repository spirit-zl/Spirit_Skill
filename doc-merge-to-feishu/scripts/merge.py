#!/usr/bin/env python3
"""多文档合并成一份 Markdown，并写入飞书云文档。

用法：
  python3 merge.py prepare <文件或目录...> [--title 标题] [--json]
  python3 merge.py push <飞书链接> [--title 标题] [--force] [--update-existing]
  python3 merge.py show [--session latest] [--json]
  python3 merge.py last

退出码：
  0 成功
  1 运行错误
  2 没有可合并的文件（或全部失败）
  3 需要用户确认（目标文档非空 / 文件夹已有同名文档）
  4 链接类型不支持
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from converters import SUPPORTED_EXTS, convert_file, ext_of  # noqa: E402
from converters.base import ImageSink  # noqa: E402
from converters.common import (  # noqa: E402
    IMG_TOKEN_RE,
    collapse_blanks,
    demote_to_start_at_h1,
    escape_md,
    natural_key,
    shift_headings,
)
import feishu  # noqa: E402

DATA_HOME = os.path.expanduser(os.environ.get("DOCMERGE_HOME", "~/.workbuddy/data/doc-merge-to-feishu"))
SESSIONS = os.path.join(DATA_HOME, "sessions")
ASSETS_DIR = "assets"
MERGED_FILE = "merged.md"


# ---------------------------------------------------------------- 文件收集


def collect_files(paths: list[str], recursive: bool) -> tuple[list[str], list[str]]:
    found: list[str] = []
    skipped: list[str] = []
    for raw in paths:
        path = os.path.abspath(os.path.expanduser(raw))
        if os.path.isdir(path):
            walker = os.walk(path) if recursive else [(path, [], os.listdir(path))]
            for root, _dirs, files in walker:
                for name in files:
                    if name.startswith((".", "~$")):
                        continue
                    if ext_of(name) in SUPPORTED_EXTS:
                        found.append(os.path.join(root, name))
            continue
        if not os.path.exists(path):
            skipped.append(f"{raw}（路径不存在）")
            continue
        if ext_of(path) not in SUPPORTED_EXTS:
            skipped.append(f"{os.path.basename(path)}（{ext_of(path) or '无扩展名'} 不支持）")
            continue
        found.append(path)
    # 去重 + 按文件名自然排序
    uniq: dict[str, str] = {}
    for path in found:
        real = os.path.realpath(path)
        uniq.setdefault(real, path)
    ordered = sorted(uniq.values(), key=lambda p: natural_key(os.path.basename(p)))
    return ordered, skipped


DOC_EXT_RE = re.compile(r"\.(?:md|markdown|pdf|docx?|txt|epub|html?|rtf|pages|wps)$", re.I)


def strip_doc_ext(name: str) -> str:
    """去掉扩展名。'xx.md.pdf' 这种二次导出的会连着去掉两层，标题里不留 .md 尾巴。"""
    base = os.path.splitext(name)[0]
    stripped = DOC_EXT_RE.sub("", base).strip()
    return stripped or base


def strip_prefix_tag(stem: str) -> str:
    """去掉文件名开头的 [202411072301] / 【合集】 这类标签，只在起标题时用。"""
    cleaned = re.sub(r"^\s*[\[【(（][^\]】)）]{0,24}[\]】)）]\s*", "", stem)
    return cleaned.strip() or stem


def suggest_title(names: list[str]) -> str:
    stems = [strip_prefix_tag(strip_doc_ext(os.path.basename(n)).strip()) for n in names]
    if not stems:
        return "合并文档"
    if len(stems) == 1:
        return stems[0][:60]

    # 「第 1 讲 / 第 2 章」这类系列：给出讲次区间
    nums = []
    for stem in stems:
        m = re.search(r"第\s*(\d+)\s*([讲章节篇课回集卷])", stem)
        if m:
            nums.append((int(m.group(1)), m.group(2)))
    if len(nums) == len(stems) and len({u for _, u in nums}) == 1:
        unit = nums[0][1]
        lo, hi = min(n for n, _ in nums), max(n for n, _ in nums)
        cleaned = [re.sub(r"^.*?第\s*\d+\s*[讲章节篇课回集卷]\s*[·\-—.、]?\s*", "", s).strip() for s in stems]
        common = os.path.commonprefix(cleaned).strip(" _-—·、:：.")
        if len(common) >= 3:
            return f"{common}（第{lo}–{hi}{unit}）"[:60]
        return f"第{lo}–{hi}{unit} 合集"[:60]

    prefix = os.path.commonprefix(stems).strip(" _-—·、:：.")
    if len(prefix) >= 3:
        return prefix[:60]
    if len(stems) <= 3:
        return "、".join(stems)[:60]
    return f"{stems[0]} 等 {len(stems)} 篇"[:60]


# ---------------------------------------------------------------- Markdown 拼装


LOOSE_KEY_RE = re.compile(r"[^0-9a-z\u3400-\u4dbf\u4e00-\u9fff]+")
LEAD_NUM_RE = re.compile(r"^\d+")
TAIL_EXT_RE = re.compile(r"(?:md|pdf|docx?|txt|epub)$")


def loose_key(s: str) -> str:
    """只留数字/字母/汉字并小写，用来比对标题与文件名是否说的是同一件事。"""
    s = re.sub(r"\s+", "", s).lower()
    s = TAIL_EXT_RE.sub("", s)
    s = LEAD_NUM_RE.sub("", s)
    return LOOSE_KEY_RE.sub("", s)


def drop_duplicate_title(md: str, stem: str) -> str:
    """文件内第一个 H1 与文件名重复时删掉它，避免 H1 下面又出现一行同名 H2。"""
    lines = md.split("\n")
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = re.match(r"^#\s+(.+)$", line)
        if not m:
            return md
        heading = m.group(1).strip("*_ ")
        base = stem
        if not loose_key(heading):
            return md
        # 文件名常是「原文标题.md.pdf」，标题里也会带 .md，比对时两边都去掉 md 残留
        heads = {loose_key(heading), loose_key(heading).replace("md", "")}
        bases = {loose_key(base), loose_key(base).replace("md", "")}
        same = any(
            h == b or (len(h) >= 4 and (h in b or b in h))
            for h in heads if h
            for b in bases if b
        )
        if same:
            rest = lines[i + 1:]
            while rest and not rest[0].strip():
                rest.pop(0)
            return "\n".join(rest)
        return md
    return md


def materialize_images(md: str, records: list[dict], no_images: bool) -> str:
    if not records:
        return md
    by_token = {rec["token"]: rec for rec in records}

    def repl(m: re.Match) -> str:
        rec = by_token.get(m.group(0))
        if not rec:
            return ""
        if no_images:
            return ""
        alt = (rec.get("alt") or "").replace("[", "\\[").replace("]", "\\]")
        return f"![{alt}](@./{ASSETS_DIR}/{rec['file']})"

    return IMG_TOKEN_RE.sub(repl, md)


def _list_kind(line: str) -> str:
    s = line.strip()
    if re.match(r"^[-*+]\s+", s):
        return "ul"
    if re.match(r"^\d+[.)]\s+", s):
        return "ol"
    if s.startswith(">"):
        return "quote"
    return ""


def tighten_lists(md: str) -> str:
    """去掉同一列表内各项之间的空行（飞书会把空行当成列表结束）。"""
    lines = md.split("\n")
    out: list[str] = []
    fence = None
    for i, line in enumerate(lines):
        stripped = line.lstrip()
        m = re.match(r"^(`{3,}|~{3,})", stripped)
        if m:
            marker = m.group(1)[0]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            out.append(line)
            continue
        if fence is None and not line.strip() and out and i + 1 < len(lines):
            prev_kind = _list_kind(out[-1])
            next_kind = _list_kind(lines[i + 1])
            if prev_kind and prev_kind == next_kind:
                continue
        out.append(line)
    return "\n".join(out)


def build_merged(entries: list[dict], keep_duplicate_title: bool, no_images: bool) -> str:
    parts = []
    for entry in entries:
        if not entry.get("ok"):
            continue
        md = entry["markdown"]
        md = demote_to_start_at_h1(md)
        if not keep_duplicate_title:
            md = drop_duplicate_title(md, entry["stem"])
            # 删掉重复的一级标题后重新对齐，避免正文从 H3 起跳
            md = demote_to_start_at_h1(md)
        md = shift_headings(md, 1)
        md = materialize_images(md, entry["images"], no_images)
        md = tighten_lists(md)
        heading = "# " + escape_md(entry["display"])
        body = collapse_blanks(md)
        parts.append(heading if not body else heading + "\n\n" + body)
    return collapse_blanks("\n\n".join(parts))


# ---------------------------------------------------------------- prepare


def cmd_prepare(args) -> int:
    files, skipped = collect_files(args.paths, args.recursive)
    if not files:
        print("没有找到可合并的文档。支持：" + "、".join(sorted(SUPPORTED_EXTS)), file=sys.stderr)
        for item in skipped:
            print("  跳过：" + item, file=sys.stderr)
        return 2

    session_id = time.strftime("%Y%m%d-%H%M%S")
    if os.path.exists(os.path.join(SESSIONS, session_id)):
        suffix = 2
        while os.path.exists(os.path.join(SESSIONS, f"{session_id}-{suffix}")):
            suffix += 1
        session_id = f"{session_id}-{suffix}"
    session_dir = os.path.join(SESSIONS, session_id)
    assets_dir = os.path.join(session_dir, ASSETS_DIR)
    os.makedirs(assets_dir, exist_ok=True)

    entries = []
    for index, path in enumerate(files, start=1):
        display = os.path.basename(path)
        entry = {
            "index": index,
            "name": display,
            "display": strip_doc_ext(display),
            "stem": strip_doc_ext(display),
            "path": path,
            "ext": ext_of(path).lstrip("."),
            "size": os.path.getsize(path),
            "ok": False,
            "chars": 0,
            "headings": 0,
            "images": [],
            "warnings": [],
            "error": "",
            "markdown": "",
        }
        sink = ImageSink(assets_dir, f"{index:02d}")
        try:
            result = convert_file(
                path,
                sink,
                max_pages=args.max_pages,
                ocr=not args.no_ocr,
                ocr_headings=args.ocr_headings,
                ocr_dpi=args.ocr_dpi,
                ocr_max_pages=args.ocr_max_pages,
                ocr_keep_image=args.ocr_keep_image,
                ocr_engine=args.ocr_engine,
            )
            entry.update(
                ok=bool(result.markdown.strip()),
                chars=len(result.markdown),
                headings=len(result.headings),
                image_count=result.image_count,
                title=result.title,
                meta=result.meta,
                markdown=result.markdown,
                images=sink.records,
            )
            entry["warnings"] = list(result.warnings)
            if not result.markdown.strip():
                entry["error"] = "解析结果为空"
        except Exception as exc:  # noqa: BLE001
            entry["error"] = f"{type(exc).__name__}: {exc}"
            entry["traceback"] = traceback.format_exc(limit=3)
        entries.append(entry)

    ok_entries = [e for e in entries if e["ok"]]
    if not ok_entries:
        print("所有文件都解析失败：", file=sys.stderr)
        for entry in entries:
            print(f"  {entry['name']}：{entry['error']}", file=sys.stderr)
        return 2

    merged = build_merged(ok_entries, args.keep_duplicate_title, args.no_images)
    merged_path = os.path.join(session_dir, MERGED_FILE)
    with open(merged_path, "w", encoding="utf-8") as f:
        f.write(merged + "\n")

    per_file_titles = [e["display"] for e in ok_entries]
    title = (args.title or "").strip() or suggest_title(per_file_titles)

    state = {
        "id": session_id,
        "dir": session_dir,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "files": [{k: v for k, v in e.items() if k not in ("markdown",)} for e in entries],
        "skipped": skipped,
        "title_suggested": title,
        "merged": {
            "path": merged_path,
            "chars": len(merged),
            "images": len([r for e in ok_entries for r in e["images"]]),
            "documents": len(ok_entries),
        },
        "push": None,
    }
    with open(os.path.join(session_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _write_pointer(session_id)

    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
    else:
        print(f"已合并 {len(ok_entries)}/{len(entries)} 个文档 → {session_dir}")
        print()
        head = f"{'#':<3}{'文件名':<40}{'类型':<7}{'字数':>8}{'标题':>5}{'图':>4}  备注"
        print(head)
        for entry in entries:
            if entry["ok"]:
                note = "；".join(entry["warnings"][:2])
                print(
                    f"{entry['index']:<3}{entry['name'][:38]:<40}{entry['ext']:<7}"
                    f"{entry['chars']:>8,}{entry['headings']:>5}{len(entry['images']):>4}  {note}"
                )
            else:
                print(f"{entry['index']:<3}{entry['name'][:38]:<40}{entry['ext']:<7}{'—':>8}{'—':>5}{'—':>4}  ⚠ {entry['error']}")
        if skipped:
            print()
            print("跳过：" + "；".join(skipped))
        print()
        print(f"合计 {state['merged']['chars']:,} 字 / {state['merged']['images']} 张图")
        print(f"建议标题：{title}")
        print()
        print("下一步：给出飞书文档或文件夹链接，用 push 写入。")
    return 0


# ---------------------------------------------------------------- stream（逐篇转换 → 逐篇写入）
#
# 用户偏好：一个文档解析完就写进飞书，再读下一个。好处是每篇落定即可核对，
# 单篇失败不影响已写入的部分，长文档也不必攒成一整块再推。


def _convert_one(path: str, index: int, assets_dir: str, args) -> dict:
    display = os.path.basename(path)
    entry = {
        "index": index,
        "name": display,
        "display": strip_doc_ext(display),
        "stem": strip_doc_ext(display),
        "path": path,
        "ext": ext_of(path).lstrip("."),
        "ok": False,
        "chars": 0,
        "headings": 0,
        "images": [],
        "warnings": [],
        "error": "",
        "markdown": "",
    }
    sink = ImageSink(assets_dir, f"{index:02d}")
    try:
        result = convert_file(
            path,
            sink,
            max_pages=args.max_pages,
            ocr=not args.no_ocr,
            ocr_headings=args.ocr_headings,
            ocr_dpi=args.ocr_dpi,
            ocr_max_pages=args.ocr_max_pages,
            ocr_keep_image=getattr(args, "ocr_keep_image", False),
            ocr_engine=getattr(args, "ocr_engine", None),
        )
        entry.update(
            ok=bool(result.markdown.strip()),
            chars=len(result.markdown),
            headings=len(result.headings),
            image_count=result.image_count,
            meta=result.meta,
            markdown=result.markdown,
            images=sink.records,
        )
        entry["warnings"] = list(result.warnings)
        if not result.markdown.strip():
            entry["error"] = "解析结果为空"
    except Exception as exc:  # noqa: BLE001
        entry["error"] = f"{type(exc).__name__}: {exc}"
    return entry


def cmd_stream(args) -> int:
    files, skipped = collect_files(args.paths, args.recursive)
    if not files:
        print("没有找到可转换的文档。支持：" + "、".join(sorted(SUPPORTED_EXTS)), file=sys.stderr)
        return 2

    target = _resolve_target(args.url)
    kind = target["kind"]
    if kind not in ("folder", "docx", "doc"):
        print(
            f"这个链接指向的是 {kind}，不是飞书文档（docx）或文件夹。",
            file=sys.stderr,
        )
        return 4

    title = (args.title or "").strip()
    if not title:
        title = suggest_title([strip_doc_ext(os.path.basename(f)) for f in files])

    session_id = time.strftime("%Y%m%d-%H%M%S") + "-stream"
    session_dir = os.path.join(SESSIONS, session_id)
    assets_dir = os.path.join(session_dir, ASSETS_DIR)
    os.makedirs(assets_dir, exist_ok=True)

    doc_url = target.get("url") or args.url
    if kind == "folder":
        doc_url = None  # 第一篇时新建

    print(f"目标：{'文件夹「' + (target['title'] or target['token']) + '」' if kind == 'folder' else title}")
    print(f"共 {len(files)} 个文件，按文件名顺序逐篇转换并写入。\n")

    done = []
    failed = []
    for i, path in enumerate(files, start=1):
        name = os.path.basename(path)
        print(f"[{i}/{len(files)}] 读取并转换：{name} …", end="", flush=True)
        entry = _convert_one(path, i, assets_dir, args)
        if not entry["ok"]:
            print(f" 跳过（{entry['error']}）")
            failed.append((name, entry["error"]))
            continue

        md = build_merged([entry], args.keep_duplicate_title, args.no_images)
        md_name = f"doc_{i:02d}.md"
        with open(os.path.join(session_dir, md_name), "w", encoding="utf-8") as f:
            f.write(md + "\n")
        print(f" {entry['chars']:,} 字 → 写入飞书 …", end="", flush=True)

        try:
            if not done:
                if kind == "folder":
                    created = feishu.create_doc(title, target["token"], md_name, session_dir)
                    doc_url = created.get("url") or (
                        f"https://www.feishu.cn/docx/{created['token']}" if created.get("token") else ""
                    )
                else:
                    feishu.overwrite_doc(doc_url, md_name, session_dir)
                mode = "新建并写入"
            else:
                feishu.append_doc(doc_url, md_name, session_dir)
                mode = "追加"
        except feishu.LarkError as exc:
            print(f" 写入失败（{exc}）")
            failed.append((name, f"写入失败：{exc}"))
            continue

        done.append(entry)
        note = f" · {'；'.join(entry['warnings'][:1])}" if entry["warnings"] else ""
        print(f" 完成（{mode}）{note}")
        if getattr(args, "delay", 0):
            time.sleep(args.delay)

    if not done:
        print("\n没有任何文档写入成功。", file=sys.stderr)
        for name, err in failed:
            print(f"  {name}：{err}", file=sys.stderr)
        return 2

    total_chars = sum(e["chars"] for e in done)
    state = {
        "id": session_id,
        "dir": session_dir,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "stream",
        "files": [{k: v for k, v in e.items() if k != "markdown"} for e in done],
        "skipped": skipped,
        "title_suggested": title,
        "merged": {
            "path": os.path.join(session_dir, "merged.md"),
            "chars": total_chars,
            "images": sum(len(e["images"]) for e in done),
            "documents": len(done),
        },
        "push": {"title": title, "url": doc_url, "mode": "stream", "folder": "", "at": time.strftime("%Y-%m-%d %H:%M:%S")},
    }
    with open(os.path.join(session_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    _write_pointer(session_id)

    print()
    print(f"已写入 {len(done)}/{len(files)} 篇，合计 {total_chars:,} 字")
    print(f"标题：{title}")
    if doc_url:
        print(f"链接：{doc_url}")
    if failed:
        print("未写入：")
        for name, err in failed:
            print(f"  {name}：{err}")
    return 0


# ---------------------------------------------------------------- push


def _resolve_target(url: str) -> dict:
    info = feishu.inspect(url)
    kind = (info.get("type") or "").lower()
    token = info.get("token") or ""
    wiki = info.get("wiki_node") or {}
    obj_type = (wiki.get("obj_type") or kind).lower()
    if kind == "wiki" or (not kind and wiki):
        node = feishu.wiki_node(url)
        if node:
            obj_type = (node.get("obj_type") or obj_type).lower()
            token = node.get("obj_token") or token
    return {
        "kind": obj_type or kind,
        "token": token,
        "title": info.get("title") or "",
        "url": info.get("url") or url,
        "wiki": bool(wiki) or kind == "wiki",
    }


def cmd_push(args) -> int:
    state, session_dir = load_session(args.session)
    if state is None:
        print("找不到合并会话，请先运行 prepare。", file=sys.stderr)
        return 1

    merged_path = os.path.join(session_dir, MERGED_FILE)
    if not os.path.isfile(merged_path):
        print(f"会话里没有 {MERGED_FILE}，请重新 prepare。", file=sys.stderr)
        return 1

    title = (args.title or "").strip() or state.get("title_suggested") or "合并文档"
    target = _resolve_target(args.url)
    kind = target["kind"]

    if kind == "folder":
        return _push_into_folder(args, state, session_dir, title, target)
    if kind in ("docx", "doc"):
        return _push_into_doc(args, state, session_dir, title, target)
    print(
        f"这个链接指向的是 {kind}，不是飞书文档（docx）或文件夹。\n"
        "请给一个文档链接（/docx/... 或 /wiki/...）或文件夹链接（/drive/folder/...）。",
        file=sys.stderr,
    )
    return 4


def _push_into_folder(args, state, session_dir, title, target) -> int:
    folder_token = target["token"]
    folder_name = target["title"] or folder_token
    existing = None
    try:
        for item in feishu.list_folder(folder_token):
            if (item.get("name") or "").strip() == title.strip():
                existing = item
                break
    except feishu.LarkError as exc:
        print(f"（无法列出文件夹内容：{exc}）", file=sys.stderr)

    if existing and not args.update_existing:
        doc_token = existing.get("token")
        print(json.dumps({
            "status": "need_confirm",
            "reason": "folder_has_same_name",
            "folder": folder_name,
            "existing_title": existing.get("name"),
            "existing_url": existing.get("url") or f"https://www.feishu.cn/docx/{doc_token}",
            "hint": "目标文件夹里已有同名文档。加 --update-existing 覆盖它，或换一个 --title 新建。",
        }, ensure_ascii=False, indent=2))
        return 3

    if args.dry_run:
        print(f"[dry-run] 将在文件夹「{folder_name}」下创建文档《{title}》")
        return 0

    if existing and args.update_existing:
        doc_url = existing.get("url") or f"https://www.feishu.cn/docx/{existing.get('token')}"
        result = feishu.overwrite_doc(doc_url, MERGED_FILE, session_dir)
        mode = "overwrite-existing"
        url = result.get("url") or doc_url
    else:
        result = feishu.create_doc(title, folder_token, MERGED_FILE, session_dir)
        mode = "create"
        url = result.get("url") or ""
        if not url and result.get("token"):
            url = f"https://www.feishu.cn/docx/{result['token']}"

    return _finish_push(state, session_dir, title, url, mode, folder=folder_name)


def _push_into_doc(args, state, session_dir, title, target) -> int:
    # wiki 链接会被 +inspect 解包成真实 docx，用解包后的地址更稳
    doc_url = target.get("url") or args.url
    doc_title = target["title"] or ""
    try:
        current = feishu.fetch_markdown(doc_url)
    except feishu.LarkError as exc:
        print(f"读取目标文档失败：{exc}", file=sys.stderr)
        return 1
    non_empty = len(re.sub(r"\s+", "", current)) > 30

    if non_empty and not args.force:
        print(json.dumps({
            "status": "need_confirm",
            "reason": "doc_not_empty",
            "doc_title": doc_title,
            "doc_url": doc_url,
            "existing_chars": len(re.sub(r"\s+", "", current)),
            "hint": "目标文档已有内容，overwrite 会整篇替换。确认后加 --force 重跑。",
        }, ensure_ascii=False, indent=2))
        return 3

    if args.dry_run:
        print(f"[dry-run] 将整篇覆盖文档《{doc_title or doc_url}》")
        return 0

    result = feishu.overwrite_doc(doc_url, MERGED_FILE, session_dir)
    url = result.get("url") or doc_url
    # 用户给的是知识库链接就回显原链接，方便他直接点回原处
    display = args.url if "/wiki/" in args.url else url
    return _finish_push(state, session_dir, doc_title or title, display, "overwrite", folder="")


def _finish_push(state, session_dir, title, url, mode, folder="") -> int:
    state["push"] = {
        "title": title,
        "url": url,
        "mode": mode,
        "folder": folder,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(os.path.join(session_dir, "session.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    if not url:
        print("已写入，但没有解析出文档链接，请到飞书里确认。", file=sys.stderr)
    mode_label = {"create": "新建文档", "overwrite": "整篇覆盖", "overwrite-existing": "覆盖同名文档"}.get(mode, mode)
    where = f"（文件夹「{folder}」下）" if folder else ""
    print(f"已写入飞书：{mode_label}{where}")
    if title:
        print(f"标题：{title}")
    if url:
        print(f"链接：{url}")
    print(f"内容：{state['merged']['chars']:,} 字 / {state['merged']['images']} 张图 / {state['merged']['documents']} 个来源文件")
    return 0


# ---------------------------------------------------------------- 会话读取


def _pointer_path() -> str:
    return os.path.join(DATA_HOME, "last_session")


def _write_pointer(session_id: str) -> None:
    os.makedirs(DATA_HOME, exist_ok=True)
    with open(_pointer_path(), "w", encoding="utf-8") as f:
        f.write(session_id)


def load_session(name: str | None = None):
    session_id = name
    if not session_id or session_id == "latest":
        if os.path.isfile(_pointer_path()):
            session_id = open(_pointer_path(), encoding="utf-8").read().strip()
        elif os.path.isdir(SESSIONS):
            candidates = sorted(os.listdir(SESSIONS))
            session_id = candidates[-1] if candidates else None
    if not session_id:
        return None, None
    session_dir = os.path.join(SESSIONS, session_id)
    state_path = os.path.join(session_dir, "session.json")
    if not os.path.isfile(state_path):
        return None, None
    with open(state_path, encoding="utf-8") as f:
        return json.load(f), session_dir


def cmd_show(args) -> int:
    state, session_dir = load_session(args.session)
    if state is None:
        print("没有合并记录。", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return 0
    print(f"会话：{state['id']}（{state['created_at']}）")
    print(f"目录：{state['dir']}")
    print(f"来源：{state['merged']['documents']} 个文件 / {state['merged']['chars']:,} 字 / {state['merged']['images']} 张图")
    print(f"建议标题：{state['title_suggested']}")
    push = state.get("push")
    if push:
        print(f"已写入：{push['mode']} → {push['url']}（{push['at']}）")
    else:
        print("尚未写入飞书。")
    return 0


def cmd_ocr(args) -> int:
    """对 PDF 的指定页试跑 OCR，用来确认扫描件能不能读出来。"""
    import pymupdf

    from converters.ocr import engine_report, ocr_page, pick_engine

    if not os.path.isfile(args.pdf):
        print(f"找不到文件：{args.pdf}", file=sys.stderr)
        return 1

    engine = pick_engine(args.engine)
    if engine is None:
        print("没有可用的 OCR 引擎：", file=sys.stderr)
        for name, ok in engine_report():
            print(f"  {'✓' if ok else '✗'} {name}", file=sys.stderr)
        print(
            "macOS 自带 Vision 应可用；其他系统装 tesseract"
            "（brew install tesseract tesseract-lang / apt install tesseract-ocr tesseract-ocr-chi-sim）；"
            "飞书 OCR 需要应用申请 optical_char_recognition:image 权限",
            file=sys.stderr,
        )
        return 1

    try:
        doc = pymupdf.open(args.pdf)
    except Exception as exc:  # noqa: BLE001
        print(f"打不开这个 PDF：{exc}", file=sys.stderr)
        return 1
    total = doc.page_count
    wanted = _parse_pages(args.pages, total)
    print(f"引擎：{engine.name}｜共 {total} 页｜本次识别 {len(wanted)} 页", file=sys.stderr)

    results = []
    for pno in wanted:
        md, lines = ocr_page(doc[pno], engine, dpi=args.dpi, headings=args.headings)
        results.append({"page": pno + 1, "lines": lines, "chars": len(md), "markdown": md})
        if not args.json:
            print(f"\n===== 第 {pno + 1} 页（{lines} 行 / {len(md)} 字）=====\n{md}")
    doc.close()

    if args.json:
        print(json.dumps({"engine": engine.name, "pages": results}, ensure_ascii=False, indent=2))
    return 0


def _parse_pages(spec: str, total: int) -> list[int]:
    if not spec or spec in ("all", "*"):
        return list(range(total))
    pages: list[int] = []
    for chunk in spec.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = chunk.split("-", 1)
            start = int(a) if a.strip() else 1
            end = int(b) if b.strip() else total
            pages.extend(range(start, end + 1))
        else:
            pages.append(int(chunk))
    out = []
    for p in pages:
        if 1 <= p <= total and (p - 1) not in out:
            out.append(p - 1)
    return out or list(range(total))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="多个文档合并为一份 Markdown 并写入飞书")
    sub = parser.add_subparsers(dest="command", required=True)

    p_prepare = sub.add_parser("prepare", help="解析并合并本地文档")
    p_prepare.add_argument("paths", nargs="+", help="文件或目录路径")
    p_prepare.add_argument("--title", help="文档标题（不给则按文件名自动总结）")
    p_prepare.add_argument("--recursive", action="store_true", help="目录递归收集")
    p_prepare.add_argument("--no-images", action="store_true", help="不保留图片")
    p_prepare.add_argument("--keep-duplicate-title", action="store_true", help="保留与文件名重复的首个一级标题")
    p_prepare.add_argument("--max-pages", type=int, default=None, help="单个 PDF 最多解析多少页（默认全部）")
    p_prepare.add_argument("--no-ocr", action="store_true", help="不对扫描版 PDF 做文字识别")
    p_prepare.add_argument("--ocr-headings", action="store_true", help="OCR 时按行高猜标题（默认全按正文）")
    p_prepare.add_argument("--ocr-dpi", type=int, default=200, help="OCR 渲染精度（默认 200，越高越慢）")
    p_prepare.add_argument("--ocr-max-pages", type=int, default=300, help="单份 PDF 最多 OCR 多少页（默认 300）")
    p_prepare.add_argument("--ocr-keep-image", action="store_true", help="OCR 后仍保留整页图片")
    p_prepare.add_argument("--ocr-engine", help="指定 OCR 引擎：vision / tesseract / 飞书")
    p_prepare.add_argument("--json", action="store_true", help="输出 JSON")
    p_prepare.set_defaults(func=cmd_prepare)

    p_stream = sub.add_parser("stream", help="按文件名顺序逐篇转换并写入飞书（一篇一写）")
    p_stream.add_argument("url", help="飞书文档或文件夹链接")
    p_stream.add_argument("paths", nargs="+", help="文件或目录路径")
    p_stream.add_argument("--title", help="新建文档时的标题")
    p_stream.add_argument("--recursive", action="store_true", help="目录递归收集")
    p_stream.add_argument("--no-images", action="store_true", help="不保留图片")
    p_stream.add_argument("--keep-duplicate-title", action="store_true", help="保留与文件名重复的首个一级标题")
    p_stream.add_argument("--max-pages", type=int, default=None, help="单个 PDF 最多解析多少页")
    p_stream.add_argument("--no-ocr", action="store_true", help="不对扫描版 PDF 做文字识别")
    p_stream.add_argument("--ocr-headings", action="store_true", help="OCR 时按行高猜标题")
    p_stream.add_argument("--ocr-dpi", type=int, default=200)
    p_stream.add_argument("--ocr-max-pages", type=int, default=300, help="单份 PDF 最多 OCR 多少页")
    p_stream.add_argument("--ocr-keep-image", action="store_true", help="OCR 后仍保留整页图片")
    p_stream.add_argument("--ocr-engine", help="指定 OCR 引擎：vision / tesseract / 飞书")
    p_stream.add_argument("--delay", type=float, default=0, help="每篇之间等待秒数（防限流）")
    p_stream.set_defaults(func=cmd_stream)

    p_push = sub.add_parser("push", help="写入飞书文档或文件夹")
    p_push.add_argument("url", help="飞书文档或文件夹链接")
    p_push.add_argument("--title", help="新建文档时的标题")
    p_push.add_argument("--session", help="指定会话 ID（默认最近一次）")
    p_push.add_argument("--force", action="store_true", help="目标文档非空时也覆盖")
    p_push.add_argument("--update-existing", action="store_true", help="文件夹里已有同名文档时覆盖它")
    p_push.add_argument("--dry-run", action="store_true", help="只打印计划，不写入")
    p_push.set_defaults(func=cmd_push)

    p_show = sub.add_parser("show", help="查看合并会话")
    p_show.add_argument("--session", default="latest")
    p_show.add_argument("--json", action="store_true")
    p_show.set_defaults(func=cmd_show)

    p_ocr = sub.add_parser("ocr", help="对 PDF 的若干页试跑 OCR")
    p_ocr.add_argument("pdf", help="PDF 路径")
    p_ocr.add_argument("--pages", default="1", help="页码范围，如 1-3,5（默认第 1 页）")
    p_ocr.add_argument("--dpi", type=int, default=200)
    p_ocr.add_argument("--headings", action="store_true", help="按行高猜标题")
    p_ocr.add_argument("--engine", help="指定引擎：vision / tesseract / 飞书")
    p_ocr.add_argument("--json", action="store_true")
    p_ocr.set_defaults(func=cmd_ocr)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return args.func(args)
    except feishu.LarkError as exc:
        print(f"飞书操作失败：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
