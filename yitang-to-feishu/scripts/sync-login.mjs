#!/usr/bin/env node
// sync-login.mjs — 把用户 Chrome 里 yitang 的登录态（cookie + localStorage token）
// 搬到「专用调试 Chrome」，抓取时就不用扫码、也不碰用户自己的浏览器。
//
// 用法：node sync-login.mjs [--port 9333] [--domain yitang.top]
// 退出码 0 = 同步成功；1 = 用户 Chrome 不可用 / 没找到登录态。

import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';

const argv = process.argv.slice(2);
const flag = (k, d = '') => { const i = argv.indexOf('--' + k); return i >= 0 ? argv[i + 1] : d; };
const PORT = flag('port', '9333');
const DOMAIN = flag('domain', 'yitang.top');
const HOME = os.homedir();
const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const ORIGIN = `https://${DOMAIN}`;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

class CDP {
  constructor(u) { this.u = u; this.id = 0; this.p = new Map(); }
  async connect() {
    this.ws = new WebSocket(this.u);
    await new Promise((res, rej) => { this.ws.onopen = res; this.ws.onerror = () => rej(new Error('CDP 连接失败')); });
    this.ws.onmessage = (e) => {
      const m = JSON.parse(e.data);
      if (m.id && this.p.has(m.id)) { const x = this.p.get(m.id); this.p.delete(m.id); m.error ? x.rej(new Error(m.error.message)) : x.res(m.result || {}); }
    };
  }
  send(method, params = {}, sid = null, timeout = 30000) {
    return new Promise((res, rej) => {
      const i = ++this.id; const pl = { id: i, method, params }; if (sid) pl.sessionId = sid;
      const t = setTimeout(() => { this.p.delete(i); rej(new Error('timeout ' + method)); }, timeout);
      this.p.set(i, { res: (v) => { clearTimeout(t); res(v); }, rej: (e) => { clearTimeout(t); rej(e); } });
      this.ws.send(JSON.stringify(pl));
    });
  }
  close() { try { this.ws.close(); } catch {} }
}

const ev = async (cdp, sid, expr) => (await cdp.send('Runtime.evaluate', { expression: expr, returnByValue: true }, sid)).result?.value;

// ─── 用户 Chrome：取 cookie + localStorage ──────────────────────────────────
const portFile = path.join(HOME, 'Library/Application Support/Google/Chrome/DevToolsActivePort');
if (!fs.existsSync(portFile)) { console.error('用户 Chrome 没有调试端点，跳过同步'); process.exit(1); }

let user;
try {
  const [p, w] = fs.readFileSync(portFile, 'utf8').trim().split('\n');
  user = new CDP(`ws://127.0.0.1:${p}${w}`);
  await user.connect();
} catch (e) { console.error('连不上用户 Chrome：' + e.message); process.exit(1); }

const { cookies } = await user.send('Storage.getCookies');
const mine = cookies.filter(c => c.domain.replace(/^\./, '').endsWith(DOMAIN) || c.domain.replace(/^\./, '').endsWith('.' + DOMAIN));
if (!mine.length) { console.error(`用户 Chrome 里没有 ${DOMAIN} 的 cookie`); process.exit(1); }

// 会话 cookie 改成 30 天，否则专用 Chrome 一重启就掉登录
const MONTH = 30 * 24 * 3600;
const now = Math.floor(Date.now() / 1000);
const fixed = mine.map(c => ({ ...c, expires: c.expires && c.expires > 0 ? c.expires : now + MONTH }));

// localStorage：优先复用已打开的 yitang 标签页，没有就后台开一个再关掉
let storage = null, created = null;
const { targetInfos } = await user.send('Target.getTargets');
let page = targetInfos.find(t => t.type === 'page' && t.url.includes(DOMAIN));
if (!page) {
  const r = await user.send('Target.createTarget', { url: ORIGIN, background: true });
  created = r.targetId;
  page = { targetId: created };
  await sleep(3000);
}
if (page) {
  try {
    const { sessionId: usid } = await user.send('Target.attachToTarget', { targetId: page.targetId, flatten: true });
    await user.send('Runtime.enable', {}, usid).catch(() => {});
    storage = await ev(user, usid, `JSON.stringify(Object.fromEntries(Object.entries(localStorage)))`);
    await user.send('Target.detachFromTarget', { sessionId: usid }).catch(() => {});
  } catch (e) { console.error('读 localStorage 失败：' + e.message); }
}
if (created) await user.send('Target.closeTarget', { targetId: created }).catch(() => {});
user.close();

// ─── 专用 Chrome：写入 ─────────────────────────────────────────────────────
const ens = spawnSync(process.execPath, [path.join(SCRIPT_DIR, 'ensure-chrome.mjs'), '--json', '--port', PORT], { encoding: 'utf8' });
if (ens.status !== 0) { console.error(ens.stderr || 'ensure-chrome 失败'); process.exit(1); }
const { ws } = JSON.parse(ens.stdout.trim());
const auto = new CDP(ws);
await auto.connect();
await auto.send('Storage.setCookies', { cookies: fixed });

// 写入 localStorage 需要一个同源页面
const { targetId } = await auto.send('Target.createTarget', { url: 'about:blank' });
const { sessionId: sid } = await auto.send('Target.attachToTarget', { targetId, flatten: true });
await auto.send('Page.enable', {}, sid);
await auto.send('Emulation.setUserAgentOverride', {
  userAgent: 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/155.0.0.0 Safari/537.36',
  acceptLanguage: 'zh-CN,zh;q=0.9',
}, sid);
await auto.send('Page.navigate', { url: ORIGIN + '/', }, sid);
await sleep(3000);

let keys = 0;
if (storage && storage !== '{}') {
  const script = `(() => { const o = ${storage}; let n = 0; for (const [k, v] of Object.entries(o)) { try { localStorage.setItem(k, v); n++; } catch {} } return n; })()`;
  keys = await ev(auto, sid, script).catch(() => 0);
}
const token = await ev(auto, sid, `localStorage.getItem('#|g.TOKEN') || ''`).catch(() => '');

await auto.send('Target.closeTarget', { targetId }).catch(() => {});
auto.close();
console.log(`已同步 ${fixed.length} 条 cookie、${keys} 条 localStorage；token ${token ? '已就位' : '缺失'}`);
await sleep(300);
process.exit(token ? 0 : 1);
