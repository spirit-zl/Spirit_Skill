#!/usr/bin/env node
// login.mjs — 在专用调试 Chrome 里完成一次登录：截微信二维码 → 轮询直到登录成功。
//
// 用法：node login.mjs --url "<要访问的页面>" [--out ~/.workbuddy/data/chrome-profile/qr.png]
//                     [--timeout 240] [--port 9333]
// 退出码 0 = 已登录（或本来就不需要登录），2 = 超时仍未登录。二维码路径打印在 stdout。

import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawnSync } from 'child_process';

const argv = process.argv.slice(2);
const flag = (k, d = '') => { const i = argv.indexOf('--' + k); return i >= 0 ? argv[i + 1] : d; };
const URL0 = flag('url');
const PORT = flag('port', '9333');
const OUT = path.resolve(flag('out', path.join(os.homedir(), '.workbuddy', 'data', 'chrome-profile', 'qr.png')));
const TIMEOUT = Number(flag('timeout', '240')) * 1000;
const SCRIPT_DIR = path.dirname(new URL(import.meta.url).pathname);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));
const log = (...a) => console.error(...a);

const ens = spawnSync(process.execPath, [path.join(SCRIPT_DIR, 'ensure-chrome.mjs'), '--json', '--port', PORT], { encoding: 'utf8' });
if (ens.status !== 0) { console.error(ens.stderr || 'ensure-chrome 失败'); process.exit(1); }
const { ws: WS, mode } = JSON.parse(ens.stdout.trim() || '{}');
log('chrome mode =', mode);

const ws = new WebSocket(WS);
let id = 0; const pending = new Map();
await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('CDP 连接失败')); });
ws.onmessage = (e) => {
  const m = JSON.parse(e.data);
  if (m.id && pending.has(m.id)) { const p = pending.get(m.id); pending.delete(m.id); m.error ? p.rej(new Error(m.error.message)) : p.res(m.result || {}); }
};
const send = (method, params = {}, sid = null, timeout = 60000) => new Promise((res, rej) => {
  const i = ++id; const pl = { id: i, method, params }; if (sid) pl.sessionId = sid;
  const t = setTimeout(() => { pending.delete(i); rej(new Error('timeout ' + method)); }, timeout);
  pending.set(i, { res: (v) => { clearTimeout(t); res(v); }, rej: (e) => { clearTimeout(t); rej(e); } });
  ws.send(JSON.stringify(pl));
});

const { targetId } = await send('Target.createTarget', { url: 'about:blank' });
const { sessionId: sid } = await send('Target.attachToTarget', { targetId, flatten: true });
await send('Page.enable', {}, sid);

const ev = async (expr) => (await send('Runtime.evaluate', { expression: expr, returnByValue: true }, sid)).result?.value;

async function shoot() {
  await send('Emulation.setDeviceMetricsOverride', { width: 1280, height: 900, deviceScaleFactor: 1, mobile: false }, sid);
  const s = await send('Page.captureScreenshot', { format: 'png' }, sid, 30000);
  fs.mkdirSync(path.dirname(OUT), { recursive: true });
  fs.writeFileSync(OUT, Buffer.from(s.data, 'base64'));
  console.log('QR ' + OUT);
}

await send('Page.navigate', { url: URL0 }, sid, 30000);
await sleep(4000);

const isLogin = (href) => /sso\.|yitang\.top\/login/.test(href || '');
const deadline = Date.now() + TIMEOUT;
let lastShot = 0;
while (Date.now() < deadline) {
  const href = await ev('location.href').catch(() => '');
  if (href && !isLogin(href)) {
    console.log('LOGIN_OK ' + href);
    try { await send('Target.detachFromTarget', { sessionId: sid }); } catch {}
    await new Promise(r => { try { ws.onclose = r; ws.close(); } catch { r(); } setTimeout(r, 1000); });
    process.exit(0);
  }
  // 二维码约 5 分钟过期，每 50 秒重刷一次页面并重新截图
  if (Date.now() - lastShot > 50000) {
    if (lastShot) { await send('Page.reload', {}, sid, 20000).catch(() => {}); await sleep(2500); }
    await shoot().catch(() => {});
    lastShot = Date.now();
  }
  await sleep(2000);
}

console.log('LOGIN_TIMEOUT');
process.exit(2);
