#!/usr/bin/env python3
"""把采集到的作者主页 + 作品列表，渲染成一份结构化的 Markdown 报告。

输入是 dtk.py 的 --json 原始输出（不做任何改写，只做聚合与排版）：

    python3 dtk.py --json parse  "<主页链接>"    > profile.json
    python3 dtk.py --json posts  "<主页链接>" -c 30 --cursor ... > items.json   # 可翻页合并
    python3 report.py --profile profile.json --items items.json --out 报告.md

设计约定（与用户既有规范一致）：
  - 不臆造：接口没返回的字段留「—」，无法确认的写「待确认」。
  - 计数类一律标注为「采集时刻快照」。
  - 作品表按发布时间倒序，附网页链接。
"""
from __future__ import annotations

import argparse
import collections
import datetime
import json
import pathlib
import sys
import zoneinfo

TZ = zoneinfo.ZoneInfo("Asia/Shanghai")


def to_local(iso: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ)


def fmt_num(v: object) -> str:
    return f"{v:,}" if isinstance(v, int) else "—"


def fmt_dur(ms: object) -> str:
    if not ms:
        return "—"
    s = int(ms) // 1000
    return f"{s // 60}:{s % 60:02d}"


def clean(text: object) -> str:
    """压成单行并转义表格分隔符，避免破坏 Markdown 表格。"""
    return " ".join(str(text or "").split()).replace("|", "／")


def load_items(paths: list[str]) -> list[dict]:
    merged, seen = [], set()
    for p in paths:
        data = json.loads(pathlib.Path(p).read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        for it in items or []:
            cid = it.get("content_id") or it.get("aweme_id")
            if cid and cid not in seen:
                seen.add(cid)
                merged.append(it)
    return merged


def build(profile: dict, items: list[dict], collected_at: str) -> str:
    items = sorted(items, key=lambda x: x.get("created_at") or "", reverse=True)
    author = (items[0].get("author") if items else {}) or {}
    name = profile.get("nickname") or author.get("nickname") or "未知账号"
    sec = profile.get("sec_uid") or author.get("uid") or author.get("sec_uid") or "待确认"
    home = profile.get("web_url") or (
        f"https://www.douyin.com/user/{sec}" if sec != "待确认" else "—")
    pstats = profile.get("stats") or {}

    tot = collections.Counter()
    for it in items:
        for k in ("digg_count", "comment_count", "share_count", "collect_count"):
            tot[k] += (it.get("stats") or {}).get(k) or 0
    n = len(items) or 1
    dates = [it["created_at"] for it in items if it.get("created_at")]
    by_month = collections.Counter(to_local(d).strftime("%Y-%m") for d in dates)
    tags = collections.Counter(t for it in items for t in (it.get("tags") or []))
    durs = sorted((it.get("duration_ms") or 0) for it in items)
    kinds = collections.Counter(it.get("kind") or "未知" for it in items)

    L: list[str] = []
    L.append(f"# 抖音账号内容采集报告：{name}\n")
    L.append(f"- **采集时间**：{collected_at}（Asia/Shanghai）")
    L.append(f"- **数据来源**：自建 dtk 实例（Douyin_TikTok_Download_API）· 抖音 Web 接口")
    L.append(f"- **账号主页**：[{name}]({home})")
    L.append(f"- **sec_user_id**：`{sec}`")
    uid = profile.get("unique_id") or author.get("unique_id")
    L.append(f"- **抖音号**：{uid or '待确认'}　**认证**："
             f"{'已认证' if profile.get('verified') else '未认证' if profile.get('verified') is False else '待确认'}")
    L.append(f"- **作品总数**：采集到 **{len(items)} 个**"
             + (f"（主页口径 {pstats['content_count']} 个）" if pstats.get("content_count") else ""))
    if dates:
        L.append(f"- **时间跨度**：{to_local(min(dates)).strftime('%Y-%m-%d')} ~ "
                 f"{to_local(max(dates)).strftime('%Y-%m-%d')}")
    if len(kinds) > 1 or (kinds and "video" not in kinds):
        L.append("- **内容类型**：" + "、".join(f"{k} {v} 条" for k, v in kinds.most_common()))
    L.append("")

    L.append("## 一、账号概览\n")
    L.append("| 指标 | 数值 |")
    L.append("|---|---|")
    if pstats.get("follower_count") is not None:
        L.append(f"| 粉丝数 | {pstats['follower_count']:,} |")
    if pstats.get("following_count") is not None:
        L.append(f"| 关注数 | {pstats['following_count']:,} |")
    if pstats.get("total_digg") is not None:
        L.append(f"| 获赞总数（主页口径） | {pstats['total_digg']:,} |")
    L.append(f"| 采集作品数 | {len(items)} |")
    L.append(f"| 作品累计点赞 | {tot['digg_count']:,} |")
    L.append(f"| 作品累计评论 | {tot['comment_count']:,} |")
    L.append(f"| 作品累计收藏 | {tot['collect_count']:,} |")
    L.append(f"| 作品累计分享 | {tot['share_count']:,} |")
    L.append(f"| 单条平均点赞 | {round(tot['digg_count'] / n):,} |")
    if durs:
        L.append(f"| 视频时长（中位/最短/最长） | {fmt_dur(durs[len(durs) // 2])} / "
                 f"{fmt_dur(durs[0])} / {fmt_dur(durs[-1])} |")
    L.append("")
    sig = (profile.get("signature") or author.get("signature") or "").strip()
    if sig:
        L.append("**账号简介（原文）**：\n")
        L.extend(f"> {ln}" if ln.strip() else ">" for ln in sig.splitlines())
        L.append("")

    if tags:
        L.append("## 二、内容特征\n")
        L.append("### 2.1 高频标签\n")
        L.append("| 标签 | 出现次数 |")
        L.append("|---|---|")
        L.extend(f"| #{t} | {c} |" for t, c in tags.most_common(15))
        L.append("")
        L.append("### 2.2 发布节奏（按月）\n")
        L.append("| 月份 | 发布数 | 分布 |")
        L.append("|---|---|---|")
        L.extend(f"| {m} | {by_month[m]} | {'█' * by_month[m]} |" for m in sorted(by_month))
        L.append("")
        top = sorted(items, key=lambda x: -((x.get("stats") or {}).get("digg_count") or 0))[:10]
        L.append("### 2.3 点赞 Top 10\n")
        L.append("| 排名 | 发布日 | 标题 | 点赞 | 评论 | 收藏 | 分享 | 链接 |")
        L.append("|---|---|---|---|---|---|---|---|")
        for i, it in enumerate(top, 1):
            s = it.get("stats") or {}
            day = to_local(it["created_at"]).strftime("%Y-%m-%d") if it.get("created_at") else "—"
            L.append(f"| {i} | {day} | {clean(it.get('title') or it.get('desc'))} | "
                     f"{fmt_num(s.get('digg_count'))} | {fmt_num(s.get('comment_count'))} | "
                     f"{fmt_num(s.get('collect_count'))} | {fmt_num(s.get('share_count'))} | "
                     f"[打开]({it.get('web_url') or '—'}) |")
        L.append("")

    L.append("## 三、全量作品清单\n")
    L.append(f"共 {len(items)} 条，按发布时间倒序。点赞/评论/收藏/分享为采集时刻快照。\n")
    L.append("| # | 发布日 | 标题 | 点赞 | 评论 | 收藏 | 分享 | 时长 | 链接 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for i, it in enumerate(items, 1):
        s = it.get("stats") or {}
        day = to_local(it["created_at"]).strftime("%Y-%m-%d") if it.get("created_at") else "—"
        L.append(f"| {i} | {day} | {clean(it.get('title') or it.get('desc'))} | "
                 f"{fmt_num(s.get('digg_count'))} | {fmt_num(s.get('comment_count'))} | "
                 f"{fmt_num(s.get('collect_count'))} | {fmt_num(s.get('share_count'))} | "
                 f"{fmt_dur(it.get('duration_ms'))} | [打开]({it.get('web_url') or '—'}) |")
    L.append("")
    L.append("---\n")
    L.append("## 附：口径说明\n")
    L.append("- 点赞/评论/收藏/分享为**采集时刻的快照**，平台侧会持续变动。")
    L.append("- 时长为 分:秒，源自接口 `duration_ms`。")
    L.append("- 接口未返回的字段留「—」，不臆造数值；无法确认的信息标「待确认」。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="渲染抖音采集 Markdown 报告")
    ap.add_argument("--profile", required=True, help="parse 的 --json 输出文件")
    ap.add_argument("--items", required=True, nargs="+", help="posts 的 --json 输出文件（可多个，自动去重）")
    ap.add_argument("--out", required=True, help="输出的 .md 路径")
    ap.add_argument("--at", help="采集时间文本（默认取当前时间）")
    a = ap.parse_args()

    profile = json.loads(pathlib.Path(a.profile).read_text(encoding="utf-8"))
    profile = profile.get("data", profile) if isinstance(profile, dict) else profile
    items = load_items(a.items)
    if not items:
        print("没有可渲染的条目", file=sys.stderr)
        return 1
    at = a.at or datetime.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    out = pathlib.Path(a.out)
    out.write_text(build(profile, items, at), encoding="utf-8")
    print(f"已写 {out}（{len(items)} 条作品，{out.stat().st_size:,} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
