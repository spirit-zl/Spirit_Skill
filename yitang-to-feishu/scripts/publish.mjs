#!/usr/bin/env node
// publish.mjs — 把抓取目录里的 markdown + 图片发布成飞书云文档。
//
// 用法：
//   node publish.mjs --dir <capture目录> [--folder <链接|token>] [--title "..."]
//                    [--as user] [--no-attach] [--doc <文档链接>] [--json] [--dry-run]
//
// 目标语义（2026-10-22 定）：
//   /drive/folder/<token>  → 在该文件夹里新建文档
//   /wiki/<node>           → 在该知识库节点下新建子文档（不覆盖节点正文）
//   /docx/<token>、--doc   → 原地覆盖该文档（链接不变）
//
// 依赖：lark-cli（已授权 user 身份）

import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';

const argv = process.argv.slice(2);
const flags = {};
const bools = new Set(['no-attach', 'json', 'help', 'dry-run']);

for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a.startsWith('--')) {
    const k = a.slice(2);
    if (bools.has(k)) flags[k] = true;
    else if (i + 1 < argv.length) flags[k] = argv[++i];
  }
}

const CONFIG_PATH = path.join(
  process.env.W2F_HOME || path.join(os.homedir(), '.workbuddy', 'data', 'yitang-to-feishu'),
  'config.json'
);

function die(msg) {
  console.error('ERROR: ' + msg);
  process.exit(1);
}

const log = (...a) => { if (!flags.json) console.error(...a); };

function loadConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8')); } catch { return {}; }
}

function saveConfig(cfg) {
  fs.mkdirSync(path.dirname(CONFIG_PATH), { recursive: true });
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(cfg, null, 2) + '\n');
}

// 解析目标：区分「文件夹」「知识库节点」「文档」三类，别把 wiki token 当文件夹用
export function parseTarget(input) {
  const s = String(input || '').trim();
  if (!s) return null;
  let m;
  if ((m = /\/wiki\/space\/[^/?#]+\/node\/([A-Za-z0-9]+)/.exec(s))) return { type: 'wiki', token: m[1], raw: s };
  if ((m = /\/wiki\/([A-Za-z0-9]+)/.exec(s))) return { type: 'wiki', token: m[1], raw: s };
  if ((m = /\/drive\/folder\/([A-Za-z0-9]+)/.exec(s))) return { type: 'folder', token: m[1], raw: s };
  if ((m = /\/(docx|docs)\/([A-Za-z0-9]+)/.exec(s))) return { type: 'doc', token: m[2], raw: s };
  if (/^[A-Za-z0-9]{16,}$/.test(s)) return { type: 'folder', token: s, raw: s };
  return { type: 'folder', token: s, raw: s };
}

function larkBin() {
  const r = spawnSync('which', ['lark-cli'], { encoding: 'utf8' });
  if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
  const cand = path.join(os.homedir(), '.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli');
  if (fs.existsSync(cand)) return cand;
  return 'lark-cli';
}

function runLark(args, cwd) {
  const bin = larkBin();
  const r = spawnSync(bin, args, { cwd, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024, env: process.env });
  if (r.error) throw new Error(r.error.message);
  const out = (r.stdout || '') + (r.stderr || '');
  let json = null;
  const start = out.indexOf('{');
  if (start >= 0) {
    try { json = JSON.parse(out.slice(start)); } catch { /* not json */ }
  }
  if (r.status !== 0 && !json) throw new Error(out.trim() || ('lark-cli 退出码 ' + r.status));
  if (json && json.ok === false) throw new Error(JSON.stringify(json.error || json));
  return json || { raw: out };
}

// 建文档时 title 由 --title 交给飞书，正文里再留一个同名 H1 会重复，去掉
export function stripTitleHeading(md, title) {
  if (!title) return md;
  const esc = String(title).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return md.replace(new RegExp('^#\\s+' + esc + '\\s*(\\r?\\n)+'), '');
}

// 建完子文档后，尽量把 wiki 节点链接（而不是裸 docx 链接）返回给用户
function resolveWikiNodeUrl(parentToken, documentId, as) {
  try {
    const parent = runLark(['wiki', '+node-get', '--as', as, '--node-token', parentToken, '--format', 'json']);
    const spaceId = parent && parent.data && parent.data.space_id;
    if (!spaceId) return '';
    const list = runLark(['wiki', '+node-list', '--as', as, '--space-id', spaceId,
      '--parent-node-token', parentToken, '--format', 'json']);
    const nodes = (list && list.data && list.data.nodes) || [];
    const hit = nodes.find((n) => n.obj_token === documentId);
    return hit ? 'https://my.feishu.cn/wiki/' + hit.node_token : '';
  } catch {
    return '';
  }
}

function main() {
  if (flags.help) {
    console.log('node publish.mjs --dir <capture目录> [--folder <文件夹|知识库节点链接>] [--title ...] [--doc <文档链接>] [--no-attach] [--dry-run]');
    return;
  }

  const dir = path.resolve(flags.dir || '.');
  if (!fs.existsSync(dir)) die('目录不存在：' + dir);
  const mdPath = path.join(dir, 'content.feishu.md');
  if (!fs.existsSync(mdPath)) die('缺少 content.feishu.md，先跑 capture.mjs');

  let meta = {};
  try { meta = JSON.parse(fs.readFileSync(path.join(dir, 'meta.json'), 'utf8')); } catch { /* optional */ }

  const cfg = loadConfig();
  const targetInput = flags.doc || flags.folder || cfg.folderUrl || cfg.folder || '';
  const target = flags.doc
    ? { type: 'doc', token: (parseTarget(flags.doc) || {}).token || flags.doc, raw: flags.doc }
    : parseTarget(targetInput);
  if (!target || !target.token) {
    die('没有目标位置。用 --folder <飞书文件夹链接 | 知识库节点链接>，或先写入配置：' + CONFIG_PATH);
  }
  if (flags.folder) {
    const parsed = parseTarget(flags.folder);
    cfg.folder = parsed.type === 'folder' ? parsed.token : cfg.folder;
    cfg.folderUrl = String(flags.folder).includes('http') ? String(flags.folder) : cfg.folderUrl;
    cfg.lastTarget = { type: parsed.type, token: parsed.token, url: String(flags.folder) };
    saveConfig(cfg);
  }

  const title = flags.title || meta.title || path.basename(dir);
  const as = flags.as || 'user';
  const shotName = meta.screenshot && meta.screenshot.file
    ? meta.screenshot.file
    : (fs.existsSync(path.join(dir, 'fullpage.png')) ? 'fullpage.png'
      : (fs.existsSync(path.join(dir, 'fullpage.jpg')) ? 'fullpage.jpg' : ''));
  const shot = shotName ? path.join(dir, shotName) : '';
  const attach = !flags['no-attach'] && shot && fs.existsSync(shot);

  // 附件说明放在正文末尾，保证附件卡片在标题之下
  let content = fs.readFileSync(mdPath, 'utf8');
  if (attach && !content.includes('## 整页截图')) {
    content = content.replace(/\s*$/, '') + '\n\n---\n\n## 整页截图\n\n见文末附件（' +
      (meta.screenshot && meta.screenshot.height ? meta.screenshot.height + 'px 长图' : 'PNG 长图') + '）。\n';
    fs.writeFileSync(mdPath, content, 'utf8');
  }

  const mode = target.type === 'doc' ? 'overwrite' : 'create';
  const where = target.type === 'wiki' ? '知识库节点下新建子文档'
    : target.type === 'doc' ? '原地覆盖文档'
      : '文件夹内新建文档';
  log(`目标：${target.raw || target.token}（${where}）`);

  if (flags['dry-run']) {
    const out = { ok: true, dryRun: true, mode, target, title, dir, attach };
    console.log(flags.json ? JSON.stringify(out) : JSON.stringify(out, null, 2));
    return;
  }

  let docId;
  let docUrl;

  if (mode === 'overwrite') {
    const docRef = target.raw && target.raw.startsWith('http') ? target.raw : target.token;
    runLark(['docs', '+update', '--as', as, '--doc', docRef, '--command', 'overwrite',
      '--doc-format', 'markdown', '--content', '@./content.feishu.md', '--format', 'json'], dir);
    docId = target.token;
    docUrl = target.raw && target.raw.startsWith('http') ? target.raw : ('https://my.feishu.cn/docx/' + docId);
  } else {
    // 新建时去掉与标题重复的首个 H1，避免「标题 + 同名一级标题」两行一样
    const body = stripTitleHeading(content, title);
    if (body !== content) fs.writeFileSync(mdPath, body, 'utf8');

    const created = runLark([
      'docs', '+create', '--as', as,
      '--doc-format', 'markdown',
      '--content', '@./content.feishu.md',
      '--title', title,
      '--parent-token', target.token,
      '--format', 'json',
    ], dir);
    const doc = created && created.data && created.data.document;
    if (!doc || !doc.document_id) die('创建文档失败：' + JSON.stringify(created).slice(0, 400));
    docId = doc.document_id;
    docUrl = (target.type === 'wiki' && resolveWikiNodeUrl(target.token, docId, as))
      || doc.url || ('https://my.feishu.cn/docx/' + docId);
  }

  let attached = false;
  let attachError = '';
  if (attach) {
    try {
      runLark(['docs', '+media-insert', '--as', as, '--doc', docId,
        '--file', './' + shotName, '--type', 'file', '--format', 'json'], dir);
      attached = true;
    } catch (e) {
      attachError = e.message;
    }
  }

  const imgCount = Array.isArray(meta.images)
    ? meta.images.filter(i => i.file).length
    : (Number(meta.imageCount) || 0);

  const result = {
    ok: true,
    mode,
    targetType: target.type,
    target: target.token,
    docId,
    docUrl,
    title,
    images: imgCount,
    attached,
    attachError: attachError || undefined,
    localDir: dir,
  };

  if (flags.json) {
    console.log(JSON.stringify(result));
  } else {
    console.log('文档已创建：' + docUrl);
    console.log('标题：' + title + '（' + where + '）');
    console.log('正文图片：' + result.images + ' 张' + (attached ? '，整页截图已作为附件追加' : ''));
    if (attachError) console.log('附件追加失败：' + attachError);
    console.log('本地副本：' + dir);
  }
}

main();
