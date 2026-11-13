#!/usr/bin/env python3
"""push_feishu.py —— 把一期回放内容前插到飞书文档最前面（不改动旧内容）

用法：
    python3 push_feishu.py --xml episode.xml --date 2026-11-12 [--doc <url|token>] [--dry-run]

行为：
  1. 先读目标文档的 Markdown，若已存在同名标题（趋势复利（日期））则直接退出 3，避免重复插入；
  2. 用 block_insert_after --block-id 0 把内容插到文档最前面（旧内容整体下移，实现日期倒序）；
  3. 回读校验：新标题必须是文档里第一个一级标题，否则退出 6。

退出码：0 成功；3 已存在（跳过）；4 需登录；6 写入或校验失败
"""

import argparse
import json
import os
import re
import subprocess
import sys

CONFIG = os.path.expanduser("~/.workbuddy/data/sztg-replay/config.json")


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def run(args, check=True):
    p = subprocess.run(args, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise SystemExit(f"命令失败：{' '.join(args)}\n{p.stderr[-500:]}\n{p.stdout[-500:]}")
    return p


def fetch_markdown(doc):
    p = run(["lark-cli", "docs", "+fetch", "--doc", doc, "--doc-format", "markdown", "--as", "user"])
    try:
        payload = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise SystemExit("解析 fetch 结果失败：\n" + p.stdout[:500])
    if not payload.get("ok"):
        if "identity" in payload and payload.get("error", {}).get("type") == "auth":
            raise SystemExit(4)
        raise SystemExit("读取文档失败：\n" + json.dumps(payload, ensure_ascii=False)[:800])
    return payload["data"]["document"]["content"]


def body_of(md):
    """去掉文档标题标签，只留正文：标题在不同接口下有时渲染为 <title>，有时渲染为 # 标题。"""
    return re.sub(r"<title>.*?</title>", "", md, flags=re.S)


def h1_titles(md):
    return [m.group(1).strip() for m in re.finditer(r"^#\s+(.+)$", body_of(md), re.M)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--doc", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    cfg = load_config()
    doc = args.doc or cfg.get("doc")
    if not doc:
        raise SystemExit("没有目标文档：传 --doc，或在 " + CONFIG + ' 里配置 {"doc": "..."}')

    expect = f"趋势复利（{args.date}）"

    md = fetch_markdown(doc)
    titles = h1_titles(md)
    if expect in titles:
        print(json.dumps({"ok": True, "skipped": True, "reason": f"已存在 {expect}"}, ensure_ascii=False))
        return 3

    xml = open(args.xml, encoding="utf-8").read()

    # 用 stdin 传内容：避免临时文件落在 lark-cli 允许目录之外
    cmd = [
        "lark-cli", "docs", "+update",
        "--doc", doc,
        "--command", "block_insert_after",
        "--block-id", "0",
        "--content", "-",
        "--as", "user",
    ]
    if args.dry_run:
        cmd.append("--dry-run")

    p = subprocess.run(cmd, capture_output=True, text=True, input=xml)
    if p.returncode != 0:
        raise SystemExit(f"写入失败：\n{p.stderr[-600:]}\n{p.stdout[-600:]}")
    try:
        res = json.loads(p.stdout)
    except json.JSONDecodeError:
        raise SystemExit("解析写入结果失败：\n" + p.stdout[:600])

    if not res.get("ok"):
        raise SystemExit("写入被拒绝：\n" + json.dumps(res, ensure_ascii=False)[:800])
    if res.get("data", {}).get("result") not in ("success", None):
        raise SystemExit("写入未完全成功：\n" + json.dumps(res, ensure_ascii=False)[:800])

    if args.dry_run:
        print(json.dumps({"ok": True, "dry_run": True}, ensure_ascii=False))
        return 0

    after = h1_titles(fetch_markdown(doc))
    if not after or after[0] != expect:
        raise SystemExit(f"校验失败：文档首个一级标题是 {after[:2]}，期望 {expect}")
    if len(after) != len(titles) + 1:
        raise SystemExit(f"校验失败：一级标题数量 {len(titles)} -> {len(after)}，期望 +1")

    print(json.dumps({
        "ok": True,
        "doc": doc,
        "inserted": expect,
        "h1_count": len(after),
        "order_head": after[:3],
        "warnings": res.get("data", {}).get("warnings", []),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
