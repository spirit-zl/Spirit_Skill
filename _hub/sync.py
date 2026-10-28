#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync.py —— 把 Spirit_Skill 仓库里的技能分发到各平台技能目录。

设计原则
  1. 仓库是唯一真身；平台目录里只放"逐技能软链接"，不复制文件。
  2. 平台目录本身必须保持真实目录。如果发现它被做成了软链接，直接报错退出
     （历史上把 ~/.workbuddy/skills 整体换成软链接，导致平台清理时删掉真实文件）。
  3. 只增不删。要删多余链接必须显式加 --prune，且只删"指向本仓库的"软链接。
  4. 幂等：重复运行结果一致。

用法
  python3 _hub/sync.py                 # 同步所有 enabled 平台
  python3 _hub/sync.py --all           # 连 enabled=false 的平台也处理（会创建目录）
  python3 _hub/sync.py --platform workbuddy
  python3 _hub/sync.py --dry-run       # 只看要做什么
  python3 _hub/sync.py --prune         # 额外清理仓库中已不存在技能的残留链接
"""

import argparse
import json
import os
import sys
import time

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HUB_DIR)
CONFIG = os.path.join(HUB_DIR, "platforms.json")

# 仓库内置目录，不作为技能分发（下划线前缀约定）
RESERVED_PREFIX = "_"


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def scan_skills():
    """仓库根目录下所有含 SKILL.md 的目录，按名字排序。"""
    skills = []
    for name in sorted(os.listdir(REPO)):
        if name.startswith(".") or name.startswith(RESERVED_PREFIX):
            continue
        path = os.path.join(REPO, name)
        if os.path.isdir(path) and os.path.isfile(os.path.join(path, "SKILL.md")):
            skills.append(name)
    return skills


def read_frontmatter(skill_path):
    """极简 frontmatter 解析，只取 name / description，不依赖 PyYAML。"""
    fm_path = os.path.join(skill_path, "SKILL.md")
    try:
        with open(fm_path, encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return {}

    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]

    data = {}
    key = None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw[0] not in (" ", "\t") and ":" in raw:
            key, _, val = raw.partition(":")
            key = key.strip()
            val = val.strip()
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                data[key] = ""
            elif val:
                data[key] = val.strip("'\"")
            else:
                data[key] = ""
        elif key and (raw.startswith(" ") or raw.startswith("\t")):
            piece = raw.strip()
            data[key] = (data.get(key, "") + " " + piece).strip()
    return data


def ensure_platform_dir(platform, dry_run=False):
    """确保平台技能目录存在，且是真实目录。返回 (ok, message)。"""
    raw = platform.get("skillsDir")
    if not raw:
        return False, "未配置 skillsDir"

    path = os.path.expanduser(raw)

    if os.path.islink(path):
        return False, (
            f"拒绝处理：{path} 是软链接（指向 {os.readlink(path)}）。\n"
            f"    平台目录必须保持真实目录，否则平台清理时会删到真实文件。"
        )

    if os.path.isdir(path):
        return True, "已存在"

    if dry_run:
        return True, "将创建"

    try:
        os.makedirs(path, exist_ok=True)
        return True, "已创建"
    except OSError as e:
        return False, f"创建失败：{e}"


def sync_platform(platform, skills, dry_run=False, prune=False):
    ok, msg = ensure_platform_dir(platform, dry_run)
    print(f"\n[{platform['id']}] {platform['name']}")
    print(f"  目录: {platform['skillsDir']}  ({msg})")
    if not ok:
        return {"linked": 0, "kept": 0, "conflict": [], "dead": [], "error": msg}

    target_dir = os.path.expanduser(platform["skillsDir"])
    if dry_run and not os.path.isdir(target_dir):
        print("  (目录尚不存在，以下为计划)")
        for s in skills:
            print(f"    + 链接 {s}")
        return {"linked": len(skills), "kept": 0, "conflict": [], "dead": [], "error": None}

    linked, kept, conflict = 0, 0, []

    for name in skills:
        link = os.path.join(target_dir, name)
        want = os.path.join(REPO, name)

        if os.path.islink(link):
            cur = os.readlink(link)
            if os.path.realpath(cur) == os.path.realpath(want):
                kept += 1
            else:
                conflict.append((name, f"链接指向别处：{cur}"))
        elif os.path.exists(link):
            conflict.append((name, "平台目录里是真实目录（不是链接）"))
        else:
            if not dry_run:
                os.symlink(want, link)
            linked += 1

    # 残留/死链检查
    dead = []
    repo_real = os.path.realpath(REPO)
    for entry in sorted(os.listdir(target_dir)):
        fp = os.path.join(target_dir, entry)
        if not os.path.islink(fp):
            continue
        tgt = os.readlink(fp)
        if not os.path.exists(fp):
            dead.append((entry, tgt, "死链（目标不存在）"))
        elif entry not in skills and os.path.realpath(tgt).startswith(repo_real + os.sep):
            dead.append((entry, tgt, "仓库中已无此技能"))

    pruned = 0
    if prune and dead:
        for entry, tgt, reason in dead:
            if reason.startswith("仓库中已无此技能"):
                if not dry_run:
                    os.remove(os.path.join(target_dir, entry))
                pruned += 1

    verb = "将" if dry_run else "已"
    print(f"  {verb}新建链接 {linked}，已存在 {kept}，冲突 {len(conflict)}")
    for name, why in conflict:
        print(f"    ! {name}: {why}")
    if dead:
        for entry, tgt, reason in dead:
            mark = "已清理" if (prune and reason.startswith("仓库中已无")) else "警告"
            print(f"    · [{mark}] {entry}: {reason}")
    if pruned:
        print(f"  {verb}清理残留链接 {pruned} 个")

    return {"linked": linked, "kept": kept, "conflict": conflict, "dead": dead, "error": None}


def write_index(skills):
    """生成平台无关的技能总览 INDEX.md。任何 agent 只要会读文件就能用。"""
    lines = [
        "# 技能总览（INDEX）",
        "",
        "> 本文件由 `_hub/sync.py` 自动生成，请勿手工编辑。",
        f"> 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}　技能数：{len(skills)}",
        "",
        "任何平台只要读取本文件与下列绝对路径，就能使用这些技能，无需平台原生支持。",
        "",
        "| 技能 | 一句话说明 | 路径 |",
        "|---|---|---|",
    ]

    for name in skills:
        path = os.path.join(REPO, name)
        fm = read_frontmatter(path)
        desc = (fm.get("description") or "").strip()
        desc = " ".join(desc.split())
        if len(desc) > 90:
            desc = desc[:90] + "…"
        lines.append(f"| `{name}` | {desc} | `{path}/SKILL.md` |")

    lines += [
        "",
        "## 怎么用（平台无关）",
        "",
        "1. 读本文件，按上表挑技能。",
        "2. 打开对应的 `SKILL.md`，按其正文执行。",
        "3. 技能若带 `scripts/`、`references/`，路径都相对于该技能目录。",
        "",
        "## 各平台接入状态",
        "",
    ]

    cfg = load_config()
    for p in cfg["platforms"]:
        if not p.get("skillsDir"):
            lines.append(f"- **{p['name']}**：{p.get('note', '无原生技能目录')}")
            continue
        d = os.path.expanduser(p["skillsDir"])
        if os.path.isdir(d) and not os.path.islink(d):
            n = sum(
                1
                for e in os.listdir(d)
                if os.path.islink(os.path.join(d, e))
                and os.path.exists(os.path.join(d, e))
                and os.path.isfile(os.path.join(d, e, "SKILL.md"))
            )
            lines.append(f"- **{p['name']}**：`{p['skillsDir']}`（已接入 {n} 个）")
        else:
            lines.append(f"- **{p['name']}**：未接入（`{p['skillsDir']}` 不存在）")

    out = os.path.join(REPO, "INDEX.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return out


def main():
    ap = argparse.ArgumentParser(description="把技能分发到各平台")
    ap.add_argument("--all", action="store_true", help="包含 enabled=false 的平台")
    ap.add_argument("--platform", action="append", default=[], help="只处理指定平台 id，可多次")
    ap.add_argument("--dry-run", action="store_true", help="只显示计划，不改动")
    ap.add_argument("--prune", action="store_true", help="清理仓库中已不存在技能的残留链接")
    ap.add_argument("--no-index", action="store_true", help="不刷新 INDEX.md")
    args = ap.parse_args()

    cfg = load_config()
    skills = scan_skills()

    if not skills:
        print("仓库里没有找到任何含 SKILL.md 的技能目录。")
        return 1

    print(f"仓库：{REPO}")
    print(f"发现技能 {len(skills)} 个：{', '.join(skills)}")

    targets = []
    for p in cfg["platforms"]:
        if args.platform:
            if p["id"] in args.platform:
                targets.append(p)
        elif p.get("enabled") or args.all:
            targets.append(p)

    if not targets:
        print("没有要处理的平台（用 --all 或 --platform 指定）。")
        return 1

    total_conflict = 0
    for p in targets:
        if p.get("mode") == "index-only":
            print(f"\n[{p['id']}] {p['name']}")
            print(f"  跳过：{p.get('note', '无原生技能目录，仅通过 INDEX.md 使用')}")
            continue
        res = sync_platform(p, skills, dry_run=args.dry_run, prune=args.prune)
        total_conflict += len(res["conflict"])

    if not args.no_index and not args.dry_run:
        out = write_index(skills)
        print(f"\n已刷新索引：{out}")

    if total_conflict:
        print(f"\n注意：有 {total_conflict} 处冲突未自动处理，请按上面提示人工确认。")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
