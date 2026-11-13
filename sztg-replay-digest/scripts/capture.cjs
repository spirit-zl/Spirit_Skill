#!/usr/bin/env node
/**
 * capture.cjs —— 策牛科技（app.cn.sztg.com）直播回放抓取
 *
 * 子命令：
 *   status                      检查持久化 profile 的登录态（退出码 0 已登录 / 4 需扫码）
 *   qr --out <png>              输出微信登录二维码并等待（退出码 0 已登录 / 4 超时）
 *   list                        列出直播间全部回放（JSON）
 *   fetch [--index N|--date D] --outdir <dir>
 *                               抓取一期的 m3u8，下载分片，抽音轨，写 meta.json
 *
 * 退出码：0 成功；3 找不到目标期；4 需要扫码登录；5 环境/依赖问题；6 抓取失败
 */

const { spawn, spawnSync } = require('child_process');
const fs = require('fs');
const os = require('os');
const path = require('path');

const CHROME_CANDIDATES = [
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  '/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge',
  '/Applications/Chromium.app/Contents/MacOS/Chromium',
];
const DATA_DIR = path.join(os.homedir(), '.workbuddy', 'data', 'sztg-replay');
const PROFILE = path.join(DATA_DIR, 'profile');
const CONFIG = path.join(DATA_DIR, 'config.json');
const PORT = 9983;

const sleep = ms => new Promise(r => setTimeout(r, ms));
const die = (code, msg) => { console.error(msg); process.exit(code); };

function loadConfig() {
  try { return JSON.parse(fs.readFileSync(CONFIG, 'utf8')); } catch { return {}; }
}

function chromePath() {
  const cfg = loadConfig();
  const cands = cfg.chrome ? [cfg.chrome, ...CHROME_CANDIDATES] : CHROME_CANDIDATES;
  for (const p of cands) if (fs.existsSync(p)) return p;
  die(5, '找不到 Chrome/Edge/Chromium，可在 ' + CONFIG + ' 里配置 {"chrome": "/path/to/Chrome"}');
}

class CDP {
  constructor(ws) { this.ws = ws; this.id = 0; this.pending = new Map(); this.handlers = []; }
  static async connect(url) {
    const ws = new WebSocket(url);
    await new Promise((res, rej) => { ws.onopen = res; ws.onerror = () => rej(new Error('ws fail')); });
    const c = new CDP(ws);
    ws.onmessage = ev => {
      const m = JSON.parse(ev.data);
      if (m.id && c.pending.has(m.id)) {
        const p = c.pending.get(m.id); c.pending.delete(m.id);
        m.error ? p.reject(new Error(JSON.stringify(m.error))) : p.resolve(m.result);
      } else if (m.method) c.handlers.forEach(f => f(m));
    };
    return c;
  }
  send(method, params = {}) {
    const id = ++this.id;
    this.ws.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  }
  async ev(expr) {
    const r = await this.send('Runtime.evaluate', { expression: expr, returnByValue: true, awaitPromise: true });
    if (r.exceptionDetails) return 'EX:' + (r.exceptionDetails.text || '');
    return r.result ? r.result.value : undefined;
  }
  async shot(file) {
    const r = await this.send('Page.captureScreenshot', { format: 'png' });
    fs.writeFileSync(file, Buffer.from(r.data, 'base64'));
    return file;
  }
}

async function withBrowser(fn) {
  fs.mkdirSync(PROFILE, { recursive: true });
  const args = [
    '--headless=new', `--remote-debugging-port=${PORT}`, `--user-data-dir=${PROFILE}`,
    '--no-sandbox', '--disable-gpu-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
    '--no-first-run', '--no-default-browser-check', '--disable-features=NetworkServiceSandbox',
    '--autoplay-policy=no-user-gesture-required', '--mute-audio', '--window-size=1440,1000',
    'about:blank',
  ];
  const chrome = spawn(chromePath(), args, { stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  chrome.stderr.on('data', d => { stderr += d.toString(); });
  try {
    let tabs = null;
    for (let i = 0; i < 100; i++) {
      try { tabs = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json(); break; }
      catch { await sleep(250); }
    }
    if (!tabs) die(5, 'Chrome DevTools 连不上：' + stderr.slice(-300));
    const page = tabs.find(t => t.type === 'page');
    const c = await CDP.connect(page.webSocketDebuggerUrl);
    await c.send('Page.enable');
    await c.send('Runtime.enable');
    await c.send('Network.enable');
    return await fn(c);
  } finally {
    chrome.kill('SIGKILL');
  }
}

function roomUrl(cfg, room, cid, eid) {
  const r = room || cfg.room || '568';
  const c = cid || cfg.cid || '7552';
  const e = eid || cfg.eid || '7061';
  return `https://app.cn.sztg.com/zhibo.html?roomid=${r}&eid=${e}&cid=${c}`;
}

async function ensureLogin(c, cfg) {
  await c.send('Page.navigate', { url: roomUrl(cfg) });
  await sleep(9000);
  const url = await c.ev('location.href');
  const logged = await c.ev('(typeof bossdk!=="undefined" && !!bossdk.ahinfo && bossdk.ahinfo.loginstatus===1)');
  return { url, logged: logged === true };
}

async function dumpQr(c, out) {
  const src = await c.ev('(function(){var i=Array.from(document.images).find(function(x){return (x.src||"").indexOf("showqrcode")>=0});return i?i.src:""})()');
  if (!src) return null;
  const png = out || path.join(DATA_DIR, 'login-qr.png');
  const r = await fetch(src);
  fs.writeFileSync(png, Buffer.from(await r.arrayBuffer()));
  return png;
}

async function listReplays(c) {
  return await c.ev(`(function(){
    var items = Array.from(document.querySelectorAll('.item'));
    return JSON.stringify(items.map(function(e){
      var t = (e.innerText||'').split('\\n');
      return { title: (t[0]||'').trim(), time: (t[1]||'').trim() };
    }));
  })()`);
}

async function clickReplay(c, index) {
  return await c.ev(`(function(){
    var items = Array.from(document.querySelectorAll('.item'));
    var el = items[${index}];
    if (!el) return 'NO_ITEM';
    el.click();
    return 'OK:' + (el.innerText||'').replace(/\\n/g,'|');
  })()`);
}

async function commandStatus() {
  const cfg = loadConfig();
  const r = await withBrowser(async c => {
    const st = await ensureLogin(c, cfg);
    const account = await c.ev('(function(){try{return bossdk.ahinfo.appuserid}catch(e){return null}})()');
    return { ...st, account };
  });
  console.log(JSON.stringify(r, null, 2));
  process.exit(r.logged ? 0 : 4);
}

async function commandQr(args) {
  const out = args.out || path.join(DATA_DIR, 'login-qr.png');
  const cfg = loadConfig();
  const r = await withBrowser(async c => {
    const st = await ensureLogin(c, cfg);
    if (st.logged) return { logged: true, account: await c.ev('(function(){try{return bossdk.ahinfo.appuserid}catch(e){return null}})()') };
    const qr = await dumpQr(c, out);
    if (!qr) return { logged: false, qr: null };
    // 轮询等待扫码，最长 4 分钟
    for (let i = 0; i < 48; i++) {
      await sleep(5000);
      const ok = await c.ev('(typeof bossdk!=="undefined" && !!bossdk.ahinfo && bossdk.ahinfo.loginstatus===1)');
      if (ok === true) return { logged: true, qr, account: await c.ev('(function(){try{return bossdk.ahinfo.appuserid}catch(e){return null}})()') };
      if (i === 40) await dumpQr(c, out); // 二维码可能刷新
    }
    return { logged: false, qr, timeout: true };
  });
  console.log(JSON.stringify(r, null, 2));
  process.exit(r.logged ? 0 : 4);
}

async function commandList() {
  const cfg = loadConfig();
  const r = await withBrowser(async c => {
    const st = await ensureLogin(c, cfg);
    if (!st.logged) return { logged: false };
    const raw = await listReplays(c);
    return { logged: true, items: JSON.parse(raw || '[]') };
  });
  console.log(JSON.stringify(r, null, 2));
  process.exit(r.logged ? 0 : 4);
}

async function commandFetch(args) {
  const outdir = args.outdir || path.join(DATA_DIR, 'tmp-' + Date.now());
  fs.mkdirSync(outdir, { recursive: true });
  const cfg = loadConfig();

  const picked = await withBrowser(async c => {
    const st = await ensureLogin(c, cfg);
    if (!st.logged) return { logged: false };
    const raw = await listReplays(c);
    const items = JSON.parse(raw || '[]');
    if (!items.length) return { logged: true, error: 'EMPTY_LIST' };

    let idx = 0;
    if (args.date) {
      const d = String(args.date).replace(/-/g, '.').replace(/^(\d{4})\.(\d{2})\.(\d{2})$/, '$1.$2');
      const hit = items.findIndex(it => (it.time || '').startsWith(String(args.date)) || (it.title || '').includes(d.replace(/\.0?(\d)/, '.$1')));
      if (hit < 0) return { logged: true, error: 'NO_MATCH', items };
      idx = hit;
    } else if (args.index) {
      idx = parseInt(args.index, 10) || 0;
    }

    const m3u8s = [];
    c.handlers.push(m => {
      if (m.method === 'Network.responseReceived') {
        const u = m.params.response.url;
        if (/\.m3u8(\?|$)/i.test(u) && !m3u8s.includes(u)) m3u8s.push(u);
      }
    });
    const click = await clickReplay(c, idx);
    if (click === 'NO_ITEM') return { logged: true, error: 'NO_ITEM', items };
    for (let i = 0; i < 30 && !m3u8s.length; i++) await sleep(1000);
    return { logged: true, chosen: items[idx], index: idx, m3u8: m3u8s[0] || null };
  });

  if (!picked.logged) die(4, '需要扫码登录，先运行 qr');
  if (picked.error) { console.log(JSON.stringify(picked, null, 2)); die(3, '未取到目标回放：' + picked.error); }
  if (!picked.m3u8) die(6, '没有捕获到 m3u8，页面可能还没加载完或回放不可播');

  const base = picked.m3u8.replace(/[^/]+$/, '');
  const m3 = await (await fetch(picked.m3u8, { headers: { Referer: 'https://app.cn.sztg.com/' } })).text();
  fs.writeFileSync(path.join(outdir, 'play.m3u8'), m3);
  const segs = m3.split('\n').map(s => s.trim()).filter(s => s && !s.startsWith('#'));
  if (!segs.length) die(6, 'm3u8 里没有分片');

  // 日期：优先 m3u8 文件名里的 _YYYYMMDDHHMMSS
  const fn = picked.m3u8.split('/').pop();
  const mDate = fn.match(/(20\d{6})\d{6}/);
  const date = mDate ? `${mDate[1].slice(0, 4)}-${mDate[1].slice(4, 6)}-${mDate[1].slice(6, 8)}` : null;

  const segDir = path.join(outdir, 'ts');
  fs.mkdirSync(segDir, { recursive: true });
  let ok = 0;
  const queue = segs.slice();
  const workers = Array.from({ length: 12 }, async () => {
    while (queue.length) {
      const s = queue.shift();
      const dst = path.join(segDir, path.basename(s));
      try {
        const r = await fetch(base + s, { headers: { Referer: 'https://app.cn.sztg.com/' } });
        if (!r.ok) continue;
        fs.writeFileSync(dst, Buffer.from(await r.arrayBuffer()));
        ok++;
      } catch { /* 忽略单个分片失败 */ }
    }
  });
  await Promise.all(workers);

  const listFile = path.join(outdir, 'concat.txt');
  fs.writeFileSync(listFile, segs.map(s => `file '${path.join(segDir, path.basename(s))}'`).join('\n'));

  const ff = ffmpegPath();
  const audio = path.join(outdir, 'audio.mp3');
  const r = spawnSync(ff, ['-hide_banner', '-loglevel', 'error', '-y', '-f', 'concat', '-safe', '0',
    '-i', listFile, '-vn', '-ac', '1', '-ar', '16000', '-b:a', '48k', audio], { encoding: 'utf8' });
  if (!fs.existsSync(audio)) die(6, 'ffmpeg 抽音轨失败：' + (r.stderr || '').slice(-300));

  const dur = spawnSync(ff, ['-hide_banner', '-i', audio], { encoding: 'utf8' });
  const dm = (dur.stderr || '').match(/Duration: (\d+):(\d+):(\d+)/);
  const seconds = dm ? (+dm[1]) * 3600 + (+dm[2]) * 60 + (+dm[3]) : null;

  const meta = {
    date, source_url: roomUrl(cfg), replay_title: picked.chosen && picked.chosen.title,
    replay_time: picked.chosen && picked.chosen.time, m3u8: picked.m3u8,
    segments: segs.length, segments_ok: ok, duration_seconds: seconds,
    audio, outdir,
  };
  fs.writeFileSync(path.join(outdir, 'meta.json'), JSON.stringify(meta, null, 2));
  console.log(JSON.stringify(meta, null, 2));
  process.exit(0);
}

function ffmpegPath() {
  const cfg = loadConfig();
  if (cfg.ffmpeg && fs.existsSync(cfg.ffmpeg)) return cfg.ffmpeg;
  const which = spawnSync('which', ['ffmpeg'], { encoding: 'utf8' });
  if (which.status === 0) return which.stdout.trim();
  const py = spawnSync('python3', ['-c', 'import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())'], { encoding: 'utf8' });
  if (py.status === 0 && fs.existsSync(py.stdout.trim())) return py.stdout.trim();
  die(5, '缺 ffmpeg：运行 python3 -m pip install imageio-ffmpeg，或在 config.json 里配置 {"ffmpeg": "/path/to/ffmpeg"}');
}

// ---- 入口 ----
const argv = process.argv.slice(2);
const cmd = argv[0];
const opt = {};
for (let i = 1; i < argv.length; i++) {
  if (argv[i].startsWith('--')) { opt[argv[i].slice(2)] = argv[i + 1] && !argv[i + 1].startsWith('--') ? argv[++i] : true; }
}
(async () => {
  if (cmd === 'status') return commandStatus();
  if (cmd === 'qr') return commandQr(opt);
  if (cmd === 'list') return commandList();
  if (cmd === 'fetch') return commandFetch(opt);
  console.log(fs.readFileSync(__filename, 'utf8').split('*/')[0]);
  process.exit(1);
})().catch(e => { console.error('FAILED: ' + (e && e.stack || e)); process.exit(6); });
