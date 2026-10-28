#!/usr/bin/env node
// capture.mjs — turn a web page into (markdown + local images + full-page PNG) via Chrome CDP.
//
// Modes:
//   --check                     环境自检
//   --list                      列出当前浏览器可接管的标签页
//   --url <URL>                 自己开标签页抓取（默认，必要时自动拉起无头 Chrome）
//   --tab <targetId>            接管用户已打开的标签页（需要登录态时用这个）
//
// Options:
//   --out <dir>        输出目录（默认 ./webpage-captures/<slug>-<时间戳>）
//   --width <n>        视口宽度，默认 1200
//   --dpr <n>          设备像素比，默认 1
//   --wait <ms>        加载等待上限，默认 20000
//   --port <n>         指定 Chrome 调试端口
//   --chrome <path>    指定 Chrome 可执行文件
//   --own-chrome       强制另起一个无头 Chrome（不碰用户浏览器）
//   --no-shot          不生成整页长截图
//   --json             仅输出机器可读的 JSON 摘要

import fs from 'fs';
import net from 'net';
import os from 'os';
import path from 'path';
import { spawn, execFileSync } from 'child_process';

// ─── args ───────────────────────────────────────────────────────────────────

const argv = process.argv.slice(2);
const flags = {};
const positional = [];
const bools = new Set(['check', 'list', 'no-shot', 'own-chrome', 'json', 'help', 'probe', 'toc', 'shot-each', 'no-virtual']);

for (let i = 0; i < argv.length; i++) {
  const a = argv[i];
  if (a.startsWith('--')) {
    const k = a.slice(2);
    if (bools.has(k)) flags[k] = true;
    else if (i + 1 < argv.length) flags[k] = argv[++i];
  } else positional.push(a);
}

const WIDTH = parseInt(flags.width || '1200', 10);
const DPR = parseInt(flags.dpr || '1', 10);
const WAIT = parseInt(flags.wait || '20000', 10);
const IMG_LIMIT = 12 * 1024 * 1024;
const TILE_THRESHOLD = 16000;
const TILE_H = 8000;
const GENERIC_ALT = /^(图片|图像|配图|插图|图|image|img|photo|picture|pic|screenshot)$/i;

const log = (...a) => { if (!flags.json) console.error(...a); };

function die(msg, code = 1) {
  console.error('ERROR: ' + msg);
  process.exit(code);
}

// ─── Chrome discovery ───────────────────────────────────────────────────────

function devtoolsPortFiles() {
  const home = os.homedir();
  if (os.platform() === 'darwin') {
    return [
      path.join(home, 'Library/Application Support/Google/Chrome/DevToolsActivePort'),
      path.join(home, 'Library/Application Support/Google/Chrome Canary/DevToolsActivePort'),
      path.join(home, 'Library/Application Support/Chromium/DevToolsActivePort'),
      path.join(home, 'Library/Application Support/Microsoft Edge/DevToolsActivePort'),
    ];
  }
  if (os.platform() === 'linux') {
    return [
      path.join(home, '.config/google-chrome/DevToolsActivePort'),
      path.join(home, '.config/chromium/DevToolsActivePort'),
    ];
  }
  const local = process.env.LOCALAPPDATA || '';
  return [
    path.join(local, 'Google/Chrome/User Data/DevToolsActivePort'),
    path.join(local, 'Chromium/User Data/DevToolsActivePort'),
  ];
}

function portOpen(port) {
  return new Promise((resolve) => {
    const s = net.createConnection(port, '127.0.0.1');
    const t = setTimeout(() => { s.destroy(); resolve(false); }, 1200);
    s.once('connect', () => { clearTimeout(t); s.destroy(); resolve(true); });
    s.once('error', () => { clearTimeout(t); s.destroy(); resolve(false); });
  });
}

async function probeWs(wsUrl, timeout = 3000) {
  return new Promise((resolve) => {
    let settled = false;
    let ws = null;
    let timer = null;

    const finish = (ok) => {
      if (settled) return;
      settled = true;
      if (timer) clearTimeout(timer);
      try {
        if (ws) {
          ws.onopen = null;
          ws.onerror = null;
          ws.onclose = null;
          if (ws.readyState === 1) ws.close();
        }
      } catch { /* ignore */ }
      resolve(ok);
    };

    timer = setTimeout(() => finish(false), timeout);
    try {
      ws = new WebSocket(wsUrl);
      ws.onopen = () => finish(true);
      ws.onerror = () => finish(false);
    } catch { finish(false); }
  });
}

async function browserWsUrl(port) {
  // Chrome 155 起，通过 chrome://inspect 打开的调试端口不再提供 /json/version（返回 404），
  // 只能依赖 DevToolsActivePort 里写好的 ws 路径。
  try {
    const r = await fetch(`http://127.0.0.1:${port}/json/version`, { signal: AbortSignal.timeout(3000) });
    const j = await r.json();
    if (j.webSocketDebuggerUrl) return j.webSocketDebuggerUrl;
  } catch { /* fall through */ }
  return null;
}

async function discoverChrome() {
  if (flags.port) {
    const p = parseInt(flags.port, 10);
    if (!(await portOpen(p))) return null;
    for (const u of [`ws://127.0.0.1:${p}/devtools/browser`, await browserWsUrl(p)]) {
      if (u && (await probeWs(u))) return { port: p, wsUrl: u };
    }
    return null;
  }

  // DevToolsActivePort 第二行就是带 UUID 的路径，是最可靠的来源
  for (const f of devtoolsPortFiles()) {
    try {
      const lines = fs.readFileSync(f, 'utf-8').trim().split('\n');
      const port = parseInt(lines[0], 10);
      if (!(port > 0 && port < 65536)) continue;
      const wsPath = (lines[1] || '').trim();
      const candidates = [];
      if (wsPath) candidates.push(`ws://127.0.0.1:${port}${wsPath}`);
      candidates.push(`ws://127.0.0.1:${port}/devtools/browser`);
      for (const u of candidates) {
        if (await probeWs(u)) return { port, wsUrl: u, source: f };
      }
      const viaHttp = await browserWsUrl(port);
      if (viaHttp && (await probeWs(viaHttp))) return { port, wsUrl: viaHttp, source: f };
    } catch { /* next */ }
  }

  for (const p of [9222, 9333, 9334]) {
    if (!(await portOpen(p))) continue;
    const viaHttp = await browserWsUrl(p);
    if (viaHttp && (await probeWs(viaHttp))) return { port: p, wsUrl: viaHttp };
  }
  return null;
}

function chromeBinary() {
  if (flags.chrome) return flags.chrome;
  const candidates = os.platform() === 'darwin'
    ? ['/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
       '/Applications/Chromium.app/Contents/MacOS/Chromium',
       '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge']
    : os.platform() === 'win32'
      ? [path.join(process.env.PROGRAMFILES || 'C:/Program Files', 'Google/Chrome/Application/chrome.exe')]
      : ['/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser'];
  for (const c of candidates) if (fs.existsSync(c)) return c;
  return null;
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, '127.0.0.1', () => {
      const p = srv.address().port;
      srv.close(() => resolve(p));
    });
    srv.on('error', reject);
  });
}

async function launchChrome() {
  const bin = chromeBinary();
  if (!bin) die('找不到 Chrome，可用 --chrome <path> 指定');
  const port = await freePort();
  const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'w2f-chrome-'));
  const args = [
    '--headless=new',
    '--no-sandbox',            // 嵌套沙箱环境下 Chrome 自带 sandbox 会初始化失败
    '--disable-dev-shm-usage',
    '--disable-gpu',
    '--hide-scrollbars',
    '--no-first-run',
    '--no-default-browser-check',
    '--disable-background-timer-throttling',
    '--disable-renderer-backgrounding',
    '--remote-allow-origins=*',
    `--remote-debugging-port=${port}`,
    `--user-data-dir=${profile}`,
    'about:blank',
  ];
  log(`启动无头 Chrome（端口 ${port}）…`);
  const proc = spawn(bin, args, { stdio: 'ignore', detached: false });
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    if (await portOpen(port)) {
      const wsUrl = await browserWsUrl(port);
      if (wsUrl) {
        return { port, wsUrl, proc, profile };
      }
    }
    await new Promise(r => setTimeout(r, 300));
  }
  try { proc.kill('SIGKILL'); } catch {}
  die('无头 Chrome 启动超时');
}

// ─── CDP client ─────────────────────────────────────────────────────────────

class CDP {
  constructor(wsUrl) {
    this.wsUrl = wsUrl;
    this.id = 0;
    this.pending = new Map();
  }

  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(this.wsUrl);
      this.ws = ws;
      const timer = setTimeout(() => reject(new Error('CDP 连接超时')), 15000);
      ws.onopen = () => { clearTimeout(timer); resolve(); };
      ws.onerror = () => { clearTimeout(timer); reject(new Error('CDP 连接失败')); };
      ws.onmessage = (ev) => {
        let msg;
        try { msg = JSON.parse(ev.data); } catch { return; }
        if (msg.id && this.pending.has(msg.id)) {
          const p = this.pending.get(msg.id);
          this.pending.delete(msg.id);
          clearTimeout(p.timer);
          if (msg.error) p.reject(new Error(msg.error.message || JSON.stringify(msg.error)));
          else p.resolve(msg.result || {});
        }
      };
      ws.onclose = () => {
        for (const [, p] of this.pending) p.reject(new Error('CDP 连接已关闭'));
        this.pending.clear();
      };
    });
  }

  send(method, params = {}, sessionId = null, timeout = 60000) {
    return new Promise((resolve, reject) => {
      const id = ++this.id;
      const payload = { id, method, params };
      if (sessionId) payload.sessionId = sessionId;
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP 超时 ${timeout}ms: ${method}`));
      }, timeout);
      this.pending.set(id, { resolve, reject, timer });
      this.ws.send(JSON.stringify(payload));
    });
  }

  close() { try { this.ws.close(); } catch {} }
}

const sleep = (ms) => new Promise(r => setTimeout(r, ms));

// ─── Page helpers ───────────────────────────────────────────────────────────

async function evaluate(cdp, sid, expression, timeout = 60000, awaitPromise = true) {
  const r = await cdp.send('Runtime.evaluate', {
    expression, returnByValue: true, awaitPromise,
  }, sid, timeout);
  if (r.exceptionDetails) {
    throw new Error('页面脚本异常: ' + (r.exceptionDetails.exception?.description || r.exceptionDetails.text));
  }
  return r.result ? r.result.value : undefined;
}

async function waitForLoad(cdp, sid, timeoutMs) {
  const t0 = Date.now();
  while (Date.now() - t0 < timeoutMs) {
    try {
      const state = await evaluate(cdp, sid, 'document.readyState', 5000, false);
      if (state === 'complete') break;
    } catch { /* retry */ }
    await sleep(400);
  }
  // DOM 元素数量稳定 → SPA 渲染完成
  let last = 0, stable = 0;
  const deadline = Date.now() + Math.min(12000, Math.max(0, timeoutMs - (Date.now() - t0)));
  while (Date.now() < deadline) {
    try {
      const n = await evaluate(cdp, sid, 'document.querySelectorAll("*").length', 5000, false);
      if (n === last && n > 0) {
        if (++stable >= 3) return;
      } else { stable = 0; last = n; }
    } catch { /* retry */ }
    await sleep(500);
  }
}

async function expandScrollContainers(cdp, sid) {
  const res = await evaluate(cdp, sid, `(async () => {
    const containers = [];
    // 先做廉价判断（尺寸），只对可能是滚动容器的块级元素求计算样式
    for (const el of document.querySelectorAll('div,section,article,main,aside,ul,ol')) {
      if (el.clientHeight < 100) continue;
      if (el.scrollHeight <= el.clientHeight + 50) continue;
      const cs = getComputedStyle(el);
      if (cs.overflowY === 'auto' || cs.overflowY === 'scroll') containers.push(el);
    }
    if (!containers.length) return { found: 0, expanded: 0 };
    for (const el of containers) {
      for (let y = 0; y < el.scrollHeight; y += 800) {
        el.scrollTop = y;
        await new Promise(r => setTimeout(r, 120));
      }
      el.scrollTop = 0;
    }
    window.__wbRestore = window.__wbRestore || [];
    const remember = (el) => {
      if (el.__wbSaved) return;
      el.__wbSaved = { style: el.getAttribute('style') };
      window.__wbRestore.push(el);
    };
    const expandEl = (el) => {
      remember(el);
      el.style.setProperty('overflow', 'visible', 'important');
      el.style.setProperty('overflow-y', 'visible', 'important');
      el.style.setProperty('max-height', 'none', 'important');
      if (parseFloat(getComputedStyle(el).height) < el.scrollHeight - 10) {
        el.style.setProperty('height', 'auto', 'important');
      }
    };
    let expanded = 0;
    for (const el of containers) {
      if (el.scrollHeight > el.clientHeight + 50) {
        expandEl(el);
        let parent = el.parentElement;
        for (let i = 0; i < 10 && parent && parent !== document.body; i++) {
          const ps = getComputedStyle(parent);
          if (ps.overflow !== 'visible' || ps.overflowY !== 'visible' ||
              parseFloat(ps.height) < parent.scrollHeight - 10) {
            expandEl(parent);
          }
          parent = parent.parentElement;
        }
        expanded++;
      }
    }
    return { found: containers.length, expanded };
  })()`, 90000);

  if (res && res.expanded) log(`展开内部滚动容器 ${res.expanded} 个`);
  await sleep(800);
}

async function triggerLazy(cdp, sid) {
  // 飞书/语雀这类文档正文是懒渲染的：不滚到底，后面的块根本不存在于 DOM。
  // 多滚几轮，直到正文字数不再明显增长。
  let lastLen = 0;
  for (let pass = 0; pass < 4; pass++) {
    let info = null;
    try {
      info = await evaluate(cdp, sid, `(async () => {
        const step = Math.max(600, Math.round(window.innerHeight * 0.8));
        const deadline = Date.now() + 45000;
        let y = 0;
        while (y < document.documentElement.scrollHeight && Date.now() < deadline) {
          window.scrollTo(0, y);
          await new Promise(r => setTimeout(r, 90));
          y += step;
        }
        window.scrollTo(0, document.documentElement.scrollHeight);
        await new Promise(r => setTimeout(r, 900));
        const len = document.body.innerText.length;
        const height = document.documentElement.scrollHeight;
        window.scrollTo(0, 0);
        await new Promise(r => setTimeout(r, 300));
        return { len, height };
      })()`, 60000);
    } catch (e) {
      log('滚动触发懒加载跳过：' + e.message);
      break;
    }
    if (!info) break;
    log(`第 ${pass + 1} 轮滚动：正文 ${info.len} 字，页面高 ${info.height}px`);
    if (pass > 0 && info.len <= lastLen * 1.02) break;
    lastLen = info.len;
  }

  try {
    const info = await evaluate(cdp, sid, `(async () => {
      const deadline = Date.now() + 12000;
      while (Date.now() < deadline) {
        const pending = Array.from(document.querySelectorAll('img'))
          .filter(i => !i.complete && i.src && !i.src.startsWith('data:'));
        if (!pending.length) return { pending: 0 };
        await new Promise(r => setTimeout(r, 400));
      }
      return { pending: document.querySelectorAll('img:not([complete])').length };
    })()`, 18000);
    if (info && info.pending) log(`警告：仍有 ${info.pending} 张图片未加载完成`);
  } catch { /* ignore */ }
}

async function restorePage(cdp, sid) {
  try {
    await evaluate(cdp, sid, `(() => {
      if (window.__wbRestore) {
        for (const el of window.__wbRestore) {
          if (el.__wbSaved && el.__wbSaved.style === null) el.removeAttribute('style');
          else if (el.__wbSaved) el.setAttribute('style', el.__wbSaved.style);
        }
        window.__wbRestore = null;
      }
      // 清掉抓取时打的标记，别把用户页面留脏
      for (const attr of ['data-wb-skip', 'data-wb-src', 'data-wb-size', 'data-wb-fs', 'data-wb-fw']) {
        for (const el of document.querySelectorAll('[' + attr + ']')) el.removeAttribute(attr);
      }
      return true;
    })()`, 15000).catch(() => {});
    await cdp.send('Emulation.clearDeviceMetricsOverride', {}, sid).catch(() => {});
  } catch { /* ignore */ }
}

// ─── Images ─────────────────────────────────────────────────────────────────

function slugify(s, max = 40) {
  const t = String(s || '').replace(/[\\/:*?"<>|\r\n\t]/g, ' ').trim()
    .replace(/\s+/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '');
  return (t || 'img').slice(0, max);
}

function extFromType(type, url) {
  const map = { 'image/jpeg': '.jpg', 'image/jpg': '.jpg', 'image/png': '.png', 'image/gif': '.gif', 'image/webp': '.webp', 'image/svg+xml': '.svg', 'image/avif': '.avif', 'image/bmp': '.bmp' };
  if (type && map[type.toLowerCase()]) return map[type.toLowerCase()];
  const m = /\.(png|jpe?g|gif|webp|svg|avif|bmp)(?:[?#]|$)/i.exec(url || '');
  return m ? '.' + m[1].toLowerCase().replace('jpeg', 'jpg') : '.png';
}

const FETCH_FN = (url, limit) => `(async () => {
  try {
    const r = await fetch(${JSON.stringify(url)}, { credentials: 'include', referrer: location.href });
    if (!r.ok) return { ok: false, status: r.status };
    const b = await r.blob();
    if (b.size > ${limit}) return { ok: false, reason: 'too-big', size: b.size };
    const bytes = new Uint8Array(await b.arrayBuffer());
    let bin = '';
    for (let i = 0; i < bytes.length; i += 0x8000) {
      bin += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
    }
    return { ok: true, b64: btoa(bin), type: b.type, size: b.size };
  } catch (e) {
    return { ok: false, error: String(e) };
  }
})()`;

async function nodeFetchImage(url, referer) {
  const headers = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36',
    'Accept': 'image/avif,image/webp,image/*,*/*;q=0.8',
  };
  if (referer) headers.Referer = referer;
  const r = await fetch(url, { headers });
  if (!r.ok) throw new Error('HTTP ' + r.status);
  const buf = Buffer.from(await r.arrayBuffer());
  return { buf, type: r.headers.get('content-type') || '' };
}

async function downloadImage(cdp, sid, img, dir, referer, prefix = '') {
  let res = await evaluate(cdp, sid, FETCH_FN(img.src, IMG_LIMIT), 90000).catch(() => null);
  if (!res || !res.ok) {
    // 站点限流时第一次常失败，隔一会儿重试一次
    await sleep(500);
    res = await evaluate(cdp, sid, FETCH_FN(img.src, IMG_LIMIT), 90000).catch(() => null);
  }
  let buf = null, type = '';
  if (res && res.ok) {
    buf = Buffer.from(res.b64, 'base64');
    type = res.type || '';
  } else {
    try {
      const alt = await nodeFetchImage(img.src, referer);
      buf = alt.buf;
      type = alt.type;
    } catch (e) {
      return { ok: false, reason: (res && (res.status || res.reason || res.error)) || e.message };
    }
  }
  const ext = extFromType(type, img.src);
  const alt = (img.alt || '').trim();
  const meaningful = alt && !GENERIC_ALT.test(alt) ? slugify(alt, 30) : '';
  const rawStem = path.basename(new URL(img.src).pathname).replace(/\.[a-z0-9]+$/i, '');
  const urlStem = (/^\d+$/.test(rawStem) || rawStem.length < 3 || /^[0-9a-f]{12,}$/i.test(rawStem)) ? '' : rawStem;
  const stem = meaningful || (urlStem ? slugify(urlStem, 24) : '');
  const name = prefix + String(img.idx).padStart(3, '0') + (stem ? '-' + stem : '') + ext;
  const file = path.join(dir, name);
  fs.writeFileSync(file, buf);
  return { ok: true, file: name, bytes: buf.length, type };
}

// ─── Screenshot ─────────────────────────────────────────────────────────────

function pythonWithPIL() {
  const candidates = [
    process.env.PYTHON,
    '/Users/spirit/.workbuddy/binaries/python/envs/default/bin/python',
    'python3',
  ].filter(Boolean);
  for (const p of candidates) {
    try {
      execFileSync(p, ['-c', 'import PIL'], { stdio: 'ignore', timeout: 8000 });
      return p;
    } catch { /* next */ }
  }
  return null;
}

async function captureShot(cdp, sid, outDir, width, dpr, namePrefix = 'fullpage') {
  fs.mkdirSync(outDir, { recursive: true });
  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width, height: 800, deviceScaleFactor: dpr, mobile: false,
  }, sid);
  await sleep(600);

  const m = await cdp.send('Page.getLayoutMetrics', {}, sid);
  const contentH = Math.ceil(m.cssContentSize.height);
  log(`页面尺寸：${width} x ${contentH}`);

  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width, height: Math.min(contentH, 8000), deviceScaleFactor: dpr, mobile: false,
  }, sid);
  await sleep(1200);

  const m2 = await cdp.send('Page.getLayoutMetrics', {}, sid);
  const finalH = Math.ceil(m2.cssContentSize.height);

  // 超长文档截成 PNG 会几十上百 MB，飞书附件扛不住；超过 2 万像素改用 JPEG
  const useJpeg = finalH > 20000;
  const outFile = path.join(outDir, useJpeg ? `${namePrefix}.jpg` : `${namePrefix}.png`);
  const fmtArgs = useJpeg ? { format: 'jpeg', quality: 82 } : { format: 'png' };

  if (finalH <= TILE_THRESHOLD) {
    const shot = await cdp.send('Page.captureScreenshot', {
      ...fmtArgs, captureBeyondViewport: true,
      clip: { x: 0, y: 0, width, height: finalH, scale: 1 },
    }, sid, 120000);
    fs.writeFileSync(outFile, Buffer.from(shot.data, 'base64'));
  } else {
    const tiles = [];
    for (let y = 0; y < finalH; y += TILE_H) {
      const h = Math.min(TILE_H, finalH - y);
      const shot = await cdp.send('Page.captureScreenshot', {
        ...fmtArgs, captureBeyondViewport: true,
        clip: { x: 0, y, width, height: h, scale: 1 },
      }, sid, 120000);
      const tf = path.join(outDir, `_tile${tiles.length}.${useJpeg ? 'jpg' : 'png'}`);
      fs.writeFileSync(tf, Buffer.from(shot.data, 'base64'));
      tiles.push({ path: tf, y });
    }
    const py = pythonWithPIL();
    if (!py) {
      log('未找到带 Pillow 的 Python，保留分片文件');
      return { height: finalH, file: null, tiled: true, stitched: false };
    }
    const script = [
      'from PIL import Image',
      'Image.MAX_IMAGE_PIXELS = None',
      `tiles = [${tiles.map(t => `(${JSON.stringify(t.path)}, ${t.y})`).join(', ')}]`,
      `full_w, full_h = ${width * dpr}, ${finalH * dpr}`,
      // JPEG 单边上限 65535px，超过就整体按比例缩小
      'scale = min(1.0, 65000.0 / full_h)',
      'W, H = int(full_w * scale), int(full_h * scale)',
      'out = Image.new("RGB", (W, H), "white")',
      'for p, y in tiles:',
      '    im = Image.open(p)',
      '    if scale < 1.0:',
      '        im = im.resize((W, max(1, int(im.size[1] * scale))), Image.LANCZOS)',
      `    out.paste(im, (0, int(y * ${dpr} * scale)))`,
      '    im.close()',
      '# 文档末尾常有一大片空白，按连续空白行裁掉',
      'try:',
      '    import numpy as np',
      '    arr = np.asarray(out.convert("L"))',
      '    rows = (arr < 245).sum(axis=1)',
      '    nz = np.where(rows > 8)[0]',
      '    if len(nz) > 0:',
      '        last = int(nz[-1]) + 40',
      '        if last < H - 200:',
      '            out = out.crop((0, 0, W, last))',
      'except Exception:',
      '    pass',
      `out.save(${JSON.stringify(outFile)}${useJpeg ? ', quality=85' : ''})`,
    ].join('\n');
    const sp = path.join(outDir, '_stitch.py');
    fs.writeFileSync(sp, script);
    execFileSync(py, [sp], { stdio: 'ignore', timeout: 180000 });
    fs.unlinkSync(sp);
    for (const t of tiles) { try { fs.unlinkSync(t.path); } catch {} }
  }

  const st = fs.statSync(outFile);
  return { height: finalH, file: path.basename(outFile), bytes: st.size, tiled: finalH > TILE_THRESHOLD, stitched: true };
}

// ─── Main ───────────────────────────────────────────────────────────────────

let ACTIVE = null;

function gracefulExit(code) {
  try {
    if (ACTIVE) {
      const { cdp, launched: proc, targetId, created } = ACTIVE;
      if (created) cdp.send('Target.closeTarget', { targetId }).catch(() => {});
      try { cdp.close(); } catch {}
      if (proc) { try { proc.proc.kill('SIGKILL'); } catch {} }
    }
  } catch {}
  // 给 WebSocket close 帧一点时间发出去，否则 Chrome 的调试端口会卡住
  setTimeout(() => process.exit(code), 150);
}

process.on('SIGTERM', () => gracefulExit(143));
process.on('SIGINT', () => gracefulExit(130));

// ─── 多页（侧边栏目录）抓取 ─────────────────────────────────────────────────

const TOC_SELECTORS = ['.el-tree-node__content', 'aside a[href]', 'nav a[href]', '.menu a[href]', '.sidebar a[href]', '.catalog a[href]'];

async function attachFresh(cdp, targetId) {
  const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  await cdp.send('Page.enable', {}, sessionId).catch(() => {});
  await cdp.send('Runtime.enable', {}, sessionId).catch(() => {});
  return sessionId;
}

async function expandToc(cdp, sid) {
  await evaluate(cdp, sid, `(() => {
    let n = 0;
    for (const icon of document.querySelectorAll('.el-tree-node__expand-icon:not(.expanded):not(.is-leaf)')) {
      try { icon.click(); n++; } catch (e) {}
    }
    return n;
  })()`, 20000, false).catch(() => 0);
  await sleep(1500);
}

async function readToc(cdp, sid) {
  const r = await evaluate(cdp, sid, `(() => {
    const sels = ${JSON.stringify(TOC_SELECTORS)};
    for (const sel of sels) {
      const list = Array.from(document.querySelectorAll(sel));
      const items = [];
      list.forEach((el, i) => {
        const text = (el.innerText || '').trim().replace(/\\s+/g, ' ');
        if (!text || text.length > 60) return;
        if (items.some(x => x.text === text)) return;
        items.push({ i, text });
      });
      if (items.length >= 3) return { selector: sel, items };
    }
    return { selector: null, items: [] };
  })()`, 30000).catch(() => ({ selector: null, items: [] }));
  return r || { selector: null, items: [] };
}

async function clickTocItem(cdp, sid, selector, index) {
  return evaluate(cdp, sid, `(() => {
    const el = document.querySelectorAll(${JSON.stringify(selector)})[${index}];
    if (!el) return 'missing';
    el.scrollIntoView({ block: 'center' });
    el.click();
    return 'clicked';
  })()`, 15000, false).catch(() => 'error');
}

// 逐章抓取并合并成一篇
async function captureToc(cdp, sid, { imagesDir, targetId, max, extractSrc, shotEach, outDir, width, dpr }) {
  const pageTitle = await evaluate(cdp, sid, 'document.title', 5000, false).catch(() => '');
  const entryUrl = await evaluate(cdp, sid, 'location.href', 5000, false).catch(() => '');
  await expandToc(cdp, sid);
  const toc = await readToc(cdp, sid);
  if (!toc.items.length) return null;
  log(`目录识别到 ${toc.items.length} 项（${toc.selector}）`);

  const chapters = [];
  const seen = new Set();
  let cur = sid;

  for (let i = 0; i < toc.items.length && chapters.length < max; i++) {
    const item = toc.items[i];
    const beforeUrl = await evaluate(cdp, cur, 'location.href', 5000, false).catch(() => '');
    const clickRes = await clickTocItem(cdp, cur, toc.selector, item.i);
    if (clickRes === 'missing') continue;

    let changed = false;
    for (let k = 0; k < 20; k++) {
      await sleep(500);
      let now = '';
      try { now = await evaluate(cdp, cur, 'location.href', 4000, false); } catch { now = ''; }
      if (now && now !== beforeUrl) { changed = true; break; }
    }
    if (changed) {
      // 导航会销毁旧 session，重新挂一个再继续
      cur = await attachFresh(cdp, targetId);
      await sleep(600);
      await waitForLoad(cdp, cur, 20000);
    }
    await triggerLazy(cdp, cur);

    log(`[${chapters.length + 1}/${Math.min(toc.items.length, max)}] ${item.text}`);
    const data = await evaluate(cdp, cur, extractSrc, 90000).catch(() => null);
    if (!data || typeof data.markdown !== 'string') continue;

    const key = data.markdown.slice(0, 300);
    if (seen.has(key)) { log('  内容与上一章相同，跳过'); continue; }
    seen.add(key);

    const prefix = String(chapters.length + 1).padStart(2, '0') + '-';
    const map = new Map();
    let cursor = 0;
    const total = data.images.length;
    async function worker() {
      while (true) {
        const n = cursor++;
        if (n >= total) return;
        const img = data.images[n];
        if (!img.src || img.src.startsWith('data:')) {
          const d = await decodeDataUri(img, imagesDir, prefix);
          if (d.ok) map.set(img.idx, d.file);
          continue;
        }
        const r = await downloadImage(cdp, cur, img, imagesDir, data.url, prefix);
        if (r.ok) map.set(img.idx, r.file);
      }
    }
    if (total) await Promise.all(Array.from({ length: Math.min(3, total) }, () => worker()));

    let md = data.markdown;
    for (const img of data.images) {
      const file = map.get(img.idx);
      const alt = (img.alt || '').replace(/[\[\]\n]/g, ' ').trim();
      const ref = file ? `![${alt}](images/${file})` : `![${alt}](${img.src})`;
      md = md.split(`{{IMG_${String(img.idx).padStart(4, '0')}}}`).join(ref);
    }
    // 章节标题是 ##，正文里的标题整体降一级
    md = md.replace(/^(#{1,6}) /gm, (m, h) => '#'.repeat(Math.min(6, h.length + 1)) + ' ');

    chapters.push({ title: item.text, markdown: md, url: data.url, images: map.size });

    if (shotEach) {
      try {
        const s = await captureShot(cdp, cur, path.join(outDir, 'shots'), width, dpr, `${prefix}${slugify(item.text, 20)}`);
        if (s) log(`  截图 ${s.file} (${s.height}px)`);
      } catch (e) { log('  截图失败：' + e.message); }
    }
  }

  if (!chapters.length) return null;
  const body = chapters.map(c => `## ${c.title}\n\n${c.markdown}`).join('\n\n');
  return {
    title: pageTitle || chapters[0].title,
    url: entryUrl || chapters[0].url,
    markdown: body,
    images: [],
    blockCount: chapters.reduce(function (n, c) { return n + c.markdown.length; }, 0),
    chapters: chapters.length,
    chapterTitles: chapters.map(function (c) { return c.title; }),
  };
}

// ─── 虚拟列表（懒渲染正文）──────────────────────────────────────────────────

// 飞书/语雀这类文档用 virtual-list + padding 占位，只渲染视口附近几块。
// 必须边滚边把每块抓下来，最后拼成一个离屏容器再统一提取。
async function harvestVirtual(cdp, sid) {
  const probe = await evaluate(cdp, sid, `(() => {
    const list = document.querySelector('.virtual-list, [class*=virtual-list], [class*=virtualList]');
    if (!list) return { found: false };
    return { found: true, height: Math.round(list.getBoundingClientRect().height), cls: (list.className || '').toString().slice(0, 40) };
  })()`, 15000).catch(() => ({ found: false }));
  if (!probe || !probe.found || probe.height < 8000) return null;

  log(`检测到虚拟列表（${probe.height}px），开始逐屏收集…`);
  const res = await evaluate(cdp, sid, `(async () => {
    const list = document.querySelector('.virtual-list, [class*=virtual-list], [class*=virtualList]');
    const group = list.querySelector('[role=group]') || list.firstElementChild;
    if (!group) return { blocks: 0, error: 'no group' };
    const step = Math.max(1000, Math.round(window.innerHeight * 1.1));
    let totalH = document.documentElement.scrollHeight;
    const deadline = Date.now() + 330000;
    const map = new Map();
    let truncated = false;

    function annotate(el) {
      for (const img of el.querySelectorAll('img')) {
        const lazy = img.getAttribute('data-src') || img.getAttribute('data-original') ||
          img.getAttribute('data-actualsrc') || '';
        let src = img.currentSrc || img.src || '';
        if ((!src || src.startsWith('data:')) && lazy) src = lazy;
        if (src) img.setAttribute('data-wb-src', src);
        let nw = img.naturalWidth || 0, nh = img.naturalHeight || 0;
        if (nw <= 1 || nh <= 1) { nw = 0; nh = 0; }
        const r = img.getBoundingClientRect();
        img.setAttribute('data-wb-size', [nw, nh, Math.round(r.width), Math.round(r.height)].join(','));
      }
      // 飞书文档把标题层级写在块类名里：docx-heading1-block / heading2 / h3 等，
      // 离屏重组后这些类名还在，直接记成 data-wb-h 最可靠
      const clsName = (el.className || '').toString();
      const hm = /(?:docx-)?heading([1-6])|(?:^|\s)h([1-6])(?:\s|$)/i.exec(clsName);
      if (hm) el.setAttribute('data-wb-h', hm[1] || hm[2]);

      // 离屏重组后样式会丢，字号必须趁现在记下来，否则标题层级全平
      let maxFs = 0;
      const nodes = [el].concat(Array.from(el.querySelectorAll('*')));
      for (const n of nodes) {
        const hasText = Array.from(n.childNodes).some(function (t) {
          return t.nodeType === 3 && t.nodeValue && t.nodeValue.trim();
        });
        if (!hasText) continue;
        const fs = parseFloat(getComputedStyle(n).fontSize) || 0;
        if (fs > maxFs) maxFs = fs;
      }
      if (maxFs) el.setAttribute('data-wb-fs', String(maxFs));
    }

    // 预热：先快速滚一遍让虚拟列表把内容渲染出来，收集阶段才不会每步都等渲染
    for (let wy = 0; wy <= totalH; wy += 2400) {
      window.scrollTo(0, wy);
      await new Promise(r => setTimeout(r, 60));
      if (Date.now() > deadline - 220000) break;
    }
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 400));
    totalH = document.documentElement.scrollHeight;

    for (let y = 0; y <= totalH; y += step) {
      if (Date.now() > deadline) { truncated = true; break; }
      window.scrollTo(0, y);
      await new Promise(r => setTimeout(r, 120));
      for (const el of group.children) {
        const rect = el.getBoundingClientRect();
        if (rect.height <= 0) continue;
        const absY = Math.round(rect.top + window.scrollY);
        if (map.has(absY)) continue;
        annotate(el);
        map.set(absY, el.outerHTML);
      }
    }
    window.scrollTo(0, 0);
    await new Promise(r => setTimeout(r, 200));

    const keys = Array.from(map.keys()).sort(function (a, b) { return a - b; });
    const holder = document.createElement('div');
    holder.id = '__w2f_harvest__';
    holder.setAttribute('style', 'position:absolute;left:-99999px;top:0;width:800px;');
    holder.innerHTML = keys.map(function (k) { return map.get(k); }).join('');
    document.body.appendChild(holder);
    window.__W2F_ROOT__ = holder;
    return { blocks: keys.length, chars: (holder.innerText || '').length, truncated };
  })()`, 400000).catch((e) => ({ blocks: 0, error: e.message }));

  if (!res || !res.blocks) {
    log('虚拟列表收集失败：' + (res && res.error ? res.error : '未知原因'));
    return null;
  }
  log(`收集到 ${res.blocks} 块、${res.chars} 字${res.truncated ? '（达到时间上限，后面可能不全）' : ''}`);
  return res;
}

async function clearHarvest(cdp, sid) {
  await evaluate(cdp, sid, `(() => {
    const el = document.getElementById('__w2f_harvest__');
    if (el) el.remove();
    window.__W2F_ROOT__ = null;
    return true;
  })()`, 10000, false).catch(() => {});
}

// 虚拟列表页面：整页截图只会拍到视口附近的块，必须逐屏截再拼
async function captureShotScrolling(cdp, sid, outDir, width, dpr, namePrefix = 'fullpage') {
  fs.mkdirSync(outDir, { recursive: true });
  const vh = 1000;
  await cdp.send('Emulation.setDeviceMetricsOverride', { width, height: vh, deviceScaleFactor: dpr, mobile: false }, sid);
  await sleep(1000);

  const m = await cdp.send('Page.getLayoutMetrics', {}, sid);
  const H = Math.ceil(m.cssContentSize.height);
  log(`逐屏截图：${width} x ${H}，视口 ${vh}`);
  const step = Math.floor(vh * 0.85);
  const tiles = [];

  for (let y = 0; y < H; y += step) {
    await cdp.send('Runtime.evaluate', { expression: `window.scrollTo(0, ${y})` }, sid, 10000);
    await sleep(320);
    const shot = await cdp.send('Page.captureScreenshot', { format: 'png' }, sid, 60000);
    const f = path.join(outDir, `_sc${String(tiles.length).padStart(3, '0')}.png`);
    fs.writeFileSync(f, Buffer.from(shot.data, 'base64'));
    tiles.push({ path: f, y });
    if (y + vh >= H) break;
  }
  await cdp.send('Runtime.evaluate', { expression: 'window.scrollTo(0, 0)' }, sid, 10000).catch(() => {});

  const useJpeg = H > 20000;
  const outFile = path.join(outDir, useJpeg ? `${namePrefix}.jpg` : `${namePrefix}.png`);
  const py = pythonWithPIL();
  if (!py) {
    log('未找到带 Pillow 的 Python，保留分屏文件');
    return { height: H, file: null, tiled: true, stitched: false };
  }

  const script = [
    'from PIL import Image',
    'Image.MAX_IMAGE_PIXELS = None',
    `tiles = [${tiles.map(t => `(${JSON.stringify(t.path)}, ${t.y})`).join(', ')}]`,
    `full_w, full_h = ${width * dpr}, ${H * dpr}`,
    'scale = min(1.0, 65000.0 / full_h)',
    'W, H2 = int(full_w * scale), int(full_h * scale)',
    'out = Image.new("RGB", (W, H2), "white")',
    'for p, y in tiles:',
    '    im = Image.open(p)',
    '    if scale < 1.0:',
    '        im = im.resize((W, max(1, int(im.size[1] * scale))), Image.LANCZOS)',
    `    out.paste(im, (0, int(y * ${dpr} * scale)))`,
    '    im.close()',
    'try:',
    '    import numpy as np',
    '    arr = np.asarray(out.convert("L"))',
    '    rows = (arr < 245).sum(axis=1)',
    '    nz = np.where(rows > 8)[0]',
    '    if len(nz) > 0:',
    '        last = int(nz[-1]) + 40',
    '        if last < H2 - 200:',
    '            out = out.crop((0, 0, W, last))',
    'except Exception:',
    '    pass',
    `out.save(${JSON.stringify(outFile)}${useJpeg ? ', quality=85' : ''})`,
  ].join('\n');
  const sp = path.join(outDir, '_stitch_scroll.py');
  fs.writeFileSync(sp, script);
  execFileSync(py, [sp], { stdio: 'ignore', timeout: 300000 });
  fs.unlinkSync(sp);
  for (const t of tiles) { try { fs.unlinkSync(t.path); } catch {} }

  const st = fs.statSync(outFile);
  const dims = execFileSync(py, ['-c', `from PIL import Image;print(Image.open(${JSON.stringify(outFile)}).size[1])`], { encoding: 'utf8' }).trim();
  return { height: parseInt(dims, 10) || H, file: path.basename(outFile), bytes: st.size, tiled: true, stitched: true };
}

async function main() {
  if (flags.help) {
    console.log(fs.readFileSync(new URL(import.meta.url), 'utf8').split('// ─── args')[0]);
    return;
  }

  if (flags.check) {
    const bin = chromeBinary();
    const found = await discoverChrome();
    console.log(JSON.stringify({
      node: process.version,
      chrome: bin || null,
      debugging: found ? { port: found.port, source: flags.port ? 'flag' : 'discovery' } : null,
      ok: Boolean(bin),
    }, null, 2));
    process.exit(0);
    return;
  }

  const url = flags.url || (!flags.tab ? positional[0] : null);
  const tab = flags.tab || (!url ? positional[1] : null);

  if (flags.list) {
    const found = await discoverChrome();
    if (!found) die('没有可用的 Chrome 调试端口。用 --url 模式可自动拉起无头 Chrome。');
    const cdp = new CDP(found.wsUrl);
    await cdp.connect();
    const { targetInfos } = await cdp.send('Target.getTargets');
    const pages = targetInfos.filter(t => t.type === 'page').map(t => ({ targetId: t.targetId, title: t.title, url: t.url }));
    console.log(JSON.stringify(pages, null, 2));
    cdp.close();
    setTimeout(() => process.exit(0), 50);
    return;
  }

  if (!url && !tab) die('需要 --url <地址> 或 --tab <targetId>（先用 --list 查看）');

  // Output dir
  let outDir = flags.out;
  if (!outDir) {
    const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 13);
    const guess = url ? slugify(decodeURIComponent(url).replace(/^https?:\/\//, ''), 30) : 'tab';
    outDir = path.resolve(process.cwd(), 'webpage-captures', `${guess}-${stamp}`);
  }
  outDir = path.resolve(outDir);
  fs.mkdirSync(path.join(outDir, 'images'), { recursive: true });

  // Browser
  let chrome = null;
  let launched = null;
  if (!flags['own-chrome']) {
    for (let i = 0; i < 5 && !chrome; i++) {
      chrome = await discoverChrome();
      if (!chrome) await sleep(700);
    }
  }
  if (!chrome && tab) {
    die('没有可用的 Chrome 调试端口。请确认 Chrome 里已勾选 Allow remote debugging（chrome://inspect/#remote-debugging），且那个标签页还开着。');
  }
  if (!chrome) {
    launched = await launchChrome();
    chrome = launched;
  }

  const cdp = new CDP(chrome.wsUrl);
  await cdp.connect();

  let targetId = null;
  let created = false;

  if (tab) {
    targetId = tab;
    const { targetInfos } = await cdp.send('Target.getTargets');
    if (!targetInfos.some(t => t.targetId === tab)) die('找不到该标签页：' + tab);
  } else {
    const created_ = await cdp.send('Target.createTarget', { url: 'about:blank', background: true });
    targetId = created_.targetId;
    created = true;
  }

  const { sessionId: sid } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
  await cdp.send('Page.enable', {}, sid);
  await cdp.send('Runtime.enable', {}, sid);

  const shutdown = async () => {
    ACTIVE = null;
    if (created) { try { await cdp.send('Target.closeTarget', { targetId }); } catch {} }
    cdp.close();
    if (launched) {
      try { launched.proc.kill('SIGKILL'); } catch {}
      try { fs.rmSync(launched.profile, { recursive: true, force: true }); } catch {}
    }
  };
  ACTIVE = { cdp, launched, targetId, created };

  if (url) {
    log('打开页面：' + url);
    try {
      await cdp.send('Page.navigate', { url }, sid, 30000);
    } catch (e) {
      log('导航未在 30s 内返回（' + e.message + '），继续等待页面内容…');
    }
  }

  await waitForLoad(cdp, sid, WAIT);

  // 导航失败会落到 Chrome 错误页，别把错误页当成正文发出去
  const landed = await evaluate(cdp, sid, 'location.href', 5000, false).catch(() => '');
  if (typeof landed === 'string' && landed.startsWith('chrome-error://')) {
    await shutdown();
    die('页面打不开（导航超时或网络不可达）：' + (url || tab));
  }

  // --probe：只体检，不产出。用来判断页面该走 DOM 提取还是截图识别
  if (flags.probe) {
    const info = await evaluate(cdp, sid, `(() => {
      const text = (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
      const imgs = Array.from(document.querySelectorAll('img'));
      const frames = Array.from(document.querySelectorAll('iframe')).map(f => f.src || '(no src)');
      return {
        url: location.href,
        title: document.title,
        textLength: text.length,
        textPreview: text.slice(0, 300),
        imgCount: imgs.length,
        bigImgs: imgs.filter(i => i.naturalWidth > 200).length,
        canvasCount: document.querySelectorAll('canvas').length,
        iframes: frames,
        bodyChildren: document.body.children.length,
      };
    })()`, 20000);
    console.log(JSON.stringify(info, null, 2));
    await shutdown();
    return;
  }

  await cdp.send('Emulation.setDeviceMetricsOverride', {
    width: WIDTH, height: 800, deviceScaleFactor: DPR, mobile: false,
  }, sid).catch(() => {});
  await sleep(800);

  await expandScrollContainers(cdp, sid);

  let harvested = null;
  await triggerLazy(cdp, sid);

  // Body metrics before extraction
  const info = await cdp.send('Page.getLayoutMetrics', {}, sid);
  const pageH = Math.ceil(info.cssContentSize.height);

  // Extract
  const extractSrc = fs.readFileSync(new URL('./extract-in-page.js', import.meta.url), 'utf8');
  let data;

  if (flags.toc) {
    log('按侧边栏目录逐章抓取…');
    data = await captureToc(cdp, sid, {
      imagesDir: path.join(outDir, 'images'),
      targetId,
      max: parseInt(flags['toc-max'] || '60', 10),
      extractSrc,
      shotEach: Boolean(flags['shot-each']),
      outDir,
      width: WIDTH,
      dpr: DPR,
    });
    if (!data) die('没有识别到侧边栏目录。确认页面左侧有目录树，或去掉 --toc 按单页抓。');
    log(`共合并 ${data.chapters} 章`);
  } else {
    if (!flags['no-virtual']) harvested = await harvestVirtual(cdp, sid);
    log('提取正文…');
    try {
      data = await evaluate(cdp, sid, extractSrc, 90000);
    } finally {
      await clearHarvest(cdp, sid);
    }
    if (!data || typeof data.markdown !== 'string') die('正文提取失败');
  }

  // 文档查看器/嵌入页常把正文放在 iframe 里，顶层拿不到就逐个 frame 再试
  const topBlocks = data.blockCount || 0;
  if (topBlocks < 8) {
    try {
      const tree = await cdp.send('Page.getFrameTree', {}, sid);
      const frames = [];
      (function collect(node) {
        for (const child of node.childFrames || []) {
          frames.push(child.frame);
          collect(child);
        }
      })(tree.frameTree);
      for (const fr of frames) {
        if (!fr.url || fr.url === 'about:blank') continue;
        try {
          const { executionContextId } = await cdp.send('Page.createIsolatedWorld', {
            frameId: fr.id, worldName: 'w2f-extract',
          }, sid);
          const r = await cdp.send('Runtime.evaluate', {
            expression: extractSrc, contextId: executionContextId,
            returnByValue: true, awaitPromise: true,
          }, sid, 90000);
          const cand = r.result && r.result.value;
          if (cand && typeof cand.markdown === 'string' && (cand.blockCount || 0) > (data.blockCount || 0)) {
            log(`正文在 iframe 里，改用 ${fr.url.slice(0, 60)}`);
            data = cand;
          }
        } catch (e) {
          log(`iframe 提取跳过（${fr.url.slice(0, 40)}）：${e.message}`);
        }
      }
    } catch { /* frame tree 不可用就算了 */ }
  }
  log(`标题：${data.title}`);
  log(`块数：${data.blockCount}，图片：${data.images.length}`);

  // Download images (4 路并发；CDP 单连接上按请求 id 分发，安全)
  const imagesDir = path.join(outDir, 'images');
  const map = new Map();
  let okCount = 0;
  let cursor = 0;
  const total = data.images.length;

  async function worker() {
    while (true) {
      const i = cursor++;
      if (i >= total) return;
      const img = data.images[i];
      if (!img.src || img.src.startsWith('data:')) {
        const d = await decodeDataUri(img, imagesDir);
        if (d.ok) { map.set(img.idx, d.file); okCount++; }
        continue;
      }
      const r = await downloadImage(cdp, sid, img, imagesDir, data.url);
      if (r.ok) { map.set(img.idx, r.file); okCount++; }
      else log(`  图片 ${img.idx} 失败（${r.reason}），保留远程地址`);
      if (okCount && okCount % 10 === 0) log(`  已下载 ${okCount}/${total}`);
    }
  }

  if (total) {
    const n = Math.min(3, total);
    await Promise.all(Array.from({ length: n }, () => worker()));
  }
  log(`图片落盘 ${okCount}/${total}`);

  // Markdown rewrite
  const stamp = new Date();
  const local = String(stamp.getFullYear()) + '-' + String(stamp.getMonth() + 1).padStart(2, '0') + '-' +
    String(stamp.getDate()).padStart(2, '0') + ' ' + String(stamp.getHours()).padStart(2, '0') + ':' +
    String(stamp.getMinutes()).padStart(2, '0');

  for (const img of data.images) {
    const file = map.get(img.idx);
    const alt = (img.alt || '').replace(/[\[\]\n]/g, ' ').trim();
    img._local = file ? `![${alt}](images/${file})` : `![${alt}](${img.src})`;
    img._feishu = file ? `![${alt}](@./images/${file})` : `![${alt}](${img.src})`;
  }

  // 两套输出：本地版用相对路径，飞书版用 @./ 前缀（占位符替换不能跨版本复用）
  function render(useFeishu) {
    let out = data.markdown;
    for (const img of data.images) {
      out = out.split(`{{IMG_${String(img.idx).padStart(4, '0')}}}`).join(useFeishu ? img._feishu : img._local);
    }
    // 逐章抓取时 markdown 里已经是 images/xxx 的本地路径，飞书版补上 @./ 前缀
    if (useFeishu) out = out.replace(/\]\(images\//g, '](@./images/');
    return out;
  }

  let site = data.site;
  if (!site) { try { site = new URL(data.url).hostname; } catch { site = ''; } }
  let header = `# ${data.title}\n\n> 来源：${site} · [原文链接](${data.url})\n> 采集时间：${local}\n\n`;
  if (Array.isArray(data.chapterTitles) && data.chapterTitles.length) {
    header += data.chapterTitles.map(function (t) { return '- ' + t; }).join('\n') + '\n\n---\n\n';
  }

  fs.writeFileSync(path.join(outDir, 'content.md'), header + render(false) + '\n', 'utf8');
  fs.writeFileSync(path.join(outDir, 'content.feishu.md'), header + render(true) + '\n', 'utf8');

  // Screenshot
  let shot = null;
  if (!flags['no-shot'] && !flags.toc) {
    log('整页截图…');
    try {
      shot = harvested
        ? await captureShotScrolling(cdp, sid, outDir, WIDTH, DPR)
        : await captureShot(cdp, sid, outDir, WIDTH, DPR);
    } catch (e) {
      log('截图失败：' + e.message);
    }
  }

  await restorePage(cdp, sid);
  await shutdown();

  const meta = {
    title: data.title,
    url: data.url,
    site: data.site,
    author: data.author,
    published: data.published,
    capturedAt: new Date().toISOString(),
    pageHeight: pageH,
    blocks: data.blockCount,
    chapters: data.chapters || undefined,
    chapterTitles: data.chapterTitles || undefined,
    images: data.images.map(i => ({ idx: i.idx, src: i.src, file: map.get(i.idx) || null })),
    screenshot: shot ? { file: shot.file, height: shot.height, bytes: shot.bytes } : null,
    outDir,
  };
  fs.writeFileSync(path.join(outDir, 'meta.json'), JSON.stringify(meta, null, 2), 'utf8');

  if (flags.json) {
    console.log(JSON.stringify({ ok: true, outDir, title: data.title, images: okCount, totalImages: data.images.length, screenshot: shot, markdown: path.join(outDir, 'content.feishu.md') }));
  } else {
    log('');
    log('完成：' + outDir);
    log('  content.md          本地可读版');
    log('  content.feishu.md   飞书导入版（图片为 @./ 前缀）');
    if (shot) log('  fullpage.png        整页长截图 ' + shot.height + 'px');
    console.log(outDir);
  }
}

async function decodeDataUri(img, dir, prefix = '') {
  try {
    const m = /^data:([^;]+);base64,(.*)$/s.exec(img.src);
    if (!m) return { ok: false };
    const ext = extFromType(m[1], '');
    const name = prefix + String(img.idx).padStart(3, '0') + '-' + slugify(img.alt || 'inline') + ext;
    fs.writeFileSync(path.join(dir, name), Buffer.from(m[2], 'base64'));
    return { ok: true, file: name };
  } catch { return { ok: false }; }
}

let launchedProcess = null;

main().catch((e) => {
  console.error('ERROR: ' + (e && e.stack ? e.stack : e));
  process.exit(1);
});
