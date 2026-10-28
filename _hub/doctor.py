#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doctor.py —— 仓库体检。回答"现在是不是健康的"。

检查项
  1. 每个技能目录是否有 SKILL.md，frontmatter 是否可解析、是否有 name/description
  2. name 是否与目录名一致（不一致会导致部分平台按 name 寻址失败）
  3. description 是否过长（平台注入时有全局预算，过长会被截断成几十字符，触发词全丢）
  4. 各平台目录形态（必须是真实目录）、链接是否有效、是否有死链
  5. 仓库里是否有不该存在的杂物（.DS_Store、zip、临时文件）

用法
  python3 _hub/doctor.py            # 人看
  python3 _hub/doctor.py --json     # 机器读
退出码：0 全部通过（可能有警告） / 1 有错误
"""

import argparse
import json
import os
import sys

HUB_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HUB_DIR)
CONFIG = os.path.join(HUB_DIR, "platforms.json")

# description 超过这个长度就提醒（平台全局预算通常只给每个技能几十字符）
DESC_WARN = 200
DESC_HARD = 500


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return json.load(f)


def parse_frontmatter(path):
    with open(path, encoding="utf-8") as f:
        text = f.read()
    if not text.startswith("---"):
        return None, "文件开头不是 --- 分隔符"
    end = text.find("\n---", 3)
    if end == -1:
        return None, "缺少结束的 --- 分隔符"

    block = text[3:end]
    data, key = {}, None
    for raw in block.splitlines():
        if not raw.strip():
            continue
        if raw[0] not in (" ", "\t") and ":" in raw:
            key, _, val = raw.partition(":")
            key, val = key.strip(), val.strip()
            if val in ("|", ">", "|-", ">-", "|+", ">+"):
                data[key] = ""
            elif val:
                data[key] = val.strip("'\"")
            else:
                data[key] = ""
        elif key and (raw.startswith(" ") or raw.startswith("\t")):
            data[key] = (data.get(key, "") + " " + raw.strip()).strip()
    return data, None


def scan_skills():
    out = []
    for name in sorted(os.listdir(REPO)):
        if name.startswith(".") or name.startswith("_"):
            continue
        p = os.path.join(REPO, name)
        if os.path.isdir(p):
            out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--fix", action="store_true", help="清理杂物文件（.DS_Store 等）")
    args = ap.parse_args()

    errors, warnings, infos = [], [], []
    skills = scan_skills()

    # ---- 逐技能检查 ----
    skill_rows = []
    for name in skills:
        p = os.path.join(REPO, name)
        row = {"name": name, "ok": True, "issues": []}

        skm = os.path.join(p, "SKILL.md")
        if not os.path.isfile(skm):
            row["ok"] = False
            row["issues"].append("缺少 SKILL.md")
            errors.append(f"{name}: 缺少 SKILL.md")
            skill_rows.append(row)
            continue

        fm, err = parse_frontmatter(skm)
        if err:
            row["ok"] = False
            row["issues"].append(f"frontmatter 解析失败：{err}")
            errors.append(f"{name}: frontmatter {err}")
            skill_rows.append(row)
            continue

        fname = (fm.get("name") or "").strip()
        desc = (fm.get("description") or "").strip()

        if not fname:
            row["ok"] = False
            row["issues"].append("frontmatter 缺 name")
            errors.append(f"{name}: frontmatter 缺 name")
        elif fname != name:
            row["issues"].append(f"name「{fname}」与目录名不一致")
            warnings.append(f"{name}: name 字段为「{fname}」，与目录名不同")

        if not desc:
            row["ok"] = False
            row["issues"].append("frontmatter 缺 description")
            errors.append(f"{name}: frontmatter 缺 description")
        else:
            n = len(desc)
            row["descLen"] = n
            if n > DESC_HARD:
                row["issues"].append(f"description {n} 字符，过长（平台注入时可能被截断）")
                warnings.append(f"{name}: description {n} 字符，建议压到 {DESC_WARN} 以内")
            elif n > DESC_WARN:
                row["issues"].append(f"description {n} 字符，偏长")
                warnings.append(f"{name}: description {n} 字符，偏长")

        row["desc"] = " ".join(desc.split())[:80]
        skill_rows.append(row)

    # ---- 平台接入检查 ----
    cfg = load_config()
    plat_rows = []
    for p in cfg["platforms"]:
        raw = p.get("skillsDir")
        pr = {"id": p["id"], "name": p["name"], "dir": raw, "state": "", "linked": 0, "dead": []}
        if not raw:
            pr["state"] = "无原生目录（index-only）"
            plat_rows.append(pr)
            continue

        d = os.path.expanduser(raw)
        if os.path.islink(d):
            pr["state"] = "错误：目录本身是软链接"
            errors.append(f"平台 {p['id']}: {raw} 是软链接，必须改成真实目录")
        elif not os.path.isdir(d):
            pr["state"] = "未接入"
        else:
            pr["state"] = "正常"
            for e in sorted(os.listdir(d)):
                fp = os.path.join(d, e)
                if not os.path.islink(fp):
                    continue
                if not os.path.exists(fp):
                    pr["dead"].append(e)
                elif os.path.isfile(os.path.join(fp, "SKILL.md")):
                    pr["linked"] += 1
            if pr["dead"]:
                errors.append(f"平台 {p['id']}: 死链 {', '.join(pr['dead'])}")
            missing = set(skills) - set(
                e for e in os.listdir(d) if os.path.islink(os.path.join(d, e))
            )
            if missing:
                warnings.append(
                    f"平台 {p['id']}: 缺 {len(missing)} 个技能链接（运行 sync.py 补齐）"
                )
        plat_rows.append(pr)

    # ---- 杂物检查 ----
    junk = []
    for dp, dn, fn in os.walk(REPO):
        if os.path.basename(dp) == ".git":
            dn[:] = [x for x in dn if x != ".git"]
            continue
        dn[:] = [x for x in dn if x != ".git"]
        for f in fn:
            if f == ".DS_Store" or f.endswith((".tmp", ".swp", "~")):
                junk.append(os.path.relpath(os.path.join(dp, f), REPO))
    if junk:
        if args.fix:
            removed = 0
            for rel in junk:
                try:
                    os.remove(os.path.join(REPO, rel))
                    removed += 1
                except OSError:
                    pass
            infos.append(f"已清理 {removed} 个杂物文件")
            junk = []
        else:
            warnings.append(f"仓库里有 {len(junk)} 个杂物文件（.DS_Store 等，可用 --fix 清理）")

    # ---- 输出 ----
    if args.json:
        print(json.dumps(
            {
                "repo": REPO,
                "skillCount": len(skills),
                "skills": skill_rows,
                "platforms": plat_rows,
                "errors": errors,
                "warnings": warnings,
                "junk": junk,
            },
            ensure_ascii=False,
            indent=2,
        ))
    else:
        print(f"仓库: {REPO}")
        print(f"技能: {len(skills)} 个\n")

        for r in skill_rows:
            mark = "✓" if r["ok"] and not r["issues"] else ("✗" if not r["ok"] else "!")
            print(f"  {mark} {r['name']:<28} {r.get('desc', '')}")
            for i in r["issues"]:
                print(f"      · {i}")

        print("\n平台接入:")
        for pr in plat_rows:
            print(f"  {pr['name']:<32} {pr['state']}  已接入 {pr['linked']} 个")

        if errors:
            print(f"\n错误 {len(errors)}:")
            for e in errors:
                print(f"  ✗ {e}")
        if warnings:
            print(f"\n警告 {len(warnings)}:")
            for w in warnings:
                print(f"  · {w}")
        if infos:
            print(f"\n已处理:")
            for i in infos:
                print(f"  ✓ {i}")
        if not errors and not warnings:
            print("\n全部检查通过。")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
