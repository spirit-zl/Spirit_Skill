#!/usr/bin/env python3
"""微信公众号文章采集 · 主入口

两步走（因为「文章精华」需要人来判断，脚本不替你编）：

  1) run   —— 读多维表格 → 找出只有链接的记录 → 抓文章 → 解析清洗 → 存 pending.json
  2) write —— 读 pending.json + summaries.json → 补齐列 → 写回表格

典型用法：
    python3 collect.py run  --base "https://my.feishu.cn/wiki/xxxx"
    # 模型读 pending.json，把每篇 200 字精华写进 summaries.json
    python3 collect.py write

目标文档会记住：下次不给 --base 时，run 只打印「上次用的文档」并以退出码 3 停下，
等用户确认（--confirm-last 继续，或 --base 换成新链接）。

单独命令：
    python3 collect.py last            看上次用的文档（名称+链接）
    python3 collect.py status          看上一次采集的待写/已完成
    python3 collect.py run --force     已有内容也重抓（覆盖）
    python3 collect.py run --limit 3   只跑前 3 条，试水
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import feishu  # noqa: E402
import wxarticle as W  # noqa: E402

HOME = os.path.expanduser(os.environ.get("WXAC_HOME", "~/.workbuddy/data/wechat-article-collect"))
ARCHIVE = os.path.join(HOME, "公众号下载")
SESSIONS = os.path.join(HOME, "sessions")
CONFIG = os.path.join(HOME, "config.json")

# 列定义：名字即契约，13 字段里挑这四个 + 既有的链接列
FIELD_SPECS = [
    {"name": "文章标题", "type": "text"},
    {"name": "发表日期", "type": "datetime", "style": {"format": "yyyy-MM-dd"}},
    {"name": "文章正文", "type": "text"},
    {"name": "文章精华", "type": "text"},
]
LINK_FIELD = "文章链接"
SUMMARY_LIMIT = 200
EXIT_NEED_CONFIRM = 3


# ────────────────────────────── 小工具 ──────────────────────────────

def session_dir(base_token: str, table_id: str) -> str:
    d = os.path.join(SESSIONS, f"{base_token}_{table_id}")
    os.makedirs(d, exist_ok=True)
    return d


def latest_session() -> str:
    if not os.path.isdir(SESSIONS):
        raise SystemExit("还没有采集记录，先跑 run")
    dirs = [os.path.join(SESSIONS, d) for d in os.listdir(SESSIONS)]
    dirs = [d for d in dirs if os.path.exists(os.path.join(d, "pending.json"))]
    if not dirs:
        raise SystemExit("还没有采集记录，先跑 run")
    return max(dirs, key=os.path.getmtime)


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def dump_json(path: str, data) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)


def brief(text: str, n: int = 40) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def cn_len(text: str) -> int:
    """按汉字个数计（用户口径是「不超过 200 个汉字」）。"""
    return sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")


# ────────────────────────────── 记住上次的文档 ──────────────────────────────

def load_config() -> dict:
    if not os.path.exists(CONFIG):
        return {}
    try:
        return load_json(CONFIG)
    except (json.JSONDecodeError, OSError):
        return {}


def save_config(cfg: dict) -> None:
    os.makedirs(HOME, exist_ok=True)
    tmp = CONFIG + ".tmp"
    dump_json(tmp, cfg)
    os.replace(tmp, CONFIG)


def remembered() -> dict | None:
    return (load_config().get("last") or {}).get("target")


def remember(info: dict, base_input: str, records: int | None = None) -> dict:
    """把本次目标文档记进 config，供下次询问用。"""
    cfg = load_config()
    prev = (cfg.get("last") or {}).get("target") or {}
    target = {
        # 文档名的优先级：Base 标题（如「六语先生」）> 表名 > token
        "doc_title": info.get("base_title") or (base_input if not base_input.startswith("http") else "") or info.get("title") or "未命名文档",
        "doc_url": base_input if base_input.startswith("http") else prev.get("doc_url", ""),
        "base_token": info["base_token"],
        "table_id": info["table_id"],
        "table_title": info.get("title", ""),
        "records": records,
        "used_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    cfg["last"] = {"target": target}
    save_config(cfg)
    return target


# ────────────────────────────── run ──────────────────────────────

def cmd_run(args) -> int:
    base_input = args.base
    if not base_input:
        last = remembered()
        if not last:
            print("还没有记录过目标文档。请用 --base 传多维表格链接，例如：")
            print('  python3 collect.py run --base "https://my.feishu.cn/wiki/xxxx"')
            return 2
        if args.confirm_last:
            base_input = last.get("doc_url") or last.get("base_token")
            print(f"沿用上次的文档：《{last.get('doc_title')}》{last.get('doc_url') or last.get('base_token')}")
        else:
            print("上次使用的文档是：")
            print(f"  文档名称：《{last.get('doc_title')}》")
            print(f"  文档链接：{last.get('doc_url') or '（只记了 base_token）' + str(last.get('base_token'))}")
            print(f"  数据表：{last.get('table_title') or last.get('table_id')}　上次使用：{last.get('used_at')}")
            print()
            print("请先向用户确认是否继续用这个文档：")
            print("  继续 → 加 --confirm-last 再执行一次")
            print("  换文档 → 用 --base 传新链接（记住的文档会随之更新）")
            return EXIT_NEED_CONFIRM

    info = feishu.resolve(base_input, args.table)
    base_token, table_id = info["base_token"], info["table_id"]
    workdir = session_dir(base_token, table_id)

    records = feishu.list_records(base_token, table_id, ["文章标题", "文章正文", "文章精华"])
    target = remember(info, base_input, records=len(records))
    print(f"目标：《{target['doc_title']}》{target['doc_url'] or base_token}　表：{info.get('title') or table_id}")

    # 同一张表里可能有人把同一篇文章粘了两行：先建 URL → 已完成记录 的索引，
    # 命中的直接复用正文/精华，不重复下载、不重复写精华。
    done_by_url: dict[str, dict] = {}
    for rec in records:
        u = feishu.cell_url(rec.get(LINK_FIELD))
        if u and feishu.cell_text(rec.get("文章标题")) and feishu.cell_text(rec.get("文章正文")):
            done_by_url.setdefault(u, rec)

    todo, done, skip = [], 0, 0
    for rec in records:
        url = feishu.cell_url(rec.get(LINK_FIELD))
        if not url:
            skip += 1
            continue
        has_title = bool(feishu.cell_text(rec.get("文章标题")))
        has_body = bool(feishu.cell_text(rec.get("文章正文")))
        if has_title and has_body and not args.force:
            done += 1
            continue
        rec["_url"] = url
        rec["_reuse"] = None if args.force else done_by_url.get(url)
        todo.append(rec)

    if args.limit:
        todo = todo[: args.limit]

    print(f"共 {len(records)} 条：已提取 {done} 条、跳过 {skip} 条（无链接）、本次待采 {len(todo)} 条")
    if not todo:
        # 不覆盖上一次的 pending.json：里面可能还有没写精华的记录
        print("没有需要采集的记录。")
        return 0

    rows, failed, reused = [], [], 0
    for idx, rec in enumerate(todo, 1):
        url = rec["_url"]
        label = f"[{idx}/{len(todo)}]"
        src = rec.get("_reuse")
        if src:
            # 表里已经有同一篇文章：直接搬过来
            body = feishu.cell_text(src.get("文章正文"))
            rows.append({
                "record_id": rec["record_id"],
                "链接": feishu.cell_url(rec.get(LINK_FIELD)) or url,
                "文章标题": feishu.cell_text(src.get("文章标题")),
                "发表日期": feishu.cell_date(src.get("发表日期")),
                "公众号": "",
                "作者": "",
                "字数": len(body),
                "文章正文": body,
                "归档": "",
                "已存在精华": feishu.cell_text(rec.get("文章精华")),
                "预填精华": feishu.cell_text(src.get("文章精华")),
                "复用自": src["record_id"],
            })
            reused += 1
            print(f"{label} ↺ 同一链接已存在，复用 {src['record_id']}：{brief(feishu.cell_text(src.get('文章标题')))}")
            continue
        try:
            html = W.fetch(url, retries=args.retries)
            meta = W.parse(html)
            body = W.clean(meta["正文原文"], meta["标题"])
            if len(body) < 30:
                raise W.WxError("正文清洗后几乎为空，可能是图片型文章或有偿阅读")
            archive = ""
            if not args.no_archive:
                archive = W.save_archive(html, meta, ARCHIVE)
            rows.append({
                "record_id": rec["record_id"],
                "链接": feishu.cell_url(rec.get(LINK_FIELD)) or url,
                "文章标题": meta["标题"],
                "发表日期": meta["日期"] or "",
                "公众号": meta["公众号"],
                "作者": meta["作者"],
                "字数": len(body),
                "文章正文": body,
                "归档": archive,
                "已存在精华": feishu.cell_text(rec.get("文章精华")),
                "预填精华": "",
                "复用自": "",
            })
            print(f"{label} ✓ {meta['日期'] or '日期缺失'} | {brief(meta['标题'])} | {len(body)} 字")
        except Exception as exc:  # noqa: BLE001 - 单篇失败不影响整批
            failed.append({"record_id": rec["record_id"], "链接": url, "原因": str(exc)})
            print(f"{label} ✗ {url} → {exc}")
        if idx < len(todo):
            time.sleep(args.delay)

    dump_json(os.path.join(workdir, "pending.json"), {
        "base_token": base_token,
        "table_id": table_id,
        "table_title": info.get("title", ""),
        "collected_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "rows": rows,
        "failed": failed,
    })

    print(f"\n采集完成：成功 {len(rows)} 篇（其中复用 {reused} 篇），失败 {len(failed)} 篇")
    if failed:
        for f in failed:
            print(f"  ✗ {f['链接']} — {f['原因']}")
    print(f"待写文件：{os.path.join(workdir, 'pending.json')}")
    print("下一步：为每篇写不超过 200 汉字的「文章精华」到")
    print(f"  {os.path.join(workdir, 'summaries.json')}")
    print("  格式：{\"records\": {\"<record_id>\": \"精华文字\"}}")
    print(f"然后执行：python3 {os.path.abspath(__file__)} write")
    return 0


# ────────────────────────────── write ──────────────────────────────

def load_summaries(path: str) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"找不到精华文件：{path}\n先按 pending.json 写好每篇的精华再执行 write。")
    data = load_json(path)
    if isinstance(data, dict) and "records" in data:
        data = data["records"]
    if not isinstance(data, dict):
        raise SystemExit("summaries.json 格式应为 {\"records\": {\"<record_id>\": \"...\"}}")
    return data


def cmd_write(args) -> int:
    workdir = args.workdir or latest_session()
    pending = load_json(os.path.join(workdir, "pending.json"))
    rows = pending.get("rows") or []
    if not rows:
        print("没有待写入的记录。")
        return 0
    if pending.get("written_at") and not args.force:
        print(f"这批记录已于 {pending['written_at']} 写回，未重复写入。")
        print("要强制重写（例如补写精华）加 --force。")
        return 0

    summaries = load_summaries(args.summaries or os.path.join(workdir, "summaries.json"))
    by_url = {}
    for rid, val in summaries.items():
        if isinstance(val, dict):
            val = val.get("精华") or val.get("summary") or ""
        by_url[rid] = (val or "").strip()

    base_token, table_id = pending["base_token"], pending["table_id"]
    created = feishu.ensure_fields(base_token, table_id, FIELD_SPECS)
    if created:
        print(f"已新增列：{'、'.join(created)}")

    updates, missing = {}, []
    for row in rows:
        rid = row["record_id"]
        # 优先级：模型新写的精华 > 表里已有的预填精华（重复链接复用来的）
        summary = by_url.get(rid) or by_url.get(row["链接"]) or row.get("预填精华") or ""
        if not summary:
            missing.append(row["链接"])
        if cn_len(summary) > SUMMARY_LIMIT:
            summary = summary[: SUMMARY_LIMIT]
            print(f"  ! 精华超过 {SUMMARY_LIMIT} 汉字，已截断：{brief(row['文章标题'], 20)}")
        payload = {
            "文章标题": row["文章标题"],
            "文章正文": row["文章正文"],
        }
        if row.get("发表日期"):
            payload["发表日期"] = f"{row['发表日期']} 00:00"
        if summary:
            payload["文章精华"] = summary
        updates[rid] = payload

    if args.dry_run:
        print(f"[dry-run] 将更新 {len(updates)} 条，缺精华 {len(missing)} 条")
        return 0

    written = feishu.batch_update(base_token, table_id, updates, workdir)

    pending["written_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    dump_json(os.path.join(workdir, "pending.json"), pending)
    done_file = os.path.join(workdir, "done.json")
    dump_json(done_file, {
        "written_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "records": [
            {"record_id": r["record_id"], "文章标题": r["文章标题"],
             "发表日期": r.get("发表日期", ""), "字数": r.get("字数", 0)}
            for r in rows
        ],
    })
    print(f"已写回 {written} 条记录（标题 / 发表日期 / 正文 / 精华）")
    if missing:
        print(f"有 {len(missing)} 条没给精华，正文已写入、精华留空：")
        for u in missing:
            print(f"  - {u}")
    return 0


# ────────────────────────────── status ──────────────────────────────

def cmd_status(args) -> int:
    workdir = args.workdir or latest_session()
    pending = load_json(os.path.join(workdir, "pending.json"))
    rows = pending.get("rows") or []
    failed = pending.get("failed") or []
    print(f"会话目录：{workdir}")
    print(f"采集时间：{pending.get('collected_at')}　表格：{pending.get('table_title')}")
    print(f"成功 {len(rows)} 篇，失败 {len(failed)} 篇")
    if pending.get("written_at"):
        print(f"写回时间：{pending['written_at']}")
    summ_path = os.path.join(workdir, "summaries.json")
    if os.path.exists(summ_path):
        s = load_summaries(summ_path)
        print(f"精华文件：{len(s)} 条已填 / 共 {len(rows)} 条")
    else:
        print("精华文件：尚未生成")
    for r in rows:
        print(f"  · {r.get('发表日期') or '----'} {brief(r['文章标题'], 34)} ({r.get('字数')} 字)")
    for f in failed:
        print(f"  ✗ {f['链接']} — {f['原因']}")
    return 0


# ────────────────────────────── last ──────────────────────────────

def cmd_last(args) -> int:
    last = remembered()
    if not last:
        if args.json:
            print(json.dumps({"target": None}, ensure_ascii=False))
        else:
            print("还没有记录过目标文档。")
        return 1
    if args.json:
        print(json.dumps({"target": last}, ensure_ascii=False, indent=2))
    else:
        print(f"文档名称：《{last.get('doc_title')}》")
        print(f"文档链接：{last.get('doc_url') or '（只记了 base_token）' + str(last.get('base_token'))}")
        print(f"数据表：{last.get('table_title') or last.get('table_id')}")
        print(f"上次使用：{last.get('used_at')}　当时记录数：{last.get('records')}")
    return 0


# ────────────────────────────── CLI ──────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="微信公众号文章采集（多维表格驱动）")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="读表 → 抓取待采文章")
    r.add_argument("--base", help="多维表格链接（wiki/base）或 base_token；不给则回落到上次记住的文档")
    r.add_argument("--confirm-last", action="store_true",
                   help="确认沿用上次记住的文档（不给 --base 时必填）")
    r.add_argument("--table", help="表名或 table_id，默认取记录最多的那张")
    r.add_argument("--limit", type=int, help="只采前 N 条")
    r.add_argument("--force", action="store_true", help="已有标题/正文也重新抓取")
    r.add_argument("--delay", type=float, default=1.5, help="两篇之间的间隔秒数，默认 1.5")
    r.add_argument("--retries", type=int, default=2, help="单篇重试次数")
    r.add_argument("--no-archive", action="store_true", help="不把 HTML 归档到本地")
    r.set_defaults(func=cmd_run)

    w = sub.add_parser("write", help="把标题/日期/正文/精华写回表格")
    w.add_argument("--workdir", help="会话目录，默认最近一次 run")
    w.add_argument("--summaries", help="精华 JSON，默认 <workdir>/summaries.json")
    w.add_argument("--dry-run", action="store_true")
    w.add_argument("--force", action="store_true", help="已写回过的批次也重写")
    w.set_defaults(func=cmd_write)

    s = sub.add_parser("status", help="查看上一次采集状态")
    s.add_argument("--workdir")
    s.set_defaults(func=cmd_status)

    l = sub.add_parser("last", help="查看上次使用的文档（名称+链接）")
    l.add_argument("--json", action="store_true")
    l.set_defaults(func=cmd_last)

    args = p.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
