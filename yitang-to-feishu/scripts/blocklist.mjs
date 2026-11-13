#!/usr/bin/env node
// blocklist.mjs — 抓「块级虚拟列表」文档（一堂/飞书式渲染），正文按 block index 逐屏收割。
//
// 与 capture.mjs 的分工：capture.mjs 默认按「页面可见文本 + 整页滚动」提取，
// 对 .virtual-list 里 roll 走的块无能为力（一堂 fs-doc 实测只能拿到开头 41 块）。
// 本脚本按块的身份（.block[index]）去重，分多次 evaluate 收割，最后离屏重组再提取。
//
// 用法：
//   node blocklist.mjs --url <页面地址> --out <目录> [--folder <飞书wiki节点|文件夹>] [--no-publish]
//   node blocklist.mjs --tab <targetId> --out <目录> ...
//
// 前置：Chrome 开着「Allow remote debugging for this browser instance」，且该页已登录。

import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';

const argv = process.argv.slice(2);
const flags = {};
const bools = new Set(['no-publish', 'help']);
for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a.startsWith('--')) {
    const k = a.slice(2);
    if (bools.has(k)) flags[k] = true;
    else if (i + 1 < argv.length) flags[k] = argv[++i];
  }
}
const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const EXTRACT = fs.readFileSync(path.join(SCRIPT_DIR, 'extract-in-page.js'), 'utf8');
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(...a);

if (flags.help || (!flags.url && !flags.tab)) {
  console.log('node blocklist.mjs --url <页面地址>|--tab <targetId> --out <目录> [--folder <wiki节点>] [--no-publish] [--title ...]');
  process.exit(0);
}

// ─── CDP ────────────────────────────────────────────────────────────────────

// 统一走 ensure-chrome：优先专用调试 Chrome（可反复连），其次用户 Chrome 的勾选式端点
const ens = spawnSync(process.execPath, [path.join(SCRIPT_DIR, 'ensure-chrome.mjs'), '--json'], { encoding: 'utf8' });
if (ens.status !== 0) {
  console.error('ERROR: ' + (ens.stderr || '拿不到 Chrome 调试端点').trim());
  process.exit(1);
}
const { ws: WS_URL, mode: CHROME_MODE } = JSON.parse(ens.stdout.trim());
log('Chrome 模式：' + CHROME_MODE);

class CDP {
  constructor(u) { this.u = u; this.id = 0; this.pending = new Map(); }
  connect() {
    return new Promise((res, rej) => {
      const ws = new WebSocket(this.u); this.ws = ws;
      const t = setTimeout(() => rej(new Error('握手超时：调试端点可能卡死，去 chrome://inspect 取消勾选再勾上')), 12000);
      ws.onopen = () => { clearTimeout(t); res(); };
      ws.onerror = () => { clearTimeout(t); rej(new Error('CDP 连接失败')); };
      ws.onmessage = (e) => {
        let m; try { m = JSON.parse(e.data); } catch { return; }
        if (m.id && this.pending.has(m.id)) {
          const p = this.pending.get(m.id); this.pending.delete(m.id); clearTimeout(p.t);
          if (m.error) p.rej(new Error(m.error.message)); else p.res(m.result || {});
        }
      };
      ws.onclose = () => { for (const [, p] of this.pending) p.rej(new Error('CDP 已关闭')); this.pending.clear(); };
    });
  }
  send(method, params = {}, sessionId = null, timeout = 180000) {
    return new Promise((res, rej) => {
      const id = ++this.id; const payload = { id, method, params };
      if (sessionId) payload.sessionId = sessionId;
      const t = setTimeout(() => { this.pending.delete(id); rej(new Error('CDP 超时 ' + method)); }, timeout);
      this.pending.set(id, { res, rej, t });
      this.ws.send(JSON.stringify(payload));
    });
  }
}

const cdp = new CDP(WS_URL);
await cdp.connect();

let targetId = flags.tab;
if (!targetId) {
  const { targetInfos } = await cdp.send('Target.getTargets');
  const tok = (flags.url.match(/fs-doc\/[^/]+\/([A-Za-z0-9]+)/) || [])[1] || '';
  const hit = targetInfos.find(t => t.type === 'page' && tok && t.url.includes(tok));
  targetId = hit ? hit.targetId : (await cdp.send('Target.createTarget', { url: flags.url })).targetId;
  log(hit ? `复用标签页 ${targetId}` : `新开标签页 ${targetId}`);
}

const { sessionId: sid } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
await cdp.send('Page.enable', {}, sid);
// 有些站点会拒绝 HeadlessChrome UA，统一伪装成普通 Chrome
await cdp.send('Emulation.setUserAgentOverride', {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
  acceptLanguage: 'zh-CN,zh;q=0.9',
}, sid);
// 复用的标签页可能停在上一次的登录页，强制重新导航一次
await cdp.send('Page.navigate', { url: flags.url }, sid, 30000).catch(() => {});
async function ev(expr, timeout = 180000) {
  const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, sid, timeout);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
}

// ─── 等页面就绪 ─────────────────────────────────────────────────────────────

let triedSync = false;
for (let i = 0; i < 40; i++) {
  await sleep(500);
  const st = await ev(`({ href: location.href, ready: document.readyState, hasList: !!document.querySelector('.virtual-list') })`, 8000).catch(() => null);
  if (!st) continue;
  if (/sso\.|\.top\/login/.test(st.href) && !/fs-doc/.test(st.href)) {
    if (!triedSync) {
      triedSync = true;
      log('落到登录页，尝试从用户 Chrome 同步登录态…');
      const r = spawnSync(process.execPath, [path.join(SCRIPT_DIR, 'sync-login.mjs')], { encoding: 'utf8' });
      if (r.status === 0) {
        log((r.stdout || '').trim());
        await cdp.send('Page.navigate', { url: flags.url }, sid);
        continue;
      }
      log('同步失败：' + ((r.stderr || '').trim() || '未知原因'));
      console.error('ERROR: 需要登录。运行 login.mjs 拿二维码扫码：');
      console.error(`  node ${path.join(SCRIPT_DIR, 'login.mjs')} --url "${flags.url}"`);
      process.exit(2);
    }
    process.exit(2);
  }
  if (st.ready === 'complete' && st.hasList) break;
}

const init = await ev(`(() => {
  window.__YT__ = null;
  const list = document.querySelector('.virtual-list');
  if (!list) return { err: '没有 .virtual-list，这类页面请用 capture.mjs' };
  const group = list.querySelector('[role=group]') || list.firstElementChild;
  const vh = window.innerHeight || 900;
  window.__YT__ = { map: {}, y: 0, list, group, step: Math.max(300, Math.round(vh * 0.7)),
    total: document.documentElement.scrollHeight };
  return { title: document.title, step: window.__YT__.step, total: window.__YT__.total, kids: group.children.length };
})()`);
if (init.err) { console.error('ERROR: ' + init.err); process.exit(1); }
log('init', JSON.stringify(init));

// ─── 逐批收割 ───────────────────────────────────────────────────────────────

const batch = `(async () => {
  const S = window.__YT__;
  const list = document.querySelector('.virtual-list');
  const group = list.querySelector('[role=group]') || list.firstElementChild;
  const t0 = Date.now();
  function grab() {
    for (const el of group.children) {
      if (el.getAttribute('role') !== 'listitem') continue;
      const inner = el.querySelector('.block');
      let idx = inner ? parseInt(inner.getAttribute('index'), 10) : NaN;
      if (!Number.isFinite(idx)) {
        const m = /\\bitem-(\\d+)\\b/.exec(el.className || '');
        idx = m ? parseInt(m[1], 10) : NaN;
      }
      if (!Number.isFinite(idx) || S.map[idx]) continue;
      for (const img of el.querySelectorAll('img')) {
        const lazy = img.getAttribute('data-src') || img.getAttribute('data-original') || '';
        let src = img.currentSrc || img.src || '';
        if ((!src || src.startsWith('data:')) && lazy) src = lazy;
        if (src && /^https?:/.test(src)) img.setAttribute('data-wb-src', src);
        const nw = img.naturalWidth || 0, nh = img.naturalHeight || 0;
        const r = img.getBoundingClientRect();
        img.setAttribute('data-wb-size', [nw, nh, Math.round(r.width), Math.round(r.height)].join(','));
      }
      S.map[idx] = el.outerHTML;
    }
  }
  while (S.y < S.total) {
    window.scrollTo(0, S.y);
    await new Promise(r => setTimeout(r, 130));
    grab();
    S.y += S.step;
    if (Date.now() - t0 > 40000) break;
  }
  S.total = document.documentElement.scrollHeight;
  if (S.y >= S.total) { window.scrollTo(0, S.total); await new Promise(r => setTimeout(r, 250)); grab(); }
  const keys = Object.keys(S.map).map(Number).sort((a,b)=>a-b);
  return { y: S.y, total: S.total, collected: keys.length, done: S.y >= S.total };
})()`;

let st, guard = 0;
do {
  st = await ev(batch, 150000); guard++;
  log(`batch ${guard}: ${st.y}/${st.total} 块 ${st.collected}${st.done ? ' 完成' : ''}`);
} while (!st.done && guard < 60);

// ─── 列表序号补回正文（序号是独立节点，提取器会丢） ────────────────────────

const markers = await ev(`(() => {
  const S = window.__YT__;
  const keys = Object.keys(S.map).map(Number).sort((a,b)=>a-b);
  let b = 0, o = 0;
  for (const k of keys) {
    const html = S.map[k];
    if (!/docx-bullet-block|docx-ordered-block/.test(html)) continue;
    const tpl = document.createElement('div'); tpl.innerHTML = html;
    const root = tpl.firstElementChild;
    const block = root.querySelector('.block'); const content = root.querySelector('.list-content');
    if (!block || !content) continue;
    let marker = '';
    if (/docx-bullet-block/.test(block.className)) {
      const dot = root.querySelector('.bullet-dot-style, .bullet'); marker = '• '; if (dot) dot.remove(); b++;
    } else {
      const ord = root.querySelector('.order'); marker = ((ord && ord.textContent.trim()) || '1.') + ' '; if (ord) ord.remove(); o++;
    }
    const first = content.querySelector('.ace-line') || content.firstElementChild;
    if (first) { const s = document.createElement('span'); s.textContent = marker; first.insertBefore(s, first.firstChild); }
    S.map[k] = root.outerHTML;
  }
  return { bullets: b, orders: o };
})()`);
log('列表标记', JSON.stringify(markers));

// ─── 离屏重组 + 提取 ────────────────────────────────────────────────────────

const built = await ev(`(() => {
  const S = window.__YT__;
  const keys = Object.keys(S.map).map(Number).sort((a,b)=>a-b);
  const old = document.getElementById('__W2F_ROOT__'); if (old) old.remove();
  const holder = document.createElement('div');
  holder.id = '__W2F_ROOT__';
  holder.setAttribute('style', 'position:absolute;left:-99999px;top:0;width:820px;');
  holder.innerHTML = keys.map(k => S.map[k]).join('');
  document.body.appendChild(holder);
  window.__W2F_ROOT__ = holder;
  return { blocks: keys.length, chars: (holder.innerText||'').length };
})()`);
log('重组', JSON.stringify(built));

const data = await ev(EXTRACT);
data.title = data.title || init.title;

// ─── 落盘 ───────────────────────────────────────────────────────────────────

const OUTDIR = path.resolve(flags.out || path.join('webpage-captures', 'doc-' + Date.now()));
const imagesDir = path.join(OUTDIR, 'images');
fs.mkdirSync(imagesDir, { recursive: true });

let md = data.markdown;
md = md.replace(/^\\(\d+)\\?\.\s/gm, '$1. ').replace(/^(\d+)\\\.\s/gm, '$1. ');
md = md.replace(/(.)\\>/g, '$1>');
md = md.replace(/^(\d+)\.[ \t]*\n\n(?=\S)/gm, '$1. ');
md = md.replace(/^•\s?/gm, '- ');
md = md.replace(/\\~/g, '~');
md = md.replace(/^\\_\\_\\_[ \t]*$/gm, '---');
md = md.replace(/\n{3,}/g, '\n\n').trim();

const EXT = { 'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif', 'image/svg+xml': 'svg' };
const list = data.images || [];
const results = new Map();
let cursor = 0;
const referer = (data.url || flags.url || '');
async function downloadOnce(img, name) {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 45000);
  let res;
  try {
    res = await fetch(img.src, { headers: { referer, 'user-agent': 'Mozilla/5.0' }, signal: ctrl.signal });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const buf = Buffer.from(await res.arrayBuffer());
    const type = (res.headers.get('content-type') || '').split(';')[0].trim();
    let ext = EXT[type] || path.extname(new URL(img.src).pathname).replace('.', '') || 'png';
    if (ext === 'jpeg') ext = 'jpg';
    const file = `${name}.${ext}`;
    fs.writeFileSync(path.join(imagesDir, file), buf);
    return { file, bytes: buf.length };
  } finally {
    clearTimeout(timer);
  }
}
// 大事记：2026-10-24 一张 1.8MB 的图被 fetch "terminated"，没重试就掉图。改成 3 次退避重试
async function download(img, name) {
  let lastErr;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try { return await downloadOnce(img, name); }
    catch (e) {
      lastErr = e;
      if (attempt < 3) await new Promise(r => setTimeout(r, 800 * attempt));
    }
  }
  throw lastErr;
}
async function worker() {
  while (true) {
    const n = cursor++;
    if (n >= list.length) return;
    const img = list[n];
    if (!img.src || img.src.startsWith('data:')) continue;
    try { results.set(img.idx, await download(img, String(n + 1).padStart(3, '0'))); }
    catch (e) { console.error(`图片 ${n + 1} 失败：${e.message}`); results.set(img.idx, { file: null, error: e.message }); }
  }
}
await Promise.all(Array.from({ length: 6 }, () => worker()));

// 下载不了时：链接本身是死的（404 / 没有文件名）就别再留远程 URL——
// 飞书侧同样取不到，只会留下一个破图；改成一行说明更清楚（2026-10-31 加）
const DEAD_NOTE = '*（原文档此处有一个媒体内容，链接已失效，未能取回）*';
const isDeadSrc = (src, err) => {
  if (!src) return true;
  if (/HTTP 404/.test(err || '')) return true;
  try { return !path.basename(new URL(src).pathname); } catch { return true; }
};
let mdLocal = md, mdFeishu = md;
for (const img of list) {
  const r = results.get(img.idx);
  const key = `{{IMG_${String(img.idx).padStart(4, '0')}}}`;
  const alt = (img.alt || '').replace(/[\[\]\n]/g, ' ').trim();
  const dead = isDeadSrc(img.src, r && r.error);
  const local = r && r.file ? `![${alt}](images/${r.file})` : (dead ? DEAD_NOTE : `![${alt}](${img.src})`);
  const feishu = r && r.file ? `![${alt}](@./images/${r.file})` : (dead ? DEAD_NOTE : `![${alt}](${img.src})`);
  mdLocal = mdLocal.split(key).join(local);
  mdFeishu = mdFeishu.split(key).join(feishu);
}
mdLocal = mdLocal.replace(/\{\{IMG_[\d_]*\}?\}?/g, '');
mdFeishu = mdFeishu.replace(/\{\{IMG_[\d_]*\}?\}?/g, '');

const stamp = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false }).format(new Date()).replace(/\//g, '-');
const src = flags.url || data.url;
const header = `> 来源：[原文链接](${src})\n> 采集时间：${stamp}\n\n`;
fs.writeFileSync(path.join(OUTDIR, 'content.md'), `# ${data.title}\n\n${header}${mdLocal}\n`, 'utf8');
fs.writeFileSync(path.join(OUTDIR, 'content.feishu.md'), `${header}${mdFeishu}\n`, 'utf8');

const meta = {
  title: data.title, url: src, capturedAt: new Date().toISOString(),
  blocks: built.blocks, imageCount: [...results.values()].filter(r => r.file).length,
  images: list.map(img => ({ idx: img.idx, src: img.src, file: (results.get(img.idx) || {}).file || null })),
};
fs.writeFileSync(path.join(OUTDIR, 'meta.json'), JSON.stringify(meta, null, 2), 'utf8');
log(`落盘 ${OUTDIR}：${built.blocks} 块 / ${meta.imageCount} 图 / ${mdLocal.length} 字`);

// ─── 发布 ───────────────────────────────────────────────────────────────────

if (!flags['no-publish']) {
  const args = [path.join(SCRIPT_DIR, 'publish.mjs'), '--dir', OUTDIR, '--json'];
  if (flags.folder) args.push('--folder', flags.folder);
  if (flags.title) args.push('--title', flags.title);
  const r = spawnSync(process.execPath, args, { cwd: OUTDIR, encoding: 'utf8', maxBuffer: 64 * 1024 * 1024 });
  if (r.stdout) log(r.stdout.trim());
  if (r.status !== 0) console.error((r.stderr || '').trim());
}

// 先 detach 会话再关连接，尽量别把调试端点留在“已占用”状态
try { await cdp.send('Target.detachFromTarget', { sessionId: sid }, null, 5000); } catch { /* ignore */ }
await new Promise((res) => {
  try { cdp.ws.onclose = () => res(); cdp.ws.close(); } catch { res(); }
  setTimeout(res, 1200);
});
process.exit(0);
