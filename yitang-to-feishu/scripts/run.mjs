#!/usr/bin/env node
// run.mjs — 一条命令：抓网页 → 出 markdown + 图片 + 长截图 → 发布成飞书文档。
//
// 用法：
//   node run.mjs --url <URL>   [--folder <链接|token>] [--title "..."] [--width 1200] [--no-attach] [--no-shot]
//   node run.mjs --tab <targetId> [同上]
//
// 想看每一步的细节或只做其中一半，直接用 capture.mjs / publish.mjs。

import path from 'path';
import { spawnSync } from 'child_process';
import { fileURLToPath } from 'url';

const here = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);

function has(k) { return argv.includes('--' + k); }

const captureArgs = [path.join(here, 'capture.mjs'), ...argv, '--json'];
if (!has('out')) {
  // 默认落在当前目录的 webpage-captures/
}
const cap = spawnSync(process.execPath, captureArgs, { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
if (cap.status !== 0) {
  process.stderr.write(cap.stderr || '');
  process.exit(cap.status || 1);
}

let summary = null;
for (const line of (cap.stdout || '').trim().split('\n').reverse()) {
  try { summary = JSON.parse(line); break; } catch { /* keep looking */ }
}
if (!summary || !summary.outDir) {
  process.stderr.write(cap.stderr || '');
  console.error('ERROR: 抓取步骤没有返回输出目录');
  process.exit(1);
}

const pubArgs = [path.join(here, 'publish.mjs'), '--dir', summary.outDir];
if (has('folder')) pubArgs.push('--folder', argv[argv.indexOf('--folder') + 1]);
if (has('title')) pubArgs.push('--title', argv[argv.indexOf('--title') + 1]);
if (has('no-attach')) pubArgs.push('--no-attach');

const pub = spawnSync(process.execPath, pubArgs, { encoding: 'utf8', maxBuffer: 32 * 1024 * 1024 });
if (pub.stderr) process.stderr.write(pub.stderr);
process.stdout.write(pub.stdout || '');
process.exit(pub.status || 0);
