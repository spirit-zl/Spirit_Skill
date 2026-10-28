#!/usr/bin/env node
// feishu_copy.mjs — 把一篇飞书云文档 / 知识库文档复制到自己的云空间（含图片、附件）。
//
// 用法：
//   node feishu_copy.mjs --url <飞书文档链接> [--out <目录>] [--folder <目标文件夹|知识库节点>]
//                        [--title "..."] [--no-publish] [--json]
//
// 原理：docs +fetch 取 markdown（图片是 https://feishu.cn/file/<token>），
// 再用 docs +media-preview 按 token 取回原图，改成本地相对路径，交给 publish.mjs 落地。
// 之所以不用 docs +media-download：跨租户文档没有导出权限，预览接口可以。
//
// 2026-10-22 修：
//   1) 图片正则原为 /!\[([^\]]*)\]\(...\)/，alt 里带 \[ \] 转义（飞书自动描述很常见）时整条匹配失败、
//      图片保留下载。改为允许转义：/!\[(?:\\.|[^\]\\])*\]\(...\)/
//   2) 同一 token 出现多次时只下载一次（原来按出现次数重复下载）
//   3) 编号按「首次出现顺序」连续编号；替换改为一次性回调，绝不 split/join 链式改号
//      （链式替换会把 017→018 再 018→019，整篇串号）
//   4) 发布统一走 publish.mjs：/wiki/ 节点 → 建子文档，/drive/folder/ → 建文档，链接判定只有一处

import fs from 'fs';
import os from 'os';
import path from 'path';
import { fileURLToPath } from 'url';
import { spawnSync } from 'child_process';

const argv = process.argv.slice(2);
const flags = {};
const bools = new Set(['no-publish', 'json', 'help']);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a.startsWith('--')) {
    const k = a.slice(2);
    if (bools.has(k)) flags[k] = true;
    else if (i + 1 < argv.length) flags[k] = argv[++i];
  }
}

const HERE = path.dirname(fileURLToPath(import.meta.url));
const CONFIG = path.join(
  process.env.W2F_HOME || path.join(os.homedir(), '.workbuddy', 'data', 'yitang-to-feishu'),
  'config.json'
);

function die(m) { console.error('ERROR: ' + m); process.exit(1); }
const log = (...a) => { if (!flags.json) console.error(...a); };

function larkBin() {
  const r = spawnSync('which', ['lark-cli'], { encoding: 'utf8' });
  if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  const c = path.join(os.homedir(), '.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli');
  return fs.existsSync(c) ? c : 'lark-cli';
}

function lark(args, cwd) {
  const r = spawnSync(larkBin(), args, {
    cwd: cwd || process.cwd(), encoding: 'utf8', maxBuffer: 256 * 1024 * 1024,
  });
  const out = (r.stdout || '') + (r.stderr || '');
  let json = null;
  const s = out.indexOf('{');
  if (s >= 0) { try { json = JSON.parse(out.slice(s)); } catch { /* ignore */ } }
  if (r.status !== 0 && !json) throw new Error(out.trim().slice(0, 400));
  if (json && json.ok === false) throw new Error(JSON.stringify(json.error || json).slice(0, 400));
  return json || { raw: out };
}

const EXT = {
  'image/png': '.png', 'image/jpeg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp',
  'image/bmp': '.bmp', 'image/svg+xml': '.svg', 'application/pdf': '.pdf',
  'application/zip': '.zip', 'text/plain': '.txt',
};

function slug(s, n = 24) {
  return String(s || '').replace(/[\\/:*?"<>|\r\n\t]/g, ' ').trim()
    .replace(/\s+/g, '-').replace(/-+/g, '-').slice(0, n) || 'file';
}

// alt 允许转义括号；抓的是 URL 而不是 alt 内容
const IMG_RE = /!\[(?:\\.|[^\]\\])*\]\((https:\/\/feishu\.cn\/file\/([A-Za-z0-9]+))\)/g;

async function main() {
  if (flags.help || !flags.url) {
    console.log('node feishu_copy.mjs --url <飞书文档链接> [--out dir] [--folder <文件夹|知识库节点>]');
    return;
  }

  const outDir = path.resolve(flags.out || path.join(process.cwd(), 'feishu-copies',
    'copy-' + new Date().toISOString().replace(/[-:T]/g, '').slice(0, 13)));
  fs.mkdirSync(path.join(outDir, 'images'), { recursive: true });
  fs.mkdirSync(path.join(outDir, 'files'), { recursive: true });

  log('读取文档…');
  const fetched = lark(['docs', '+fetch', '--as', 'user', '--doc', flags.url,
    '--doc-format', 'markdown', '--format', 'json']);
  const doc = fetched && fetched.data && fetched.data.document;
  if (!doc || typeof doc.content !== 'string') die('读取失败：' + JSON.stringify(fetched).slice(0, 300));

  let md = doc.content;
  const titleMatch = /<title>([\s\S]*?)<\/title>/.exec(md);
  const title = flags.title || (titleMatch ? titleMatch[1].replace(/&amp;/g, '&').trim() : '未命名文档');
  md = md.replace(/<title>[\s\S]*?<\/title>\s*/, '');
  log(`标题：${title}；正文 ${md.length} 字`);

  // 1) 收集图片：按 token 去重，编号按首次出现顺序
  const imageOrder = [];
  const imageIndex = new Map(); // token -> 'images/NNN.ext'
  let m;
  while ((m = IMG_RE.exec(md)) !== null) {
    const token = m[2];
    if (!imageIndex.has(token)) {
      imageIndex.set(token, null);
      imageOrder.push(token);
    }
  }
  // 2) 附件 <source token="...">
  const sources = new Map();
  const sourceRe = /<source\s+([^>]*?)\/?>/g;
  while ((m = sourceRe.exec(md)) !== null) {
    const token = /token="([A-Za-z0-9]+)"/.exec(m[1]);
    const name = /name="([^"]*)"/.exec(m[1]);
    if (token && !sources.has(token[1])) sources.set(token[1], { name: name ? name[1] : '', raw: m[0] });
  }

  log(`图片 ${imageOrder.length} 张（去重后）、附件 ${sources.size} 个`);

  let done = 0;
  let failed = 0;
  let cursor = 0;

  const jobs = imageOrder.map((token, i) => ({ kind: 'image', token, idx: i }));
  for (const [token, info] of sources) jobs.push({ kind: 'file', token, name: info.name });

  async function worker() {
    while (true) {
      const i = cursor++;
      if (i >= jobs.length) return;
      const job = jobs[i];
      const sub = job.kind === 'image' ? 'images' : 'files';
      const base = job.kind === 'image'
        ? String(job.idx + 1).padStart(3, '0')
        : String(jobs.filter(j => j.kind === 'image').length + (i - imageOrder.length + 1)).padStart(3, '0');
      try {
        const r = lark(['docs', '+media-preview', '--as', 'user', '--token', job.token,
          '--output', path.join(outDir, sub, base + '.bin'), '--format', 'json']);
        const saved = r && r.data && r.data.saved_path;
        const ctype = r && r.data && r.data.content_type;
        if (!saved || !fs.existsSync(saved)) throw new Error('未落盘');
        const ext = EXT[ctype] || path.extname(job.name || '') || '.bin';
        const stem = job.kind === 'image' ? '' : '-' + slug((job.name || '').replace(/\.[^.]+$/, ''));
        const finalName = base + stem + ext;
        fs.renameSync(saved, path.join(outDir, sub, finalName));
        job.saved = sub + '/' + finalName;
        job.ctype = ctype;
        done++;
      } catch (e) {
        failed++;
        log(`  ${job.kind} ${job.token} 失败：${e.message.slice(0, 80)}`);
      }
      if (done && done % 10 === 0) log(`  已取回 ${done}/${jobs.length}`);
    }
  }
  await Promise.all(Array.from({ length: Math.min(5, Math.max(1, jobs.length)) }, () => worker()));
  log(`资源取回 ${done}/${jobs.length}，失败 ${failed}`);

  // 3) 一次性回调替换（不做链式 split/join）
  const imageLocal = new Map();
  for (const job of jobs) {
    if (job.kind === 'image' && job.saved) imageLocal.set(job.token, job.saved);
  }
  md = md.replace(IMG_RE, (raw, url, token) => {
    const local = imageLocal.get(token);
    if (!local) return raw;
    // 飞书图片的 alt 是自动生成的长描述，写进 markdown 会变成一大段 caption，直接丢掉
    return `![](@./${local})`;
  });
  md = md.replace(sourceRe, (raw, attrs) => {
    const t = /token="([A-Za-z0-9]+)"/.exec(attrs);
    if (!t) return raw;
    const job = jobs.find(j => j.kind === 'file' && j.token === t[1]);
    return job && job.saved ? `<source path="@./${job.saved}"/>` : raw;
  });

  const stamp = new Date();
  const local = `${stamp.getFullYear()}-${String(stamp.getMonth() + 1).padStart(2, '0')}-${String(stamp.getDate()).padStart(2, '0')} ${String(stamp.getHours()).padStart(2, '0')}:${String(stamp.getMinutes()).padStart(2, '0')}`;
  const header = `# ${title}\n\n> 来源：[飞书文档](${flags.url})\n> 复制时间：${local}\n\n`;
  const body = header + md.replace(/\n{3,}/g, '\n\n').trim() + '\n';

  fs.writeFileSync(path.join(outDir, 'content.md'), body.replace(/@\.\//g, ''), 'utf8');
  fs.writeFileSync(path.join(outDir, 'content.feishu.md'), body, 'utf8');
  fs.writeFileSync(path.join(outDir, 'meta.json'), JSON.stringify({
    title, source: flags.url, documentId: doc.document_id,
    imageCount: imageLocal.size,
    fileCount: jobs.filter(j => j.kind === 'file' && j.saved).length,
    imageTotal: imageOrder.length, failed, outDir,
  }, null, 2), 'utf8');

  if (flags['no-publish']) {
    console.log(outDir);
    return;
  }

  let folder = flags.folder;
  if (!folder) {
    try {
      const cfg = JSON.parse(fs.readFileSync(CONFIG, 'utf8'));
      folder = cfg.folderUrl || cfg.folder;
    } catch { /* none */ }
  }
  if (!folder) die('没有目标位置，用 --folder 指定');

  log('发布…');
  const pubArgs = [path.join(HERE, 'publish.mjs'), '--dir', outDir, '--folder', folder, '--json'];
  const r = spawnSync(process.execPath, pubArgs, { encoding: 'utf8', cwd: outDir, maxBuffer: 64 * 1024 * 1024 });
  const out = (r.stdout || '') + (r.stderr || '');
  const s = out.indexOf('{');
  let pub = null;
  if (s >= 0) { try { pub = JSON.parse(out.slice(s)); } catch { /* ignore */ } }
  if (!pub || !pub.ok) die('发布失败：' + out.trim().slice(0, 400));

  const result = {
    ok: true, docUrl: pub.docUrl, docId: pub.docId, title: pub.title,
    targetType: pub.targetType,
    images: imageLocal.size, failed, localDir: outDir,
  };
  if (flags.json) console.log(JSON.stringify(result));
  else {
    console.log('文档已创建：' + result.docUrl);
    console.log(`资源：${result.images} 张图，失败 ${failed} 个`);
    console.log('本地副本：' + outDir);
  }
}

main().catch((e) => { console.error('ERROR: ' + (e.stack || e)); process.exit(1); });
