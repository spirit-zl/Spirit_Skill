#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
collect.py —— 把各平台目录里"新建/修改过的真实技能目录"收纳回 Spirit_Skill。

为什么需要它
  有些平台只能在它自己的目录里创建技能（例如 WorkBuddy 的技能创建流程会写到
  ~/.workbuddy/skills/）。这些是真实目录而非软链接，仓库里看不到。
  本脚本把它们搬回仓库，并在原位补一条软链接，使仓库重新成为唯一真身。

判定规则
  · 只处理"真实目录 + 含 SKILL.md"的条目；软链接一律跳过（已经是仓库的一部分）。
  · 仓库里没有同名技能 → 直接搬入，原位建链接。
  · 仓库里已有同名技能 → 视为冲突，不覆盖，列出两边差异供人工决定；
                     加 --overwrite 才用平台的版本覆盖仓库版本（原版先备份）。

用法
  python3 _hub/collect.py                 # 只看不搬（dry-run）
  python3 _hub/collect.py --apply         # 真正执行搬入
  python3 _hub/collect.py --apply --overwrite   # 同名时用平台版本覆盖（会先备份）
"""

import argparse
import json
import os
import shutil
import sys
import time

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HUB_DIR)
CONFIG = os.path.join(HUB_DIR, "platforms.json")
BACKUP_DIR = os.path.join(HUB_DIR, "backups")


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def dir_stats(path):
    """返回 (文件数, 总字节, SKILL.md 修改时间)。"""
    files = total = 0
    skm = 0.0
    for dp, _dn, fn in os.walk(path):
        for f in fn:
            fp = os.path.join(dp, f)
            if os.path.isfile(fp) and not os.path.islink(fp):
                files += 1
                try:
                    total += os.path.getsize(fp)
                except OSError:
                    pass
    p = os.path.join(path, "SKILL.md")
    if os.path.isfile(p):
        skm = os.path.getmtime(p)
    return files, total, skm


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "—"


def collect(platform, apply_changes=False, overwrite=False, dry_run=True):
    raw = platform.get("skillsDir")
    if not raw:
        return

    pdir = os.path.expanduser(raw)
    print(f"\n[{platform['id']}] {pdir}")
    if not os.path.isdir(pdir) or os.path.islink(pdir):
        print("  跳过：目录不存在或本身是软链接")
        return

    found_new, found_conflict = [], []

    for name in sorted(os.listdir(pdir)):
        if name.startswith(".") or name.startswith("_"):
            continue
        fp = os.path.join(pdir, name)
        if os.path.islink(fp):
            continue                                   # 已是仓库映射
        if not os.path.isdir(fp):
            continue
        if not os.path.isfile(os.path.join(fp, "SKILL.md")):
            continue                                   # 不是技能

        repo_path = os.path.join(REPO, name)
        if os.path.exists(repo_path):
            found_conflict.append(name)
        else:
            found_new.append(name)

    if not found_new and not found_conflict:
        print("  没有需要收纳的技能")
        return

    if found_new:
        print(f"  可收纳（仓库里没有）: {', '.join(found_new)}")
        for name in found_new:
            src = os.path.join(pdir, name)
            dst = os.path.join(REPO, name)
            nf, sz, skm = dir_stats(src)
            print(f"    + {name}  ({nf} 文件, {sz} 字节, SKILL.md 改于 {fmt_time(skm)})")
            if dry_run:
                continue
            shutil.move(src, dst)
            os.symlink(dst, src)
            print(f"      已搬入仓库，原位已建链接")

    if found_conflict:
        print(f"  冲突（两边都有）: {', '.join(found_conflict)}")
        for name in found_conflict:
            src = os.path.join(pdir, name)
            dst = os.path.join(REPO, name)
            rf, rs, rm = dir_stats(src)
            df, ds, dm = dir_stats(dst)
            newer = "平台" if rm > dm else "仓库"
            print(f"    ! {name}")
            print(f"        平台: {rf} 文件 / {rs} 字节 / SKILL.md {fmt_time(rm)}")
            print(f"        仓库: {df} 文件 / {ds} 字节 / SKILL.md {fmt_time(dm)}")
            print(f"        较新：{newer}")
            if overwrite and not dry_run:
                os.makedirs(BACKUP_DIR, exist_ok=True)
                bak = os.path.join(
                    BACKUP_DIR,
                    f"repo-{name}-{time.strftime('%Y%m%d-%H%M%S')}",
                )
                shutil.move(dst, bak)
                shutil.move(src, dst)
                os.symlink(dst, src)
                print(f"        已用平台版本覆盖仓库版（原版备份至 {bak}）")
            elif overwrite and dry_run:
                print(f"        将用平台版本覆盖（原版会先备份）")
            else:
                print(f"        未处理；确认要覆盖请加 --overwrite")


def main():
    ap = argparse.ArgumentParser(description="把平台目录里的真实技能收纳回仓库")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认只预览）")
    ap.add_argument("--overwrite", action="store_true", help="同名时用平台版本覆盖仓库版本")
    ap.add_argument("--platform", action="append", default=[], help="只处理指定平台 id")
    args = ap.parse_args()

    dry_run = not args.apply
    cfg = load_config()

    if dry_run:
        print("== 预览模式（不改动任何文件）。确认无误后加 --apply 执行。==")

    for p in cfg["platforms"]:
        if args.platform and p["id"] not in args.platform:
            continue
        if not p.get("skillsDir"):
            continue
        collect(p, apply_changes=args.apply, overwrite=args.overwrite, dry_run=dry_run)

    if args.apply:
        print("\n收纳完成。建议接着运行：python3 _hub/sync.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
