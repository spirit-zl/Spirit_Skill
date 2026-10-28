#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""读书笔记组装与结构体检。

用法:
  python3 assemble_note.py build <parts_dir> -o <note.md> --title "<书名>" [--demote] [--json]
  python3 assemble_note.py check <note.md> [--json]

build : 把 <parts_dir> 下的 .md 文件按文件名顺序拼接，最前面写入 `# <title>` 主标题，
        生成完整读书笔记，并自动做结构体检、打印报告。
        若分节文件里混入 `#` 级标题，默认报错拦截；确认要整体降级时用 --demote
        （全部标题降一级：`#` → `##`，`##` → `###`，以此类推）。
check : 只体检，不写文件。

体检规则:
  1. 第一个非空行必须是主标题（`# ...`），且全篇只有这一个一级标题；
  2. 标题不得跳级（如 `##` 后直接出现 `####`，给警告）；
  3. 每个 `##` 节内必须有正文（只有标题没有内容的"空节"给警告）。

退出码: 0 = 没有错误；1 = 存在错误或无法执行。
"""

import argparse
import json
import re
import sys
from pathlib import Path

H_RE = re.compile(r'^(#{1,6})\s+(.+?)\s*$')
H1_RE = re.compile(r'^#(?!#)\s+\S', re.M)
H2_RE = re.compile(r'^##(?!#)\s+\S', re.M)


def scan(text):
    """扫描标题。返回 [{'line','level','text'}] 与全部行。"""
    lines = text.splitlines()
    titles = []
    for i, line in enumerate(lines, 1):
        m = H_RE.match(line)
        if m:
            titles.append({'line': i, 'level': len(m.group(1)), 'text': m.group(2)})
    return titles, lines


def inspect(text):
    """结构体检。返回 (errors, warnings, stats, outline)。"""
    errors, warnings = [], []
    titles, lines = scan(text)

    # 1. 主标题检查
    first_nonempty = next((l.strip() for l in lines if l.strip()), '')
    if not re.match(r'^#(?!#)\s+\S', first_nonempty):
        errors.append('第一行不是一级标题（# ...）：主标题缺失或不在开头')

    h1 = [t for t in titles if t['level'] == 1]
    if not h1:
        errors.append('全篇没有一级标题（# ...）')
    elif len(h1) > 1:
        where = ', '.join('第 %d 行' % t['line'] for t in h1)
        errors.append('出现 %d 个一级标题（应只有 1 个主标题）：%s' % (len(h1), where))

    # 2. 跳级检查
    prev = None
    for t in titles:
        if prev is not None and t['level'] - prev['level'] > 1:
            warnings.append('第 %d 行标题层级跳级：%s → %s（%s）' % (
                t['line'], '#' * prev['level'], '#' * t['level'], t['text'][:40]))
        prev = t

    # 3. 空节检查（每个 ## 的子树内应有正文）
    for idx, t in enumerate(titles):
        if t['level'] != 2:
            continue
        start = t['line']            # 标题所在行的下一行（0-based 切片起点）
        end = len(lines)
        for nxt in titles[idx + 1:]:
            if nxt['level'] <= 2:
                end = nxt['line'] - 1
                break
        body = lines[start:end]
        chars = sum(len(l.strip()) for l in body if l.strip() and not H_RE.match(l))
        if chars < 10:
            warnings.append('第 %d 行节「%s」内没有正文内容' % (t['line'], t['text'][:32]))

    stats = {}
    for t in titles:
        stats[t['level']] = stats.get(t['level'], 0) + 1

    outline = [{'level': t['level'], 'text': t['text']} for t in titles]
    return errors, warnings, stats, outline


def demote_headings(text):
    """全部标题降一级（跳过代码围栏内的行）。"""
    out, in_fence = [], False
    for line in text.splitlines():
        if line.strip().startswith('```'):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = H_RE.match(line)
            if m:
                level = min(len(m.group(1)) + 1, 6)
                line = '#' * level + ' ' + m.group(2)
        out.append(line)
    return '\n'.join(out)


def print_report(stats, outline, errors, warnings, limit_outline=200, file=None):
    print('== 读书笔记结构体检 ==', file=file)
    if stats:
        order = sorted(stats)
        print('标题统计: ' + '  '.join('H%d × %d' % (k, stats[k]) for k in order), file=file)
    if outline and len(outline) <= limit_outline:
        print('大纲:', file=file)
        for t in outline:
            print('  ' + '  ' * (t['level'] - 1) + '#' * t['level'] + ' ' + t['text'], file=file)
    elif outline:
        print('大纲: 共 %d 个标题，从略（超出 %d 条）' % (len(outline), limit_outline), file=file)
    for e in errors:
        print('  [错误] ' + e, file=file)
    for w in warnings:
        print('  [警告] ' + w, file=file)
    if errors:
        print('结论: 存在 %d 项错误、%d 项警告，需要处理' % (len(errors), len(warnings)), file=file)
    elif warnings:
        print('结论: 无错误，有 %d 项警告（请人工复核）' % len(warnings), file=file)
    else:
        print('结论: 通过，未发现问题', file=file)


def emit(payload, as_json):
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


def cmd_check(args):
    path = Path(args.file)
    if not path.is_file():
        print('文件不存在: %s' % path, file=sys.stderr)
        return 1
    text = path.read_text(encoding='utf-8')
    errors, warnings, stats, outline = inspect(text)
    out = sys.stderr if args.json else None
    print_report(stats, outline, errors, warnings, file=out)
    emit({'ok': not errors, 'file': str(path), 'errors': errors,
          'warnings': warnings, 'stats': stats, 'outline': outline}, args.json)
    return 1 if errors else 0


def cmd_build(args):
    parts_dir = Path(args.parts_dir)
    if not parts_dir.is_dir():
        print('分节目录不存在: %s' % parts_dir, file=sys.stderr)
        return 1
    files = sorted(p for p in parts_dir.glob('*.md') if p.is_file())
    out_path = Path(args.output) if args.output else parts_dir.parent / 'note.md'
    files = [p for p in files if p.resolve() != out_path.resolve()]
    if not files:
        print('分节目录里没有 .md 文件: %s' % parts_dir, file=sys.stderr)
        return 1

    part_warnings = []
    hashed = []
    chunks = []
    for p in files:
        text = p.read_text(encoding='utf-8').strip('\n')
        if not text:
            print('分节文件内容为空: %s' % p, file=sys.stderr)
            return 1
        if H1_RE.search(text):
            hashed.append(p.name)
        first = next((l.strip() for l in text.splitlines() if l.strip()), '')
        h2_count = len(H2_RE.findall(text))
        if h2_count == 0:
            part_warnings.append('%s 缺少 ## 级部分标题（如 "## 一、本书简介"）' % p.name)
        elif not H2_RE.match(first):
            part_warnings.append('%s 的 ## 级部分标题不在文件开头（第一个标题应为部分标题）' % p.name)
        elif h2_count > 1:
            part_warnings.append('%s 包含 %d 个 ## 级标题（部分标题只应有 1 个，部分内内容请用 ### 及以下）'
                                 % (p.name, h2_count))
        chunks.append(demote_headings(text) if args.demote else text)

    if hashed and not args.demote:
        print('检测到分节文件含 # 级标题: %s' % ', '.join(hashed), file=sys.stderr)
        print('请修正源文件，或确认后整体降级重组: 加 --demote', file=sys.stderr)
        return 1

    title = args.title.strip().lstrip('#').strip()
    body = '\n\n'.join(chunks)
    note = '# %s\n\n%s\n' % (title, body)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(note, encoding='utf-8')

    errors, warnings, stats, outline = inspect(note)
    warnings = part_warnings + warnings
    out = sys.stderr if args.json else None
    print('已组装 %d 个分节 → %s（%d 字）' % (len(files), out_path, len(note)), file=out)
    for p in files:
        print('  拼接: %s' % p.name, file=out)
    print('', file=out)
    print_report(stats, outline, errors, warnings, file=out)
    emit({'ok': not errors, 'output': str(out_path), 'parts': [p.name for p in files],
          'stats': stats, 'errors': errors, 'warnings': warnings}, args.json)
    return 1 if errors else 0


def main():
    ap = argparse.ArgumentParser(description='读书笔记组装与结构体检')
    sub = ap.add_subparsers(dest='command', required=True)

    b = sub.add_parser('build', help='拼接分节文件为完整笔记并体检')
    b.add_argument('parts_dir', help='分节文件目录（*.md，按文件名排序）')
    b.add_argument('-o', '--output', help='输出文件路径（默认 parts_dir 的上级目录 note.md）')
    b.add_argument('--title', required=True, help='主标题（书名本身），如 "反脆弱"，不加书名号与"读书笔记"')
    b.add_argument('--demote', action='store_true', help='全部标题降一级后再拼接')
    b.add_argument('--json', action='store_true', help='JSON 输出结果到 stdout（人类可读报告转 stderr）')
    b.set_defaults(func=cmd_build)

    c = sub.add_parser('check', help='体检已有笔记文件的结构')
    c.add_argument('file', help='读书笔记 .md 文件')
    c.add_argument('--json', action='store_true', help='JSON 输出结果到 stdout（人类可读报告转 stderr）')
    c.set_defaults(func=cmd_check)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
