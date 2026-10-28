#!/usr/bin/env node
// legacy-doc.mjs — 抓「旧版飞书文档」（/docs/doccn...，etherpad 渲染）并发布到飞书。
//
// 为什么需要它：
//   1. lark-cli docs +fetch 只吃 docx / wiki，旧版 /docs/ 链接直接报 unsupported；
//   2. drive +export 对跨租户文档返回 permission denied；
//   3. 页面正文是 etherpad 的 .ace-line，且按视口虚拟化（离屏行被回收），
//      普通「整页滚动 + 提取」只能拿到开头几十行。
//
// 做法：按 .ace-line 的 top 坐标去重收集 → 离屏重组 → 转 markdown；
//      图片真实地址在 img[data-src]（src 是 1×1 占位图，lazy 加载经常不触发），
//      所以不依赖图片是否加载出来，直接用 cookie 下载 data-src。
//
// 用法：
//   node legacy-doc.mjs --url "https://xxx.feishu.cn/docs/doccnXXXX" \
//        [--out <目录>] [--folder <文件夹|知识库节点>] [--title "..."] [--no-publish]
//
// 前置：lark-cli user 已授权；本机有 Chrome（ensure-chrome 会拉起专用调试 Chrome，端口 9333）

import fs from 'fs';
import os from 'os';
import path from 'path';
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
const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.log(...a);

if (flags.help || !flags.url) {
  console.log('node legacy-doc.mjs --url <旧版飞书文档链接> [--out 目录] [--folder 文件夹|wiki节点] [--title ...] [--no-publish]');
  process.exit(0);
}

// ─── 浏览器端点 ──────────────────────────────────────────────────────────────
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
      const t = setTimeout(() => rej(new Error('握手超时：调试端点可能卡死')), 12000);
      ws.onopen = () => { clearTimeout(t); res(); };
      ws.onerror = () => rej(new Error('CDP 连接失败'));
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

const { targetInfos } = await cdp.send('Target.getTargets');
const tok = (flags.url.match(/\/docs\/([A-Za-z0-9]+)/) || [])[1] || '';
const hit = targetInfos.find(t => t.type === 'page' && tok && t.url.includes(tok));
const targetId = hit ? hit.targetId : (await cdp.send('Target.createTarget', { url: flags.url })).targetId;
log(hit ? '复用标签页 ' + targetId : '新开标签页 ' + targetId);

const { sessionId: sid } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
await cdp.send('Page.enable', {}, sid);
await cdp.send('Emulation.setUserAgentOverride', {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
  acceptLanguage: 'zh-CN,zh;q=0.9',
}, sid);
await cdp.send('Page.navigate', { url: flags.url }, sid, 30000).catch(() => {});

async function ev(expr, timeout = 180000) {
  const r = await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true }, sid, timeout);
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.exception?.description || r.exceptionDetails.text);
  return r.result?.value;
}

// ─── 等页面就绪（etherpad 容器） ─────────────────────────────────────────────
let triedSync = false;
let ready = false;
for (let i = 0; i < 60; i++) {
  await sleep(500);
  const st = await ev(`({ href: location.href, ready: document.readyState,
    ep: !!document.querySelector('#innerdocbody'),
    docx: !!document.querySelector('.virtual-list') })`, 8000).catch(() => null);
  if (!st) continue;
  if (/accounts\.feishu\.cn|sso\./.test(st.href) && !st.ep) {
    if (triedSync) { console.error('ERROR: 仍然需要登录，先跑 login.mjs 扫码'); process.exit(2); }
    triedSync = true;
    log('落到登录页，尝试从用户 Chrome 同步登录态…');
    const r = spawnSync(process.execPath, [path.join(SCRIPT_DIR, 'sync-login.mjs')], { encoding: 'utf8' });
    if (r.status === 0) { log((r.stdout || '').trim()); await cdp.send('Page.navigate', { url: flags.url }, sid); continue; }
    console.error('ERROR: ' + ((r.stderr || '').trim() || '同步登录失败，先跑 login.mjs'));
    process.exit(2);
  }
  if (st.docx && !st.ep) {
    console.error('ERROR: 这页是 docx 渲染（有 .virtual-list），用 blocklist.mjs 或 docs +fetch');
    process.exit(3);
  }
  if (st.ready === 'complete' && st.ep) { ready = true; break; }
}
if (!ready) { console.error('ERROR: 页面没等到 etherpad 容器'); process.exit(4); }

const pageTitle = (await ev('document.title')).replace(/\s*-\s*飞书云文档\s*$/, '').trim();
log('标题：' + pageTitle);

// ─── 收集行 ─────────────────────────────────────────────────────────────────
const init = await ev(`(() => {
  const walk = (n) => { while (n && n !== document.body) { const s = getComputedStyle(n);
    if (n.scrollHeight > n.clientHeight + 50 && /auto|scroll/.test(s.overflowY)) return n; n = n.parentElement; } return null; };
  const sc = walk(document.querySelector('#innerdocbodyWrap')) || walk(document.querySelector('#innerdocbody'));
  if (!sc) return JSON.stringify({ ok: false });
  window.__EP__ = { lines: {}, imgs: {}, sc };
  sc.scrollTop = 0;
  return JSON.stringify({ ok: true, sh: sc.scrollHeight, ch: sc.clientHeight });
})()`);
if (!JSON.parse(init).ok) { console.error('ERROR: 找不到滚动容器'); process.exit(5); }

const COLLECT = `(() => {
  const st = window.__EP__, sc = st.sc;
  const scRect = sc.getBoundingClientRect();
  document.querySelectorAll('#innerdocbody .ace-line[data-node]').forEach(el => {
    const id = el.id; if (!id) return;
    const top = Math.round(el.getBoundingClientRect().top - scRect.top + sc.scrollTop);
    const prev = st.lines[id];
    if (!prev || top < prev.top) st.lines[id] = { id, top, cls: el.className, html: el.outerHTML };
    el.querySelectorAll('img').forEach(img => {
      const real = img.getAttribute('data-src') || (img.currentSrc || img.src || '');
      const uuid = img.getAttribute('data-uuid') || real.slice(-40);
      const rec = st.imgs[uuid] || (st.imgs[uuid] = { uuid, url: real, w: img.naturalWidth, h: img.naturalHeight, line: id });
      if (!rec.line) rec.line = id;
      if (rec.w <= 1 && img.naturalWidth > 1) { rec.w = img.naturalWidth; rec.h = img.naturalHeight; }
    });
  });
  return 1;
})()`;

const step = 320;
let start = 0, lastKey = '', stall = 0;
while (start < 250) {
  const r = await ev(`(async () => {
    const sc = window.__EP__.sc;
    const sleep = (ms) => new Promise(r => setTimeout(r, ms));
    for (let i = ${start}; i < ${start + 15}; i++) { sc.scrollTop = i * ${step}; await sleep(140); ${COLLECT}; }
    return JSON.stringify({ lines: Object.keys(window.__EP__.lines).length, imgs: Object.keys(window.__EP__.imgs).length,
      scroll: Math.round(sc.scrollTop), sh: sc.scrollHeight, ch: sc.clientHeight,
      atEnd: sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 20 });
  })()`);
  start += 15;
  const s = JSON.parse(r);
  log(`  滚动 ${s.scroll}/${s.sh} · 行 ${s.lines} · 图 ${s.imgs}`);
  const key = s.lines + ':' + s.sh;
  if (key === lastKey) stall++; else stall = 0;
  lastKey = key;
  if (s.atEnd && stall >= 2) break;
}

const data = JSON.parse(await ev(`JSON.stringify({ lines: window.__EP__.lines, imgs: window.__EP__.imgs })`));
const lines = Object.values(data.lines).sort((a, b) => a.top - b.top);
log(`收集完成：${lines.length} 行，${Object.keys(data.imgs).length} 张图`);

// ─── 图片：cookie 下载 ──────────────────────────────────────────────────────
await cdp.send('Network.enable', {}, sid);
const ck = await cdp.send('Network.getAllCookies', {}, sid);
const cookies = (ck.cookies || []).filter(c => /feishu\.cn$/.test(c.domain.replace(/^\./, '')));
const cookieHeader = cookies.map(c => c.name + '=' + c.value).join('; ');

// 行内 img 统一成真实地址
for (const l of lines) {
  l.html = l.html.replace(/<img[^>]*>/g, (tag) => {
    const ds = /data-src="([^"]+)"/.exec(tag);
    const src = /src="([^"]+)"/.exec(tag);
    return '<img src="' + ((ds && ds[1]) || (src && src[1]) || '') + '">';
  });
}

const urls = [...new Set(lines.flatMap(l => [...l.html.matchAll(/<img[^>]*src="([^"]+)"/g)].map(m => m[1])))]
  .filter(u => /^https?:/.test(u));
log('待下载图片 ' + urls.length);

const fileMap = new Map();  // url -> 本地文件名
let cursor = 0, okImg = 0, failImg = 0;
const imgDir = path.join(flags.out ? path.resolve(flags.out) : defaultOut(), 'images');
fs.mkdirSync(imgDir, { recursive: true });
const EXT = { 'image/png': '.png', 'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/gif': '.gif', 'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/bmp': '.bmp' };

async function worker() {
  while (true) {
    const i = cursor++;
    if (i >= urls.length) return;
    const url = urls[i];
    try {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 30000);
      const r = await fetch(url, {
        signal: ctrl.signal,
        headers: {
          Cookie: cookieHeader,
          Referer: new URL(flags.url).origin + '/',
          'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
        },
      });
      clearTimeout(timer);
      if (!r.ok) throw new Error('HTTP ' + r.status);
      const buf = Buffer.from(await r.arrayBuffer());
      if (buf.length < 200) throw new Error('文件过小 ' + buf.length);
      const ext = EXT[(r.headers.get('content-type') || '').split(';')[0].trim()] || '.png';
      const name = String(i + 1).padStart(3, '0') + ext;
      fs.writeFileSync(path.join(imgDir, name), buf);
      fileMap.set(url, name);
      okImg++;
    } catch (e) {
      failImg++;
      log('  图片失败 ' + url.slice(-40) + ' · ' + String(e.message).slice(0, 50));
    }
  }
}
await Promise.all(Array.from({ length: 6 }, worker));
log(`图片 ${okImg} 张，失败 ${failImg}`);

// ─── 转 markdown ────────────────────────────────────────────────────────────
const dec = (s) => s.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&');

function inlineMarkup(html) {
  const h = html
    .replace(/<span[^>]*class="[^"]*ace-line-pocket[^"]*"[^>]*>[\s\S]*?<\/span>/g, '')
    .replace(/<div class="new-gallery"[\s\S]*?<\/div>\s*<\/div>/g, '');
  const tokens = h.match(/<[^>]+>|[^<]+/g) || [];
  let out = '';
  const stack = [];
  const styleOf = (attr) => ({
    bold: /font-weight:\s*(bold|[6-9]00)/.test(attr),
    italic: /font-style:\s*italic/.test(attr),
    strike: /line-through/.test(attr),
  });
  for (const t of tokens) {
    if (t.startsWith('<')) {
      const name = (/^<\/?([a-z0-9]+)/i.exec(t) || [])[1];
      if (!name) continue;
      if (/^<\//.test(t)) {
        for (let i = stack.length - 1; i >= 0; i--) if (stack[i].name === name) { stack.splice(i); break; }
      } else if (!/\/>$/.test(t) && name !== 'br' && name !== 'img') {
        stack.push({ name, ...styleOf(t) });
      }
      if (name === 'br') out += '\n';
      continue;
    }
    let text = dec(t).replace(/\u200b/g, '');
    if (!text) continue;
    const agg = stack.reduce((a, s) => ({
      bold: a.bold || s.bold, italic: a.italic || s.italic, strike: a.strike || s.strike,
    }), { bold: false, italic: false, strike: false });
    if (agg.strike) text = '~~' + text + '~~';
    if (agg.italic) text = '*' + text + '*';
    if (agg.bold) text = '**' + text + '**';
    out += text;
  }
  return out.replace(/\*\*\s*\*\*/g, '').replace(/\u200b/g, '').trim();
}

const blocks = [];
let listStack = null;
for (const l of lines) {
  const cls = l.cls;
  const head = /heading-h([1-6])/.exec(cls);
  const text = inlineMarkup(l.html);
  if (/gallery-line/.test(cls)) {
    const ims = [...l.html.matchAll(/<img[^>]*src="([^"]+)"/g)].map(m => fileMap.get(m[1])).filter(Boolean);
    if (ims.length) { listStack = null; blocks.push(ims.map(n => `![](@./images/${n})`).join('\n')); }
    continue;
  }
  if (head) { listStack = null; if (text) blocks.push('#'.repeat(Number(head[1])) + ' ' + text); continue; }
  if (/list-div/.test(cls)) {
    const m = /<ol[^>]*start="?(\d+)"?/.exec(l.html);
    const ol = !!m;
    if (!listStack || listStack.type !== (ol ? 'ol' : 'ul')) {
      listStack = { type: ol ? 'ol' : 'ul', n: ol ? Number(m[1]) - 1 : 0 };
    }
    listStack.n++;
    blocks.push((ol ? listStack.n + '.' : '-') + ' ' + text);
    continue;
  }
  listStack = null;
  if (!text) continue;
  if (!blocks.length && text === pageTitle) continue;
  blocks.push(text);
}

let md = '';
const isLi = (b) => /^(\d+\.|-)\s/.test(b);
blocks.forEach((b, i) => {
  if (i === 0) { md = b; return; }
  md += (isLi(b) && isLi(blocks[i - 1]) ? '\n' : '\n\n') + b;
});
md = md.replace(/\n{3,}/g, '\n\n').trim() + '\n';

const title = flags.title || pageTitle;
const stamp = new Date();
const p = (n) => String(n).padStart(2, '0');
const localTime = `${stamp.getFullYear()}-${p(stamp.getMonth() + 1)}-${p(stamp.getDate())} ${p(stamp.getHours())}:${p(stamp.getMinutes())}`;
const header = `# ${title}\n\n> 来源：[飞书文档](${flags.url})\n> 复制时间：${localTime}\n\n`;
const body = header + md;

const OUT = flags.out ? path.resolve(flags.out) : defaultOut();
fs.mkdirSync(OUT, { recursive: true });
fs.writeFileSync(path.join(OUT, 'content.md'), body.replace(/@\.\//g, ''), 'utf8');
fs.writeFileSync(path.join(OUT, 'content.feishu.md'), body, 'utf8');
fs.writeFileSync(path.join(OUT, 'meta.json'), JSON.stringify({
  title, source: flags.url, renderer: 'etherpad-legacy',
  imageCount: fileMap.size, imageTotal: urls.length, imageFailed: failImg,
  lines: lines.length, chars: md.length, outDir: OUT,
}, null, 2), 'utf8');
log(`正文 ${md.length} 字 · 图 ${fileMap.size} 张 · 本地 ${OUT}`);

if (flags['no-publish']) {
  console.log(OUT);
  process.exit(0);
}

const folder = flags.folder || (() => {
  try { const c = JSON.parse(fs.readFileSync(path.join(os.homedir(), '.workbuddy/data/yitang-to-feishu/config.json'), 'utf8')); return c.folderUrl || c.folder; } catch { return ''; }
})();
if (!folder) { console.error('ERROR: 没有目标位置，用 --folder 指定'); process.exit(6); }

const pubArgs = [path.join(SCRIPT_DIR, 'publish.mjs'), '--dir', OUT, '--folder', folder, '--json'];
const pub = spawnSync(process.execPath, pubArgs, { encoding: 'utf8', cwd: OUT, maxBuffer: 64 * 1024 * 1024 });
const out = (pub.stdout || '') + (pub.stderr || '');
const s = out.indexOf('{');
let res = null;
if (s >= 0) { try { res = JSON.parse(out.slice(s)); } catch { /* ignore */ } }
if (!res || !res.ok) { console.error('ERROR: 发布失败 ' + out.trim().slice(0, 300)); process.exit(7); }
console.log(JSON.stringify({ ...res, images: fileMap.size, localDir: OUT }));

function defaultOut() {
  const slug = (pageTitle || 'legacy-doc').replace(/[\\/:*?"<>|\s]+/g, '-').slice(0, 40);
  const d = new Date();
  const stamp2 = `${d.getFullYear()}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`;
  return path.join(process.cwd(), 'webpage-captures', `${slug}-${stamp2}`);
}
