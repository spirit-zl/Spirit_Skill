#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把读书笔记 Markdown 上传为飞书云文档，存放到指定文件夹；或更新已有文档。

用法:
  python3 upload_to_feishu.py <note.md> --folder <链接|token> [--title <标题>] [--dry-run] [--json]
  python3 upload_to_feishu.py <note.md>                  # 用上次记录的默认文件夹
  python3 upload_to_feishu.py <note.md> --update <文档链接|token>   # 整篇覆盖更新已有文档
  python3 upload_to_feishu.py --show-folder              # 查看默认文件夹
  python3 upload_to_feishu.py --set-folder <链接|token>  # 更换默认文件夹（不上传）

说明:
  - 文件夹支持: 云空间目录链接（.../folder/xxx）、知识库链接（.../wiki/xxx）、裸 token；
    以及"我的空间"根目录（.../drive/my 或 my_library）。
  - 上传成功（或 --set-folder）后文件夹会记为默认，下次不带 --folder 时直接使用。
  - 固定以用户身份创建文档（--as user），文档出现在目标文件夹中。
  - --update 用整篇覆盖（overwrite）更新已有文档、保持文档链接不变；更新时自动去除
    正文首行的 H1 主标题（与飞书文档标题避免重复）。仅适用于纯文本笔记：会丢失文档内
    评论与图片等富内容，请谨慎使用。
  - --dry-run 只打印将要执行的请求，不真正创建/更新文档。

退出码: 0 = 成功；1 = 出错。
"""

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

CONFIG_PATH = Path.home() / '.workbuddy' / 'data' / 'reading-notes' / 'config.json'


def find_lark_cli():
    p = shutil.which('lark-cli')
    if p:
        return p
    fallback = Path.home() / '.workbuddy' / 'binaries' / 'node' / 'cli-connector-packages' / 'bin' / 'lark-cli'
    if fallback.is_file():
        return str(fallback)
    return None


def resolve_target(folder_input):
    """解析目标位置。返回 (kind, value)，kind 为 'token' 或 'position'。"""
    s = folder_input.strip()
    if s in ('my_library', 'my_library_root') or re.search(r'/drive/my(?:[/?#]|$)', s):
        return 'position', 'my_library'
    m = re.search(r'/folder/([A-Za-z0-9_-]+)', s)
    if m:
        return 'token', m.group(1)
    m = re.search(r'/wiki/([A-Za-z0-9_-]+)', s)
    if m:
        return 'token', m.group(1)
    if re.fullmatch(r'[A-Za-z0-9_-]{8,}', s):
        return 'token', s
    raise ValueError('无法解析文件夹: %s（支持目录链接 / 知识库链接 / token / .../drive/my）' % folder_input)


def load_config():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_default_folder(input_str, target):
    cfg = load_config()
    kind, value = target
    cfg['feishu_folder'] = {
        'input': input_str,
        'kind': kind,
        'value': value,
        'updated_at': datetime.now().astimezone().isoformat(timespec='seconds'),
    }
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')


def guess_title(text):
    for line in text.splitlines():
        m = re.match(r'^#(?!#)\s+(.+?)\s*$', line)
        if m:
            return m.group(1)
    return None


def require_cli():
    cli = find_lark_cli()
    if not cli:
        raise RuntimeError('找不到 lark-cli。请确认其已安装并在 PATH 中，或先完成 lark-cli 登录。')
    return cli


def run_lark(cmd, text):
    proc = subprocess.run(cmd, input=text.encode('utf-8'), capture_output=True)
    out = proc.stdout.decode('utf-8', 'replace').strip()
    err = proc.stderr.decode('utf-8', 'replace').strip()
    if proc.returncode != 0:
        raise RuntimeError('lark-cli 执行失败（退出码 %d）\n--- stdout ---\n%s\n--- stderr ---\n%s'
                           % (proc.returncode, out or '(空)', err or '(空)'))
    try:
        data = json.loads(out)
    except Exception:
        data = None
    return data, out, err


def do_upload(text, title, target, dry_run):
    cli = require_cli()
    kind, value = target
    cmd = [cli, 'docs', '+create',
           '--doc-format', 'markdown',
           '--content', '-',
           '--title', title,
           '--as', 'user']
    if kind == 'token':
        cmd += ['--parent-token', value]
    else:
        cmd += ['--parent-position', value]
    if dry_run:
        cmd.append('--dry-run')
    return run_lark(cmd, text)


def do_update(text, doc, dry_run):
    """整篇覆盖更新已有文档（overwrite），保持文档链接不变。"""
    cli = require_cli()
    cmd = [cli, 'docs', '+update',
           '--doc', doc,
           '--command', 'overwrite',
           '--doc-format', 'markdown',
           '--content', '-',
           '--as', 'user']
    if dry_run:
        cmd.append('--dry-run')
    return run_lark(cmd, text)


def strip_first_h1(text):
    """去掉正文首行的 H1 主标题（飞书文档标题已承载），连同其后的空行。"""
    lines = text.splitlines()
    if lines and re.match(r'^#(?!#)\s+\S', lines[0]):
        rest = lines[1:]
        while rest and not rest[0].strip():
            rest = rest[1:]
        return '\n'.join(rest).rstrip('\n') + '\n'
    return text


def fail(msg, as_json):
    if as_json:
        print(json.dumps({'ok': False, 'error': msg}, ensure_ascii=False, indent=2))
    else:
        print('错误: %s' % msg, file=sys.stderr)
    return 1


def main():
    ap = argparse.ArgumentParser(description='把读书笔记上传为飞书云文档（指定文件夹）')
    ap.add_argument('file', nargs='?', help='读书笔记 .md 文件')
    ap.add_argument('--folder', help='目标文件夹（链接或 token）；缺省用上次记录的默认')
    ap.add_argument('--title', help='文档标题；缺省取文件第一行的一级标题')
    ap.add_argument('--dry-run', action='store_true', help='只打印请求，不创建文档')
    ap.add_argument('--json', action='store_true', help='以 JSON 输出结果')
    ap.add_argument('--show-folder', action='store_true', help='查看默认文件夹')
    ap.add_argument('--set-folder', help='把该文件夹设为默认（不上传）')
    ap.add_argument('--update', dest='update_doc', metavar='DOC',
                    help='整篇覆盖更新已有飞书文档（文档链接或 token）')
    args = ap.parse_args()

    if args.show_folder:
        cfg = load_config().get('feishu_folder')
        if cfg:
            info = {'ok': True, 'folder': cfg.get('input'), 'kind': cfg.get('kind', 'token'),
                    'value': cfg.get('value'), 'updated_at': cfg.get('updated_at')}
            if args.json:
                print(json.dumps(info, ensure_ascii=False, indent=2))
            else:
                print('默认文件夹: %s' % cfg.get('input'))
                print('（%s = %s，更新于 %s）' % (cfg.get('kind', 'token'), cfg.get('value'),
                                              cfg.get('updated_at', '未知')))
        else:
            if args.json:
                print(json.dumps({'ok': True, 'folder': None}, ensure_ascii=False))
            else:
                print('未设置默认文件夹。使用 --folder 或 --set-folder 指定。')
        return 0

    if args.set_folder:
        try:
            target = resolve_target(args.set_folder)
        except ValueError as e:
            return fail(str(e), args.json)
        save_default_folder(args.set_folder, target)
        if args.json:
            print(json.dumps({'ok': True, 'folder': args.set_folder,
                              'kind': target[0], 'value': target[1]}, ensure_ascii=False, indent=2))
        else:
            print('已把默认文件夹设为: %s（%s = %s）' % (args.set_folder, target[0], target[1]))
        return 0

    if args.update_doc:
        if not args.file:
            ap.error('--update 需要提供笔记文件')
        path = Path(args.file)
        if not path.is_file():
            return fail('笔记文件不存在: %s' % path, args.json)
        text = path.read_text(encoding='utf-8')
        if not text.strip():
            return fail('笔记内容为空: %s' % path, args.json)
        body = strip_first_h1(text)
        if not args.json:
            print('目标文档: %s' % args.update_doc)
            print('正文字数: %d（已去除正文首行主标题，与飞书文档标题避免重复）' % len(body))
            print('正在整篇覆盖更新...' if not args.dry_run else 'dry-run 模式: 只打印请求，不更新文档')
        try:
            data, out, err = do_update(body, args.update_doc, args.dry_run)
        except RuntimeError as e:
            return fail(str(e), args.json)
        if args.dry_run:
            if args.json:
                print(json.dumps({'ok': True, 'dry_run': True, 'target': args.update_doc,
                                  'raw_output': out}, ensure_ascii=False, indent=2))
            else:
                print('--- lark-cli 输出 ---')
                print(out or '(空)')
                print('dry-run 完成，未更新文档。')
            return 0
        if data is None:
            if not args.json:
                print('--- lark-cli 原始输出 ---')
                print(out or '(空)')
                if err:
                    print('--- stderr ---')
                    print(err)
            return fail('无法解析 lark-cli 输出为 JSON，请检查上面的原始输出', args.json)
        if not data.get('ok'):
            return fail('更新失败: %s' % json.dumps(data, ensure_ascii=False)[:1000], args.json)
        d = data.get('data') or {}
        result = d.get('result')
        rev = (d.get('document') or {}).get('revision_id')
        warnings = d.get('warnings') or []
        if args.json:
            print(json.dumps({'ok': True, 'updated': args.update_doc, 'result': result,
                              'revision_id': rev, 'warnings': warnings},
                             ensure_ascii=False, indent=2))
        else:
            print('')
            print('[完成] 已更新飞书文档')
            print('  文档: %s' % args.update_doc)
            print('  result: %s    revision: %s' % (result, rev))
            if warnings:
                print('  服务端警告: %s' % json.dumps(warnings, ensure_ascii=False))
        return 0

    if not args.file:
        ap.error('需要提供笔记文件；或用 --show-folder / --set-folder')

    path = Path(args.file)
    if not path.is_file():
        return fail('笔记文件不存在: %s' % path, args.json)
    text = path.read_text(encoding='utf-8')
    if not text.strip():
        return fail('笔记内容为空: %s' % path, args.json)

    title = (args.title or '').strip() or guess_title(text)
    if not title:
        return fail('无法从文件解析标题（第一行应为 "# 标题"），请用 --title 指定', args.json)

    folder_input = (args.folder or '').strip()
    source = '本次指定'
    if not folder_input:
        cfg = load_config().get('feishu_folder')
        if not cfg:
            return fail('未提供 --folder，也没有已保存的默认文件夹。'
                        '按文件夹协议先向用户确认目标文件夹，再用 --folder 指定。', args.json)
        folder_input = cfg.get('input') or cfg.get('value')
        source = '默认（上次用过）'

    try:
        target = resolve_target(folder_input)
    except ValueError as e:
        return fail(str(e), args.json)

    if not args.json:
        print('目标文件夹（%s）: %s' % (source, folder_input))
        print('解析结果: %s = %s' % (target[0], target[1]))
        print('文档标题: %s' % title)
        print('正文字数: %d' % len(text))
        print('正在创建飞书文档...' if not args.dry_run else 'dry-run 模式: 只打印请求，不创建文档')

    try:
        data, out, err = do_upload(text, title, target, args.dry_run)
    except RuntimeError as e:
        return fail(str(e), args.json)

    if args.dry_run:
        if args.json:
            print(json.dumps({'ok': True, 'dry_run': True, 'folder': folder_input,
                              'kind': target[0], 'value': target[1], 'title': title,
                              'raw_output': out}, ensure_ascii=False, indent=2))
        else:
            print('--- lark-cli 输出 ---')
            print(out or '(空)')
            if err:
                print('--- stderr ---')
                print(err)
            print('dry-run 完成，未创建文档。')
        return 0

    if data is None:
        if not args.json:
            print('--- lark-cli 原始输出 ---')
            print(out or '(空)')
            if err:
                print('--- stderr ---')
                print(err)
        return fail('无法解析 lark-cli 输出为 JSON，请检查上面的原始输出', args.json)

    if not data.get('ok'):
        return fail('创建失败: %s' % json.dumps(data, ensure_ascii=False)[:1000], args.json)

    doc = (data.get('data') or {}).get('document') or {}
    url = doc.get('url') or ''
    document_id = doc.get('document_id') or ''
    warnings = (data.get('data') or {}).get('warnings') or []

    if args.folder:
        save_default_folder(folder_input, target)

    if args.json:
        print(json.dumps({'ok': True, 'url': url, 'document_id': document_id,
                          'title': title, 'folder': folder_input,
                          'kind': target[0], 'value': target[1],
                          'warnings': warnings}, ensure_ascii=False, indent=2))
    else:
        print('')
        print('[完成] 已创建飞书文档')
        print('  标题: %s' % title)
        print('  文件夹: %s' % folder_input)
        print('  链接: %s' % (url or '(未返回，请到目标文件夹查看)'))
        if args.folder:
            print('  已将本次文件夹记为默认（下次不带 --folder 时使用）')
        if warnings:
            print('  服务端警告: %s' % json.dumps(warnings, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
