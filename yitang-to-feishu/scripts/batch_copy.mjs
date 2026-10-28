#!/usr/bin/env node
// batch_copy.mjs — 一次把多篇飞书文档复制到同一个目标位置（文件夹或知识库节点）。
//
// 用法：
//   node batch_copy.mjs --folder <文件夹|wiki节点> --url <链接1> --url <链接2> ...
//   node batch_copy.mjs --folder <...> --urls-file list.txt
//
// 每篇都会：
//   1. drive +inspect 取真实标题（有些文档正文里没有 <title>，不取就只能叫「未命名文档」）
//   2. feishu_copy.mjs 抓正文 + 图片 + 附件，并按目标类型发布（文件夹→新文档，wiki→子文档）
//   3. 结果写进 <out-root>/result.json，最后打印汇总表
//
// 单篇失败不会中断整批；退出码 0 表示全部成功，1 表示有失败。

import fs from 'fs';
import path from 'path';
import os from 'os';
import { spawnSync } from 'child_process';

const HERE = path.dirname(new URL(import.meta.url).pathname);
const argv = process.argv.slice(2);
const flags = { url: [] };
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a === '--url') flags.url.push(argv[++i]);
  else if (a === '--urls-file') flags['urls-file'] = argv[++i];
  else if (a === '--folder') flags.folder = argv[++i];
  else if (a === '--out-root') flags['out-root'] = argv[++i];
  else if (a === '--dry-run') flags['dry-run'] = true;
  else if (a === '--help' || a === '-h') flags.help = true;
}

if (flags.help || (!flags.url.length && !flags['urls-file']) || !flags.folder) {
  console.log('node batch_copy.mjs --folder <文件夹|wiki节点> --url <链接> [--url <链接> ...] [--urls-file list.txt] [--out-root 目录] [--dry-run]');
  process.exit(0);
}

const urls = [...flags.url];
if (flags['urls-file']) {
  const txt = fs.readFileSync(flags['urls-file'], 'utf8');
  for (const line of txt.split('\n')) {
    const u = line.trim();
    if (/^https?:\/\//.test(u)) urls.push(u);
  }
}

const outRoot = path.resolve(flags['out-root'] || path.join(process.cwd(), 'webpage-captures',
  'feishu-batch-' + new Date().toISOString().slice(0, 10).replace(/-/g, '')));
fs.mkdirSync(outRoot, { recursive: true });
console.log(`共 ${urls.length} 篇 → ${flags.folder}`);
console.log(`工作目录：${outRoot}\n`);

const parseJson = (s) => {
  const i = s.indexOf('{');
  if (i < 0) return null;
  try { return JSON.parse(s.slice(i)); } catch { return null; }
};

const inspectTitle = (url) => {
  try {
    const r = spawnSync('lark-cli', ['drive', '+inspect', '--url', url, '--as', 'user', '--format', 'json'],
      { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
    const j = parseJson((r.stdout || '') + (r.stderr || ''));
    return (j && j.data && j.data.title) || '';
  } catch { return ''; }
};

const results = [];
for (const [n, url] of urls.entries()) {
  const tag = `[${n + 1}/${urls.length}]`;
  const title = inspectTitle(url);
  console.log(`${tag} ${title || url}`);
  if (flags['dry-run']) {
    results.push({ n: n + 1, url, title, ok: null, dryRun: true });
    continue;
  }
  const outDir = path.join(outRoot, String(n + 1).padStart(2, '0'));
  const args = [path.join(HERE, 'feishu_copy.mjs'), '--url', url, '--out', outDir,
    '--folder', flags.folder, '--json'];
  if (title) args.push('--title', title);
  const t0 = Date.now();
  const r = spawnSync(process.execPath, args, { encoding: 'utf8', maxBuffer: 128 * 1024 * 1024, cwd: outRoot });
  const out = (r.stdout || '') + (r.stderr || '');
  const parsed = parseJson(out);
  const ok = !!(parsed && parsed.ok);
  const sec = Math.round((Date.now() - t0) / 1000);
  results.push({
    n: n + 1, url, title: title || (parsed && parsed.title) || '',
    ok, docUrl: (parsed && parsed.docUrl) || '', images: (parsed && parsed.images) || 0,
    failed: (parsed && parsed.failed) || 0, sec,
    error: ok ? '' : out.trim().slice(-300),
  });
  console.log(ok
    ? `${tag} OK  ${parsed.docUrl} · 图 ${parsed.images} · ${sec}s`
    : `${tag} FAIL ${out.trim().slice(-200)}`);
  fs.writeFileSync(path.join(outRoot, 'result.json'), JSON.stringify(results, null, 2), 'utf8');
}

if (!flags['dry-run']) fs.writeFileSync(path.join(outRoot, 'result.json'), JSON.stringify(results, null, 2), 'utf8');

const okCount = results.filter(r => r.ok).length;
console.log(flags['dry-run']
  ? `\n预检 ${results.length} 篇（--dry-run：只列标题，没有真跑）`
  : `\n完成 ${okCount}/${results.length}`);
const failedList = results.filter(r => r.ok === false);
if (failedList.length) {
  console.log('失败：');
  for (const f of failedList) console.log(`  #${f.n} ${f.title || f.url}\n    ${f.error}`);
}
console.log('明细：' + path.join(outRoot, 'result.json'));
process.exit(failedList.length ? 1 : 0);
