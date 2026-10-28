#!/usr/bin/env node
/**
 * 小红书作品列表采集 —— 用一个受控 Chrome 打开小红书，让页面自己去发请求。
 *
 * 为什么不做「cookie + 直接打接口」：小红书的接口要 `x-s` / `x-t` 签名，
 * 签名由页面里的 JS 现算（`window._webmsxyw`），而且服务端会校验请求是不是
 * 从带着完整设备状态的页面里出来的。实测用页面里那个函数手动签一次，
 * 详情接口直接回 `406 / code -1`；而让页面自己滚动，`user_posted` 接口
 * 每页 30 条稳定返回。所以这里的策略是**只做搬运，不做伪造**：
 *
 *   1. 从本机 Chrome 解出登录态 cookie（xhs_cookie.py），注入一个独立的
 *      无头 Chrome（不动用户正在用的那个窗口）；
 *   2. 打开作者主页，注入一个 hook 拦住 `user_posted` 的响应，滚动加载；
 *   3. 逐条打开笔记详情页（带 `xsec_token`），读页面里的 `__INITIAL_STATE__`。
 *
 * 产出与抖音那条链路**同一份 13 字段契约**，所以 feishu_bitable.py 不用改。
 *
 * 用法：
 *   node xhs.mjs "<主页链接>" [--count N] [--no-detail] [--out rows.json]
 *   node xhs.mjs --stop            # 关掉后台那个受控 Chrome
 *   node xhs.mjs --status          # 看受控 Chrome 是否在跑
 */

import { spawn, execFile } from 'node:child_process';
import { existsSync, mkdirSync, openSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const DATA_DIR = resolve(homedir(), '.workbuddy/data/xiaohongshu-cdp');
const PORT = Number(process.env.XHS_CDP_PORT || 9333);
const PROFILE_DIR = resolve(DATA_DIR, 'chrome-profile');
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

/** 13 字段契约，与抖音那条链路逐字一致（下游按列名认列）。 */
const FIELDS = [
  '账号名称', '账号主页链接', '搜索关键词', '发布日期', '作品标题', '作品标签',
  '作品文案', '点赞', '评论', '收藏', '分享', '作品链接', '视频链接',
];

const BEIJING_OFFSET_MS = 8 * 3600 * 1000;

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const log = (...a) => console.error(...a);

// --------------------------------------------------------------------------- //
// CDP
// --------------------------------------------------------------------------- //
class CDP {
  constructor(url) { this.url = url; this.seq = 0; this.pending = new Map(); }
  async open() {
    this.ws = new WebSocket(this.url);
    await new Promise((res, rej) => {
      this.ws.addEventListener('open', res, { once: true });
      this.ws.addEventListener('error', rej, { once: true });
    });
    this.ws.addEventListener('message', (ev) => {
      const msg = JSON.parse(ev.data);
      if (msg.id && this.pending.has(msg.id)) {
        const { resolve, reject } = this.pending.get(msg.id);
        this.pending.delete(msg.id);
        msg.error ? reject(new Error(JSON.stringify(msg.error))) : resolve(msg.result);
      }
    });
  }
  send(method, params = {}, timeoutMs = 30000) {
    const id = ++this.seq;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 超时：${method}`));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: (v) => { clearTimeout(timer); resolve(v); },
        reject: (e) => { clearTimeout(timer); reject(e); },
      });
    });
  }
  /** 求值并直接拿回 JS 值（返回字符串/数字/对象都行）。 */
  async eval(expression, { awaitPromise = false } = {}) {
    const r = await this.send('Runtime.evaluate', {
      expression, awaitPromise, returnByValue: true,
    });
    if (r.exceptionDetails) {
      throw new Error(`页面里执行失败：${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ''}`);
    }
    return r.result?.value;
  }
  close() { try { this.ws.close(); } catch { /* 已断就不用管 */ } }
}

async function cdpVersion() {
  try {
    const r = await fetch(`http://127.0.0.1:${PORT}/json/version`, { signal: AbortSignal.timeout(1500) });
    return r.ok ? await r.json() : null;
  } catch { return null; }
}

async function ensureChrome() {
  if (await cdpVersion()) return { spawned: false };
  if (!existsSync(CHROME)) throw new Error(`找不到 Chrome：${CHROME}`);
  mkdirSync(PROFILE_DIR, { recursive: true });
  const logPath = resolve(DATA_DIR, 'chrome.log');
  const logFd = openSync(logPath, 'a');
  const child = spawn(CHROME, [
    `--remote-debugging-port=${PORT}`,
    `--user-data-dir=${PROFILE_DIR}`,
    '--no-first-run', '--no-default-browser-check',
    '--headless=new',
    // 这几个不是可选项：macOS 上无头模式偶尔会「GPU process isn't usable」
    // 直接 FATAL 退出，加上这几个才会稳定起得来（实测不加必崩）。
    '--no-sandbox', '--disable-gpu', '--disable-gpu-sandbox',
    '--disable-software-rasterizer', '--disable-dev-shm-usage',
    '--mute-audio', '--window-size=1440,900',
    '--disable-blink-features=AutomationControlled',
    'about:blank',
  ], { detached: true, stdio: ['ignore', logFd, logFd] });
  child.unref();
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if (await cdpVersion()) return { spawned: true, pid: child.pid };
  }
  let tail = '';
  try { tail = readFileSync(logPath, 'utf8').split('\n').slice(-6).join('\n'); } catch { /* 没日志 */ }
  throw new Error(`受控 Chrome 没能在 20 秒内起来（端口 ${PORT} 被占？）\n${tail}`);
}

async function firstPage() {
  const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
  return list.find((t) => t.type === 'page') || null;
}

// --------------------------------------------------------------------------- //
// 页面里的注入与读取
// --------------------------------------------------------------------------- //
/** 拦 user_posted 的响应。用 fetch + XHR 双钩，页面走哪条都能拦住。
 *
 * 同时**在页面里累计**笔记卡片（`window.__xhsCards`）：主页列表是虚拟滚动，
 * 节点会被回收 —— 从外面每隔一秒抓一次 DOM 只能看到当时在场的那十几张，
 * 越往下滚越少。所以在页面内用定时器持续把见过的卡片塞进 Map，外面只读结果。
 * 这一步是「不丢条目」的关键：首屏 30 条根本不发接口请求，只在 DOM 里。
 */
const HOOK = `(() => {
  if (window.__xhsCaptured) return;
  window.__xhsCaptured = true;
  window.__xhsCap = [];
  window.__xhsCards = new Map();

  const keep = (url, text) => {
    try {
      if (!url || !url.includes('/api/sns/web/v1/user_posted')) return;
      window.__xhsCap.push({ url, body: text });
    } catch (e) {}
  };
  const origFetch = window.fetch;
  window.fetch = async function (...args) {
    const res = await origFetch.apply(this, args);
    try {
      const u = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) || '';
      res.clone().text().then((t) => keep(u, t)).catch(() => {});
    } catch (e) {}
    return res;
  };
  const oOpen = XMLHttpRequest.prototype.open;
  const oSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (m, u, ...rest) { this.__u = u; return oOpen.call(this, m, u, ...rest); };
  XMLHttpRequest.prototype.send = function (...rest) {
    this.addEventListener('load', () => { try { keep(this.__u || '', this.responseText); } catch (e) {} });
    return oSend.apply(this, rest);
  };

  // 卡片：id / token / 标题 / 点赞 / 作者 uid（用来剔掉「相关推荐」里的别人）
  const scan = () => {
    try {
      for (const el of document.querySelectorAll('section.note-item')) {
        const id = el.getAttribute('data-note-id');
        if (!id || window.__xhsCards.has(id)) continue;
        const a = el.querySelector('a[href*="xsec_token"]') || el.querySelector('a[href*="' + id + '"]');
        const href = a ? a.getAttribute('href') : '';
        const uidMatch = href.match(/\\/user\\/profile\\/([0-9a-z]{24})\\//);
        const tok = href.match(/xsec_token=([^&]+)/);
        const t = el.querySelector('.title');
        const l = el.querySelector('.like-wrapper .count') || el.querySelector('.count');
        window.__xhsCards.set(id, {
          note_id: id,
          author_uid: uidMatch ? uidMatch[1] : '',
          xsec_token: tok ? decodeURIComponent(tok[1]) : '',
          display_title: t ? t.textContent.trim() : '',
          liked_text: l ? l.textContent.trim() : '',
        });
      }
    } catch (e) {}
  };
  scan();
  setInterval(scan, 700);
})();`;

/** 页内累计下来的笔记卡片（见 HOOK 里那个 700ms 的扫描）。
 *
 * 只回传 id/token/标题/点赞/作者，不搬正文。作者 uid 留着给外面过滤：
 * 主页底部常混进「相关推荐」，那些卡片长得一样但作者是别人。
 */
const READ_CARDS = `(() => {
  const out = [];
  for (const c of (window.__xhsCards ? window.__xhsCards.values() : [])) out.push(c);
  return JSON.stringify(out);
})()`;

/** 读出所有已截获的分页（只回传游标/条数，正文留在页面里，避免反复搬运 36KB）。 */
const READ_CAPTURES = `(() => {
  const caps = window.__xhsCap || [];
  const out = [];
  for (const c of caps) {
    try {
      const j = JSON.parse(c.body);
      const d = j.data || {};
      const notes = d.notes || [];
      out.push({
        url: c.url,
        hasMore: !!d.has_more,
        cursor: d.cursor || '',
        code: j.code,
        msg: j.msg || '',
        notes: notes.map((n) => ({
          note_id: n.note_id, type: n.type, display_title: n.display_title || '',
          xsec_token: n.xsec_token || '',
          time: n.time || 0,
          user: { user_id: (n.user||{}).user_id, nickname: (n.user||{}).nickname || (n.user||{}).nick_name || '' },
          interact: n.interact_info || {},
          cover: (n.cover && (n.cover.url_default || n.cover.url_pre)) || '',
        })),
      });
    } catch (e) { /* 半截响应，跳 */ }
  }
  return JSON.stringify(out);
})()`;

/** 主页头部的作者信息（昵称、笔记数、粉丝、获赞与收藏）。
 *  笔记数不在 interactions 里（那里只有关注/粉丝/获赞与收藏），
 *  它在 tabPublic 的「笔记」页签上 —— 两者都要摸，才不至于恒为 null。 */
const READ_PROFILE = `(() => {
  const s = window.__INITIAL_STATE__ || {};
  const unwrap = (v) => (v && v._rawValue) || (v && v._value) || v;
  const page = unwrap(s.user && s.user.userPageData) || {};
  const basic = page.basicInfo || page.basic_info || {};
  const inter = page.interactions || [];
  const stat = (t) => { const x = inter.find((i) => i.type === t); return x ? x.count : null; };
  const tabsRaw = unwrap(page.tabPublic);
  // tabPublic 有两种形状：数组 [{name,count}] 或对象 {笔记: n}。两种都认，
  // 认不出就算了 —— 笔记数只用于「采全了没有」的自检，不该让整批失败。
  let notes = stat('notes') ?? stat('note') ?? null;
  try {
    if (Array.isArray(tabsRaw)) {
      const t = tabsRaw.find((x) => String((x && x.name) || '').includes('笔记'));
      if (t && t.count !== undefined) notes = t.count;
    } else if (tabsRaw && typeof tabsRaw === 'object') {
      const hit = Object.entries(tabsRaw).find(([k]) => String(k).includes('笔记'));
      if (hit) notes = typeof hit[1] === 'object' ? (hit[1].count ?? notes) : hit[1];
    }
  } catch (e) {}
  return JSON.stringify({
    nickname: basic.nickname || basic.nick_name || '',
    redId: basic.redId || basic.red_id || '',
    desc: basic.desc || '',
    ipLocation: basic.ipLocation || '',
    notes,
    fans: stat('fans'),
    liked: stat('interaction'),
  });
})()`;

/** 详情页：文案、标签、四项互动数、视频直链。 */
const READ_NOTE = `(() => {
  const s = window.__INITIAL_STATE__ || {};
  const unwrap = (v) => (v && v._rawValue) || (v && v._value) || v;
  const map = unwrap(s.note && s.note.noteDetailMap) || {};
  const key = Object.keys(map)[0];
  const n = key ? (map[key].note || {}) : {};
  const ii = n.interactInfo || {};

  // 视频直链藏在 video.media.stream.h264[].masterUrl；结构换过一次，
  // 所以这里递归找第一个像 mp4 的地址，别写死路径。
  const findUrl = (node, depth = 0) => {
    if (!node || depth > 6) return '';
    if (typeof node === 'string') return /^https?:.*\\.(mp4|m3u8)/.test(node) ? node : '';
    if (Array.isArray(node)) {
      for (const x of node) { const u = findUrl(x, depth + 1); if (u) return u; }
      return '';
    }
    if (typeof node === 'object') {
      for (const k of ['masterUrl', 'master_url', 'url', 'backupUrls', 'backup_urls']) {
        const u = findUrl(node[k], depth + 1); if (u) return u;
      }
      for (const v of Object.values(node)) { const u = findUrl(v, depth + 1); if (u) return u; }
    }
    return '';
  };

  return JSON.stringify({
    found: !!key,
    noteId: n.noteId || key || '',
    title: n.title || '',
    desc: n.desc || '',
    time: n.time || 0,
    type: n.type || '',
    tags: (n.tagList || []).map((t) => t.name).filter(Boolean),
    liked: ii.likedCount ?? null,
    collected: ii.collectedCount ?? null,
    comment: ii.commentCount ?? null,
    share: ii.shareCount ?? null,
    video: n.video ? findUrl(n.video) : '',
    images: (n.imageList || []).length,
  });
})()`;

// --------------------------------------------------------------------------- //
// 输入解析
// --------------------------------------------------------------------------- //
const HEX24 = /[0-9a-f]{24}/i;

function parseInput(text) {
  const raw = String(text || '').trim();
  if (!raw) throw new Error('输入为空');
  const url = (raw.match(/https?:\/\/[^\s，。；、）】]+/) || [])[0] || raw;

  let kind = null;
  let id = null;
  if (/\/user\/profile\//.test(url)) {
    kind = 'author';
    id = (url.match(/\/user\/profile\/([0-9a-z]{24})/i) || [])[1] || null;
  } else if (/\/explore\/|\/discovery\/item\//.test(url)) {
    kind = 'note';
    id = (url.match(/\/(?:explore|item)\/([0-9a-z]{24})/i) || [])[1] || null;
  } else if (/xhslink\.com|xhslink\.cn/.test(url)) {
    kind = 'short';
    id = url;
  } else if (HEX24.test(url) && !url.includes('://')) {
    kind = 'author';
    id = (url.match(HEX24) || [])[0];
  }
  if (!kind || !id) {
    throw new Error(`认不出这个输入：${raw}\n  支持：主页链接 /user/profile/<id>、笔记链接 /explore/<id>、xhslink 短链、裸用户 ID`);
  }
  return { raw, url, kind, id };
}

function beijing(ms) {
  if (!ms) return '';
  const d = new Date(Number(ms) + BEIJING_OFFSET_MS);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())} `
       + `${p(d.getUTCHours())}:${p(d.getUTCMinutes())}:${p(d.getUTCSeconds())}`;
}

function num(v) {
  if (v === null || v === undefined || v === '') return null;
  const n = Number(String(v).replace(/[^\d.]/g, ''));
  return Number.isFinite(n) ? n : null;
}

/** 正文里的 #话题#。小红书两种写法都有：`#投资[话题]#` 和老式的 `#投资`。 */
function tagsFromText(text) {
  if (!text) return [];
  const out = [];
  const re = /#([^#\[\]\s]{1,30})(?:\[话题\])?#/g;
  let m;
  while ((m = re.exec(text)) !== null) out.push(m[1].trim());
  if (out.length === 0) {
    for (const m2 of text.matchAll(/#([^#\s\[\]]{1,30})(?=\s|$)/g)) out.push(m2[1].trim());
  }
  return out;
}

function dedupeTags(list) {
  const seen = new Set();
  const out = [];
  for (const t of list) {
    const v = String(t || '').trim();
    if (!v || seen.has(v)) continue;
    seen.add(v);
    out.push(v);
  }
  return out;
}

// --------------------------------------------------------------------------- //
// 采集
// --------------------------------------------------------------------------- //
async function collect(input, opts) {
  const target = parseInput(input);
  log(`识别输入：${target.kind}　${target.id}`);

  const { spawned } = await ensureChrome();
  if (spawned) log('已启动受控 Chrome（无头，独立 profile，不碰你正在用的窗口）');

  // cookie 从本机 Chrome 自动取；取不到就带着空手上阵（公开主页通常也能看前几页）
  let cookieData = null;
  try {
    cookieData = await new Promise((res, rej) => {
      execFile('python3', [resolve(HERE, 'xhs_cookie.py'), 'export'], { timeout: 90000 },
        (err, stdout, stderr) => (err ? rej(new Error(stderr || err.message)) : res(JSON.parse(stdout))));
    });
    log(`已取到登录态 cookie：${cookieData.count} 条（${cookieData.profile.split('/').pop()}）`);
    for (const w of cookieData.warnings || []) log(`  ! ${w}`);
  } catch (err) {
    log(`! 没能自动取到 cookie（${String(err.message).slice(0, 120)}），先按游客跑`);
  }

  let page = await firstPage();
  if (!page) {
    await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: 'PUT' }).catch(() => {});
    await sleep(500);
    page = await firstPage();
  }
  if (!page) throw new Error('受控 Chrome 里没有可用的页面');

  const cdp = new CDP(page.webSocketDebuggerUrl);
  await cdp.open();
  await cdp.send('Page.enable');
  await cdp.send('Runtime.enable');
  const ua = cookieData?.user_agent
    || `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36`;
  await cdp.send('Emulation.setUserAgentOverride', { userAgent: ua, acceptLanguage: 'zh-CN,zh;q=0.9', platform: 'MacIntel' });
  if (cookieData?.cookies?.length) {
    await cdp.send('Network.enable');
    await cdp.send('Network.setCookies', { cookies: cookieData.cookies });
  }
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', { source: HOOK });

  // 短链先跳一次拿到真实地址
  let profileUrl;
  let noteOnly = null;
  if (target.kind === 'short') {
    await goto(cdp, target.id);
    await sleep(5000);
    const landed = await cdp.eval('location.href');
    log(`短链跳转到：${landed}`);
    const again = parseInput(landed);
    if (again.kind === 'note') { noteOnly = again; }
    else { profileUrl = `https://www.xiaohongshu.com/user/profile/${again.id}`; }
  } else if (target.kind === 'note') {
    noteOnly = target;
  } else {
    profileUrl = `https://www.xiaohongshu.com/user/profile/${target.id}`;
  }

  // 笔记链接输入：先打开那条笔记，从它的作者跳到主页
  if (noteOnly) {
    log('输入是笔记链接，先读作者…');
    const note = await readNote(cdp, noteOnly.id, target.url);
    const uid = await cdp.eval(`(() => {
      const s = window.__INITIAL_STATE__ || {};
      const unwrap = (v) => (v && v._rawValue) || (v && v._value) || v;
      const map = unwrap(s.note && s.note.noteDetailMap) || {};
      const k = Object.keys(map)[0];
      const n = k ? (map[k].note || {}) : {};
      return (n.user && n.user.userId) || '';
    })()`);
    if (!uid) throw new Error('这条笔记读不到作者，无法顺着它取作品列表');
    profileUrl = `https://www.xiaohongshu.com/user/profile/${uid}`;
    log(`作者 ${uid}，转到主页取全部作品`);
    var seedNote = note; // 这条笔记本身可能不在列表里（置顶/已删），先留着
  }

  // 1) 主页 + 滚动加载
  //
  // 首屏那 30 条是**服务端渲染进 HTML 的**，页面根本不发 `user_posted` 请求 ——
  // 只等接口响应会一条都等不到（踩过）。所以列表有两个来源：DOM 里的卡片
  // （首屏唯一来源）和滚动时新发出的接口响应（更全，带发布时间）。两路合并去重。
  log(`打开主页：${profileUrl}`);
  await goto(cdp, profileUrl);
  await waitFor(
    cdp,
    `document.querySelectorAll('section.note-item').length > 0
     || (Array.isArray(window.__xhsCap) && window.__xhsCap.length > 0)`,
    30000, '主页首屏',
  );

  const profile = JSON.parse((await cdp.eval(READ_PROFILE)) || '{}');
  log(`作者：${profile.nickname || '(未知)'}　主页笔记数：${profile.notes ?? '未知'}`);

  const seen = new Map();
  const wantsUid = target.kind === 'author' ? target.id : '';
  const cardToNote = (c) => ({
    note_id: c.note_id,
    type: '',
    display_title: c.display_title || '',
    xsec_token: c.xsec_token || '',
    time: 0,
    user: { user_id: c.author_uid || '', nickname: '' },
    interact: { liked_count: c.liked_text || '' },
    cover: '',
  });
  const merge = (list, { fromCards = false } = {}) => {
    let fresh = 0;
    for (const raw of list) {
      const n = fromCards ? cardToNote(raw) : raw;
      const id = n.note_id;
      if (!id) continue;
      // 主页底部会混进「相关推荐」（别人的笔记），作者对不上就丢掉。
      // 卡片上没解析出作者时放行 —— 宁可多一条待详情校验，也别漏采。
      if (fromCards && wantsUid && n.user.user_id && n.user.user_id !== wantsUid) continue;
      const old = seen.get(id);
      if (!old) {
        seen.set(id, n);
        fresh++;
      } else if (n.time && !old.time) {
        seen.set(id, { ...old, ...n });
      }
    }
    return fresh;
  };

  let idle = 0;
  const want = opts.count || Infinity;
  for (let round = 0; round < 200; round++) {
    const cards = JSON.parse((await cdp.eval(READ_CARDS)) || '[]');
    const caps = JSON.parse((await cdp.eval(READ_CAPTURES)) || '[]');
    let fresh = merge(cards, { fromCards: true });
    for (const c of caps) fresh += merge(c.notes);

    if (fresh > 0) log(`  已加载 ${seen.size} 条`);
    if (seen.size >= want) break;
    // **不能**用接口的 has_more 判结束：我们拦到的那一页往往是列表的**尾页**
    // （首屏走 SSR，页面直接从后面几页开始发请求），尾页的 has_more 天然是 false。
    // 按它停会停在 32 条上（踩过）。只认「连续几轮滚不出新东西」。

    // 逐屏往下滚，而不是一步跳到底：主页是虚拟列表，`scrollTo(bottom)` 只会
    // 让页面渲染末尾那一屏，中间那些卡片永远不会进 DOM（也就永远采不到）。
    // 每轮前进约 1.2 屏，滚到底后再多等几轮 —— 它加载得慢。
    await cdp.eval(`(() => {
      const step = Math.round(window.innerHeight * 1.2);
      window.scrollBy(0, step);
      if (window.scrollY + window.innerHeight >= document.body.scrollHeight - 4) {
        const box = [...document.querySelectorAll('div')].find((d) => {
          const st = getComputedStyle(d);
          return /(auto|scroll)/.test(st.overflowY) && d.scrollHeight > d.clientHeight + 200;
        });
        if (box) box.scrollTop = box.scrollHeight;
      }
      return 1;
    })()`);
    await sleep(1200);
    idle = fresh === 0 ? idle + 1 : 0;
    if (idle >= 15) { log('  连续 15 轮滚不出新数据（到底了）'); break; }
  }

  // 不排序就直接用：主页是「新→旧」渲染的，插入顺序本来就是最新在前。
  // （DOM 卡片没有发布时间，先按时间排会把它们全甩到后面去。）
  let notes = [...seen.values()];
  if (opts.count) notes = notes.slice(0, opts.count);
  if (notes.length === 0) throw new Error('一条作品都没拿到 —— 账号可能设了隐私，或被风控要求登录');
  log(`列表到手：${notes.length} 条`);

  // 2) 逐条补详情（文案/标签/四项互动数/视频直链都只在这里）
  const detail = new Map();
  if (!opts.noDetail) {
    log(`逐条打开详情页补字段（${notes.length} 条，别关电脑）…`);
    for (let i = 0; i < notes.length; i++) {
      const n = notes[i];
      try {
        const d = await readNote(cdp, n.note_id, n.xsec_token);
        detail.set(n.note_id, d);
      } catch (err) {
        log(`  ${i + 1}/${notes.length} ${n.note_id} 详情失败：${String(err.message).slice(0, 80)}`);
      }
      if ((i + 1) % 10 === 0 || i + 1 === notes.length) log(`  进度 ${i + 1}/${notes.length}`);
      await sleep(opts.delayMs);
    }
    // 详情页给了准确发布时间，这时才排序（DOM 卡片原本没有时间，先排会把它们打乱）
    const t = (n) => detail.get(n.note_id)?.time || n.time || 0;
    notes.sort((a, b) => t(b) - t(a));
  }

  // 3) 拼 13 字段
  const userId = target.id && target.kind === 'author' ? target.id : (notes[0]?.user?.user_id || '');
  const rows = notes.map((n) => {
    const d = detail.get(n.note_id) || {};
    const desc = (d.desc || '').trim();
    // 标签有两个来源：详情页的 tagList（结构化、最准），和正文里的 #话题。
    // 有些笔记 tagList 是空的，但正文里明明带了话题 —— 两边都取，去重后按原顺序。
    const tags = dedupeTags([...(d.tags || []), ...tagsFromText(desc)]);
    // 作品链接默认**不带** `xsec_token`（用户要的是「随时能打开」的干净链接）。
    // 代价是：小红书现在对这类裸链会跳 404 / `error_code=300031`，实测同一篇笔记
    // 同一分钟、同一个浏览器，带 token 能开、不带就开不了。要能点开就加 --with-token。
    const token = opts.withToken && n.xsec_token
      ? `?xsec_token=${encodeURIComponent(n.xsec_token)}&xsec_source=pc_user`
      : '';
    return {
      账号名称: n.user?.nickname || profile.nickname || '',
      账号主页链接: userId ? `https://www.xiaohongshu.com/user/profile/${userId}` : '',
      搜索关键词: '',
      发布日期: beijing(d.time || n.time),
      作品标题: (d.title || n.display_title || '').trim(),
      作品标签: tags.join('、'),
      作品文案: desc,
      点赞: num(d.liked ?? n.interact?.liked_count),
      评论: num(d.comment ?? n.interact?.comment_count),
      收藏: num(d.collected ?? n.interact?.collected_count),
      分享: num(d.share ?? n.interact?.share_count),
      作品链接: `https://www.xiaohongshu.com/explore/${n.note_id}${token}`,
      视频链接: d.video || '',
    };
  });

  cdp.close();
  return {
    input: String(input),
    input_kind: 'author',
    platform: 'xiaohongshu',
    keyword: '',
    account_name: profile.nickname || rows[0]?.['账号名称'] || '',
    account_url: userId ? `https://www.xiaohongshu.com/user/profile/${userId}` : '',
    account_stats: { notes: profile.notes ?? null, fans: profile.fans ?? null, liked: profile.liked ?? null },
    table_name: profile.nickname || rows[0]?.['账号名称'] || '小红书账号',
    collected_at: beijing(Date.now()),
    count: rows.length,
    with_detail: !opts.noDetail,
    with_token: !!opts.withToken,
    fields: FIELDS,
    rows,
  };
}

async function readNote(cdp, noteId, tokenOrUrl) {
  const tok = typeof tokenOrUrl === 'string' && /^https?:/.test(tokenOrUrl)
    ? (tokenOrUrl.match(/xsec_token=([^&]+)/) || [])[1] || ''
    : tokenOrUrl || '';
  const url = `https://www.xiaohongshu.com/explore/${noteId}`
    + (tok ? `?xsec_token=${encodeURIComponent(tok)}&xsec_source=pc_user` : '');
  await goto(cdp, url);
  await waitFor(cdp, `(() => {
    const s = window.__INITIAL_STATE__ || {};
    const unwrap = (v) => (v && v._rawValue) || (v && v._value) || v;
    const m = unwrap(s.note && s.note.noteDetailMap) || {};
    return Object.keys(m).length > 0;
  })()`, 12000, `笔记 ${noteId} 详情`).catch(() => null);
  return JSON.parse((await cdp.eval(READ_NOTE)) || '{}');
}

/** 跳到一个地址，并**保证是一次全新的文档**。
 *
 * 小红书是 SPA：如果当前页已经是同一个 URL，`Page.navigate` 等于什么也没做 ——
 * 页面不重新发请求，注入的 hook 也不会重新执行，于是「等首屏请求」永远等不到。
 * 上一轮跑完停在主页、下一轮再采同一个账号，就正好踩这个坑。先回 about:blank
 * 断掉 SPA 的现状，再进目标页。
 */
async function goto(cdp, url) {
  await cdp.send('Page.navigate', { url: 'about:blank' });
  await sleep(250);
  await cdp.send('Page.navigate', { url });
}

async function waitFor(cdp, expression, timeoutMs, label) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try { if (await cdp.eval(expression)) return true; } catch { /* 页面还在跳 */ }
    await sleep(400);
  }
  // 超时的时候，把「停在哪个页面」一起报出来 —— 小红书的风控页、登录页
  // 都是 200 的正常页面，只报「等超时」等于什么都没说。
  let where = '';
  try { where = await cdp.eval('location.href + " | " + document.title'); } catch { /* 页面都没了 */ }
  throw new Error(`等超时：${label}${where ? `（当前停在 ${String(where).slice(0, 120)}）` : ''}`
    + (/captcha|website-login/.test(String(where)) ? ' —— 撞上安全验证了，先在受控浏览器里过一次验证' : ''));
}

// --------------------------------------------------------------------------- //
// 输出
// --------------------------------------------------------------------------- //
function render(plan) {
  const lines = [
    `输入：${plan.input}`,
    `平台：小红书（${plan.with_detail ? '含详情' : '仅列表'}；链接${plan.with_token ? '带 token' : '不带 token'}）`,
    `账号：${plan.account_name}　笔记数：${plan.account_stats?.notes ?? '未知'}　粉丝：${plan.account_stats?.fans ?? '未知'}`,
    `表格名：${plan.table_name}`,
    `采集时间：${plan.collected_at}`,
    `作品数：${plan.count}`,
    '',
    '| # | 发布日期 | 标题 | 赞 | 评 | 藏 | 转 | 链接 |',
    '|---|---|---|---|---|---|---|---|',
  ];
  plan.rows.forEach((r, i) => {
    const t = (r['作品标题'] || '').replace(/\|/g, '｜').slice(0, 30);
    lines.push(`| ${i + 1} | ${r['发布日期']} | ${t} | ${r['点赞'] ?? ''} | ${r['评论'] ?? ''} `
      + `| ${r['收藏'] ?? ''} | ${r['分享'] ?? ''} | ${r['作品链接'].split('?')[0].replace('https://www.xiaohongshu.com', '')} |`);
  });
  return lines.join('\n');
}

function parseArgs(argv) {
  const out = { count: null, noDetail: false, out: null, delayMs: 500, withToken: false };
  const rest = [];
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a === '--count') out.count = Number(argv[++i]);
    else if (a === '--no-detail') out.noDetail = true;
    else if (a === '--with-token') out.withToken = true;
    else if (a === '--out') out.out = argv[++i];
    else if (a === '--json') out.json = true;
    else if (a === '--delay') out.delayMs = Number(argv[++i]);
    else if (a === '--stop') out.stop = true;
    else if (a === '--status') out.status = true;
    else if (a.startsWith('--')) throw new Error(`未知参数：${a}`);
    else rest.push(a);
  }
  out.input = rest[0];
  return out;
}

async function main() {
  const args = parseArgs(process.argv.slice(2));

  if (args.status) {
    const v = await cdpVersion();
    console.log(v ? `受控 Chrome 在跑：${v.Browser}（端口 ${PORT}）` : '受控 Chrome 没在跑（下次采集会自动起）');
    return 0;
  }
  if (args.stop) {
    if (!(await cdpVersion())) { console.log('受控 Chrome 本来就没跑'); return 0; }
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === 'page');
      if (page) {
        const cdp = new CDP(page.webSocketDebuggerUrl);
        await cdp.open();
        await cdp.send('Browser.close').catch(() => {});
        cdp.close();
      }
    } catch { /* 关不掉就算了 */ }
    await sleep(800);
    console.log((await cdpVersion()) ? '还没关掉，手动 kill 端口 9333 上的 Chrome' : '已关闭受控 Chrome');
    return 0;
  }
  if (!args.input) {
    console.log('用法：node xhs.mjs "<主页链接>" [--count N] [--no-detail] [--with-token] [--out rows.json] [--json]\n'
      + '      node xhs.mjs --status | --stop\n'
      + '  作品链接默认不带 xsec_token（干净、可长期保存，但小红书现在会跳 404）；\n'
      + '  要能直接点开就加 --with-token。');
    return 2;
  }

  const plan = await collect(args.input, {
    count: args.count, noDetail: args.noDetail, delayMs: args.delayMs, withToken: args.withToken,
  });
  const payload = JSON.stringify(plan, null, 2);
  if (args.out) {
    writeFileSync(args.out, payload + '\n', 'utf8');
    log(`已写入 ${args.out}（${plan.count} 条）`);
  }
  console.log(args.json ? payload : render(plan));
  return 0;
}

main()
  .then((code) => process.exit(code ?? 0))
  .catch((err) => { console.error(`采集失败：${err.message}`); process.exit(2); });
