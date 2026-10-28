#!/usr/bin/env node
// ensure-chrome.mjs — 保证有一个「可以反复连接」的 CDP 端点。
//
// 背景：Chrome 155 里 chrome://inspect 那个开关是一次性的（客户端一断开端点就废），
// 而命令行 --remote-debugging-port 启动的实例没有这个限制。所以这里维护一个
// 专用 profile 的无头 Chrome（默认端口 9333），登录态只存一次，之后每次抓取都复用它。
//
// 用法：
//   node ensure-chrome.mjs [--port 9333] [--profile <目录>] [--json] [--restart]
// 输出：stdout 为 ws 地址；--json 时输出 {ws,mode,port}

import fs from 'fs';
import os from 'os';
import path from 'path';
import { spawn } from 'child_process';

const argv = process.argv.slice(2);
const flag = (k, d = '') => { const i = argv.indexOf('--' + k); return i >= 0 ? argv[i + 1] : d; };
const PORT = Number(flag('port', '9333'));
const PROFILE = path.resolve(flag('profile', path.join(os.homedir(), '.workbuddy', 'data', 'chrome-profile')));
const JSON_OUT = argv.includes('--json');
const log = (...a) => console.error(...a);
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const CHROME = ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge'].find(p => fs.existsSync(p));

function handshake(wsUrl, timeout = 4000) {
  return new Promise((resolve) => {
    if (!wsUrl) return resolve(false);
    let ws;
    try { ws = new WebSocket(wsUrl); } catch { return resolve(false); }
    const t = setTimeout(() => { try { ws.close(); } catch {} resolve(false); }, timeout);
    ws.onopen = () => { clearTimeout(t); try { ws.close(); } catch {} resolve(true); };
    ws.onerror = () => { clearTimeout(t); resolve(false); };
  });
}

async function wsOfPort(port) {
  try {
    const r = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(2500) });
    if (!r.ok) return null;
    const j = await r.json();
    return j.webSocketDebuggerUrl || null;
  } catch { return null; }
}

function userEndpoint() {
  const f = path.join(os.homedir(), 'Library/Application Support/Google/Chrome/DevToolsActivePort');
  if (!fs.existsSync(f)) return null;
  const [p, w] = fs.readFileSync(f, 'utf8').trim().split('\n');
  return p && w ? `ws://127.0.0.1:${p}${w}` : null;
}

async function main() {
  // 1) 专用 profile 已经在跑（优先级最高：这个端点可以反复连，不会像勾选式端点那样一次性废掉）
  const live = await wsOfPort(PORT);
  if (live && await handshake(live, 3000)) return { ws: live, mode: 'profile', port: PORT };

  // 2) 拉起专用无头 Chrome
  if (CHROME) {
    fs.mkdirSync(PROFILE, { recursive: true });
    for (const f of ['SingletonLock', 'SingletonCookie', 'SingletonSocket']) {
      try { fs.rmSync(path.join(PROFILE, f)); } catch { /* 没有就算了 */ }
    }
    log(`拉起专用调试 Chrome（端口 ${PORT}）…`);
    const proc = spawn(CHROME, [
      '--headless=new', '--no-sandbox', '--disable-gpu', '--hide-scrollbars',
      '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
      `--remote-debugging-port=${PORT}`,
      `--user-data-dir=${PROFILE}`,
      '--no-first-run', '--no-default-browser-check',
      '--window-size=1280,1000',
      'about:blank',
    ], { stdio: 'ignore', detached: true });
    proc.unref();

    const deadline = Date.now() + 25000;
    while (Date.now() < deadline) {
      await sleep(400);
      const ws = await wsOfPort(PORT);
      if (ws && await handshake(ws, 2500)) return { ws, mode: 'launched', port: PORT };
    }
    log('专用 Chrome 启动失败，退回用户 Chrome 端点');
  }

  // 3) 兜底：用户自己 Chrome 的「勾选式」端点（一次性的，用完可能要重勾）
  const u = userEndpoint();
  if (u && await handshake(u, 3500)) return { ws: u, mode: 'user', port: Number(/127\.0\.0\.1:(\d+)/.exec(u)[1]) };

  throw new Error('没有可用的 Chrome 调试端点');
}

try {
  const res = await main();
  if (JSON_OUT) console.log(JSON.stringify(res));
  else console.log(res.ws);
  process.exit(0);
} catch (e) {
  console.error('ERROR: ' + e.message);
  process.exit(1);
}
