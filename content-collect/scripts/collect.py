#!/usr/bin/env python3
"""统一入口：给一个主页/作品链接或关键词，采成 13 字段，写进指定的飞书表格。

这个脚本本身**不采集**，它只做三件事：认清输入属于哪个平台 → 调对应平台的引擎 →
把结果交给飞书写入器。两个引擎各自都是一套完整实现，各自可独立使用：

| 平台 | 引擎 | 为什么这么分工 |
|---|---|---|
| 抖音 | `scripts/douyin/collect.py`（自建 dtk 实例，REST + 签名） | 接口直连，最省；关键词搜索也只有它能做 |
| 小红书 | 本技能的 `xhs.mjs`（受控 Chrome，页面自己发请求） | 小红书接口签名在页面 JS 里，只能让浏览器代劳 |

产物是同一份 13 字段契约，所以落表那一半两个平台共用 `feishu_bitable.py`。

用法：
    python3 collect.py "<主页链接>" --to <base链接>            # 采 + 写
    python3 collect.py "操盘手" --to <base链接>                # 关键词（仅抖音）
    python3 collect.py "<链接>" --out rows.json                # 只采不写
    python3 collect.py "<链接>" --to <base链接> --dry-run      # 只报告会写什么
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent

#: 引擎位置：两个引擎现在都在本技能里（`scripts/douyin/` 与 `scripts/xhs.mjs`），
#: 环境变量只是给「想放到外面」留的口子。
DOUYIN_ENGINE = Path(os.environ.get(
    "CONTENT_COLLECT_DOUYIN", HERE / "douyin" / "collect.py",
)).expanduser()
XHS_ENGINE = Path(os.environ.get("CONTENT_COLLECT_XHS", HERE / "xhs.mjs")).expanduser()
WRITER = HERE / "feishu_bitable.py"

#: 表里「来源」列的选项名。写表时按这个值填。
SOURCE_LABEL = {"douyin": "抖音", "xiaohongshu": "小红书"}

#: 记住「上次用的那张表」。用户每次只用说一句「还用上次那张」，不用再翻链接。
#: 存在本技能自己的数据目录里，不跟任何平台的数据混在一起。
CONFIG_HOME = Path(os.environ.get("CONTENT_COLLECT_HOME", "~/.workbuddy/data/content-collect")).expanduser()
CONFIG_FILE = CONFIG_HOME / "config.json"

#: 退出码约定（调用方/模型据此决定下一步，不要改成 1）：
#:   3 —— 有一张「上次用过的表」，等用户确认是否继续用
#:   4 —— 还没有表，必须向用户要一条链接
EXIT_NEED_CONFIRM = 3
EXIT_NEED_TABLE = 4

_URL = re.compile(r"https?://[^\s\u4e00-\u9fff，。；！？、（）【】]+")
_SEC_UID = re.compile(r"^MS4wLjABAAAA[\w-]{10,}$")
_XHS_UID = re.compile(r"^[0-9a-f]{24}$")


class CollectError(RuntimeError):
    """原因已经说人话了，不需要堆栈。"""


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def save_last_table(url: str, name: str, base_token: str = "") -> None:
    """记下这次写的表。下次用户说「还用上次那张」时读它。"""
    cfg = load_config()
    cfg["last_table"] = {
        "name": name or "",
        "url": url,
        "base_token": base_token,
        "used_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    CONFIG_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def last_table() -> dict | None:
    entry = (load_config() or {}).get("last_table")
    return entry if isinstance(entry, dict) and entry.get("url") else None


def detect(text: str) -> tuple[str, str]:
    """判断 ``(platform, kind)``。

    ``kind`` 取 ``link`` / ``keyword``。平台只看域名与 ID 形状，不猜内容：
    猜错的代价是拿另一套引擎去跑，会把「链接无效」报成「服务不可用」。
    """
    raw = text.strip()
    if not raw:
        raise CollectError("输入为空")
    url = (_URL.search(raw) or [None])
    url = url.group(0) if url and hasattr(url, "group") else None

    target = url or raw
    if re.search(r"(^|\.)douyin\.com/", target) or target.startswith("v.douyin"):
        return "douyin", "link"
    if re.search(r"(^|\.)xiaohongshu\.com/", target) or "xhslink" in target:
        return "xiaohongshu", "link"
    if _SEC_UID.match(target):
        return "douyin", "link"
    if _XHS_UID.match(target):
        return "xiaohongshu", "link"
    # 没链接、又不是平台 ID —— 只可能是关键词。小红书的站内搜索对未登录会话
    # 直接返回空（实测），所以关键词这条路只有抖音能走。
    return "douyin", "keyword"


def run(cmd: list[str], label: str, quiet: bool) -> None:
    """跑一个引擎；失败就把它的错误原样带出来。

    引擎们自己会把「为什么失败」说清楚（还有修复提示），这里不要再包一层
    「采集失败」把原话盖掉 —— 那是最容易让人白折腾的一种体贴。
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise CollectError(f"{label}引擎失败：\n{err}")
    if not quiet and proc.stderr.strip():
        # 引擎的进度都打在 stderr，转发给用户看（带缩进，和本脚本的日志区分开）
        for line in proc.stderr.rstrip().splitlines():
            print(f"  {line}", file=sys.stderr)


def collect(platform: str, text: str, *, rows_path: Path, args, quiet: bool) -> dict:
    if platform == "douyin":
        if not DOUYIN_ENGINE.exists():
            raise CollectError(
                f"找不到抖音引擎：{DOUYIN_ENGINE}\n"
                "  它在 scripts/douyin/ 下；要是搬走了，用环境变量 CONTENT_COLLECT_DOUYIN 指过去。"
            )
        cmd = [sys.executable, str(DOUYIN_ENGINE), text, "--json", "--out", str(rows_path)]
        if args.count:
            cmd += ["--count", str(args.count)]
        run(cmd, "抖音", quiet)
    else:
        if not XHS_ENGINE.exists():
            raise CollectError(f"找不到小红书引擎：{XHS_ENGINE}")
        cmd = ["node", str(XHS_ENGINE), text, "--out", str(rows_path)]
        if args.count:
            cmd += ["--count", str(args.count)]
        if args.no_detail:
            cmd += ["--no-detail"]
        if args.with_token:
            cmd += ["--with-token"]
        run(cmd, "小红书", quiet)

    if not rows_path.exists():
        raise CollectError(f"引擎跑完了却没有产物：{rows_path}")
    return json.loads(rows_path.read_text(encoding="utf-8"))


def write_table(plan: dict, platform: str, *, target: str, extra: list[str],
                dry_run: bool, quiet: bool) -> dict:
    """把结果写进表。``target`` 是 ``/base/…`` 或 ``/drive/folder/…`` 链接，也可以是裸 token。"""
    flag = "--folder"
    if re.search(r"/base/[A-Za-z0-9]+", target) or (
        "://" not in target and not target.startswith("fld")
    ):
        # 裸 token 没有线索时按 base 处理：用户说「指定表格」时给了串 token，
        # 更可能是表格 token（文件夹那种一般会贴链接）。
        flag = "--base"
    if re.search(r"/drive/folder/", target) or target.startswith("fld"):
        flag = "--folder"

    tmp = Path(tempfile.mkdtemp(prefix="cc-write-", dir="/tmp")) / "rows.json"
    tmp.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

    cmd = [sys.executable, str(WRITER), "--rows", str(tmp), flag, target,
           "--set", f"来源={SOURCE_LABEL[platform]}", "--json", *extra]
    if dry_run:
        cmd.append("--dry-run")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise CollectError(f"写飞书失败：\n{(proc.stderr or proc.stdout).strip()}")
    if not quiet and proc.stderr.strip():
        for line in proc.stderr.rstrip().splitlines():
            print(f"  {line}", file=sys.stderr)
    try:
        result = json.loads(proc.stdout)
    except json.JSONDecodeError:
        result = {"created": 0, "updated": 0}
        print(proc.stdout.strip(), file=sys.stderr)
    result.update(ok=True, dry_run=dry_run)
    return result


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collect.py",
        description="根据主页/关键词/作品链接采集作品信息到指定飞书表格（抖音 / 小红书）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            '  python3 collect.py "https://www.douyin.com/user/MS4wLjABAAAA..." --to <base链接>\n'
            '  python3 collect.py "https://www.xiaohongshu.com/user/profile/<id>" --to <base链接>\n'
            '  python3 collect.py "操盘手" --to <base链接>          # 关键词只有抖音支持\n'
            '  python3 collect.py "<链接>" --out /tmp/rows.json      # 只采不写\n'
        ),
    )
    p.add_argument("input", help="主页链接、作品链接、关键词，或平台 ID")
    p.add_argument("--to", metavar="URL_OR_TOKEN",
                   help="目标表格：/base/<token> 链接、/drive/folder/<token> 链接或裸 token；"
                        "不给也没加 --reuse-last 时，会提示上次用过的表并退出")
    p.add_argument("--reuse-last", action="store_true",
                   help="沿用上次写过的那张表（用户说「还用上次那张」时加这个）")
    p.add_argument("--forget-last", action="store_true",
                   help="忘掉「上次用过的表」，然后退出")
    p.add_argument("--no-write", action="store_true",
                   help="只采集不落表（预览/排障用）；不会动「上次用过的表」")
    p.add_argument("--platform", choices=["auto", "douyin", "xiaohongshu"], default="auto",
                   help="强制指定平台（默认按输入自动判断）")
    p.add_argument("--count", type=int, help="最多采多少条")
    p.add_argument("--no-detail", action="store_true",
                   help="小红书：只拉列表不逐条开详情（快，但文案/标签/评论数会空）")
    p.add_argument("--with-token", action="store_true",
                   help="小红书：作品链接带上 xsec_token（能直接点开，但会过期）")
    p.add_argument("--set", action="append", default=[], metavar="列名=值",
                   help="额外常量列（来源会自动带上，不用手写）")
    p.add_argument("--out", help="把 13 字段结果另存一份到这个路径")
    p.add_argument("--dry-run", action="store_true", help="只采集并报告会写什么，不落表")
    p.add_argument("--json", action="store_true", help="输出 JSON 摘要")
    return p


def main() -> int:
    args = build_parser().parse_args()
    quiet = args.json

    if args.forget_last:
        cfg = load_config()
        cfg.pop("last_table", None)
        CONFIG_HOME.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("已忘掉「上次用过的表」。")
        return 0

    # 目标表的三种情形，先定下来再采集 —— 不要采完一整批才发现没地方写。
    saved = last_table()
    target = args.to or ""
    if args.no_write:
        target = ""
    elif not target and args.reuse_last:
        if not saved:
            print("还没有「上次用过的表」，请给一条飞书多维表格链接（--to <链接>）。", file=sys.stderr)
            return EXIT_NEED_TABLE
        target = saved["url"]
        print(f"沿用上次的表：{saved.get('name') or '（未命名）'}　{target}"
              f"（上次用于 {saved.get('used_at', '?')}）", file=sys.stderr)
    elif not target and saved:
        # 有旧表但没说要沿用、也没给新表 —— 停下来问，不要替用户决定。
        print("上次用过这张表：", file=sys.stderr)
        print(f"  名称：{saved.get('name') or '（未命名）'}", file=sys.stderr)
        print(f"  链接：{saved['url']}", file=sys.stderr)
        print(f"  上次写入：{saved.get('used_at', '?')}", file=sys.stderr)
        print("继续用它 → 加 --reuse-last；换一张 → 用 --to <新链接>。", file=sys.stderr)
        return EXIT_NEED_CONFIRM
    elif not target:
        print("还没有目标表格。请向用户要一条飞书多维表格链接，然后用 --to <链接> 重跑。",
              file=sys.stderr)
        return EXIT_NEED_TABLE

    try:
        platform, kind = detect(args.input)
        if args.platform != "auto":
            platform = args.platform
        if platform == "xiaohongshu" and kind == "keyword":
            raise CollectError(
                "小红书不支持按关键词采集（站内搜索对非登录会话直接返回空）。\n"
                "  两条路：① 换成作者的**主页链接**；② 关键词交给抖音：不加 --platform 即可。"
            )
        if not quiet:
            print(f"平台：{SOURCE_LABEL[platform]}　输入类型：{kind}", file=sys.stderr)

        scratch = Path(tempfile.mkdtemp(prefix="cc-collect-", dir="/tmp"))
        rows_path = scratch / "rows.json"
        plan = collect(platform, args.input, rows_path=rows_path, args=args, quiet=quiet)

        if args.out:
            Path(args.out).write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n",
                                      encoding="utf-8")
            if not quiet:
                print(f"已写入 {args.out}", file=sys.stderr)

        written = None
        if target:
            written = write_table(plan, platform, target=target, extra=args.set,
                                  dry_run=args.dry_run, quiet=quiet)
            if written and not args.dry_run:
                # 写成功了才记住它 —— 干跑/失败不污染「上次用过的表」。
                save_last_table(target, written.get("base_title", ""), written.get("base_token", ""))

        summary = {
            "platform": platform,
            "kind": kind,
            "account_name": plan.get("account_name"),
            "table_name": plan.get("table_name"),
            "count": plan.get("count"),
            "rows_path": str(rows_path) if not args.out else args.out,
            "table": written,
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            print(f"\n{SOURCE_LABEL[platform]}　{plan.get('account_name') or '(关键词)'}　"
                  f"{plan.get('count')} 条")
            if not written:
                print("（--no-write，只采集没落表）")
            else:
                title = written.get("base_title") or ""
                where = written.get("url") or target
                print(f"→ {title}{'（' + where + '）' if title else where}")
                if written.get("dry_run"):
                    print(f"干跑：会新增 {written.get('created')} 条、更新 {written.get('updated')} 条")
                else:
                    print(f"写入：新增 {written.get('created')} 条、更新 {written.get('updated')} 条")
                    print("已记住这张表，下次可以说「还用上次那张」")
        return 0
    except CollectError as exc:
        print(f"失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
