// extract-in-page.js — runs inside the page via Runtime.evaluate.
// Returns JSON: { title, url, site, author, published, markdown, images[] }
// Images are replaced by {{IMG_0001}} markers; the caller downloads them and rewrites.
(function () {
  const BLOCK_TAGS = new Set(['P', 'DIV', 'SECTION', 'ARTICLE', 'MAIN', 'HEADER', 'FOOTER', 'ASIDE',
    'NAV', 'UL', 'OL', 'LI', 'BLOCKQUOTE', 'PRE', 'TABLE', 'FIGURE', 'H1', 'H2', 'H3', 'H4', 'H5',
    'H6', 'HR', 'FORM', 'DL', 'DT', 'DD', 'ADDRESS']);

  const JUNK_TAGS = new Set(['SCRIPT', 'STYLE', 'NOSCRIPT', 'IFRAME', 'CANVAS', 'SVG', 'FORM',
    'INPUT', 'BUTTON', 'SELECT', 'TEXTAREA', 'VIDEO', 'AUDIO', 'MAP', 'OBJECT', 'EMBED', 'LINK', 'META',
    'NAV', 'HEADER', 'FOOTER', 'ASIDE']);

  const JUNK_ROLE = /^(navigation|banner|contentinfo|complementary|search|menu|menubar|dialog|alertdialog|toolbar|tablist)$/i;

  // Conservative: only obviously-decorative containers.
  const JUNK_CLASS = /(^|[\s_-])(ads?|advert|adsbygoogle|share|share-?bar|social|comment|comments|related|recommend|sidebar|side-?bar|footer|navbar|nav|menu|toolbar|breadcrumbs?|pagination|pager|qrcode|qr-?code|promo|subscribe|newsletter|popup|modal|float|sticky-?bar|copyright|disclaimer|login-?box|app-?download|post-?nav|article-?nav|prev-?next|next-?prev|byline|meta-?info)([\s_-]|$)/i;

  const IMG_SKIP = /(^|[\s_\/-])(avatar|icon|icons|logo|emoji|sprite|badge|bullet|spacer|placeholder|loading|arrow|btn|button)([\s_\-\/.]|$)/i;

  // 行内元素不可能是滚动容器/广告区块/hero 背景，跳过它们能省掉绝大部分 getComputedStyle
  const INLINE_TAGS = new Set(['SPAN', 'A', 'B', 'I', 'EM', 'STRONG', 'SMALL', 'CODE', 'SUP', 'SUB',
    'MARK', 'TIME', 'U', 'S', 'DEL', 'INS', 'LABEL', 'ABBR', 'CITE', 'Q', 'VAR', 'KBD', 'SAMP',
    'BDI', 'BDO', 'WBR', 'FONT', 'BIG']);

  function abs(u) {
    try { return new URL(u, document.baseURI).href; } catch (e) { return u; }
  }

  function esc(t) {
    return String(t)
      .replace(/\\/g, '\\\\')
      .replace(/([*_\[\]<>$~`])/g, '\\$1');
  }

  function escLineStart(t) {
    return t.replace(/^(\s*)(#{1,6}\s|[-+>]\s|\d+\.\s)/, '$1\\$2');
  }

  function clean(s) {
    return String(s || '')
      .replace(/[\u200b\u200c\u200d\ufeff\u200e\u200f]/g, '')
      .replace(/\u00a0/g, ' ')
      .replace(/[ \t]+/g, ' ')
      .replace(/\s+$/, '')
      .replace(/^\s+/, '');
  }

  function pad4(n) { return String(n).padStart(4, '0'); }

  // ─── Pick the main content root ──────────────────────────────────────────

  function textLen(el) { return (el.innerText || '').length; }

  function score(el) {
    if (!el || !el.querySelectorAll) return -1;
    const t = textLen(el);
    if (t < 120) return -1;
    const ps = el.querySelectorAll('p').length;
    const imgs = el.querySelectorAll('img').length;
    let linkLen = 0;
    for (const a of el.querySelectorAll('a')) linkLen += (a.innerText || '').length;
    const density = t ? linkLen / t : 1;
    if (density > 0.5 && ps < 5) return -1;
    return t * (1 - Math.min(density, 0.95)) + ps * 40 + Math.min(imgs, 25) * 25;
  }

  function pickRoot() {
    const candidates = [];
    const selectors = ['article', 'main', '[role="main"]', '#js_content', '.rich_media_content',
      '.article-content', '.article_body', '.post-content', '.entry-content', '.markdown-body',
      '#content', '.content', '.article', '.post', '.blog-post', '#article', '.detail-content'];
    for (const sel of selectors) {
      for (const el of document.querySelectorAll(sel)) candidates.push(el);
    }
    for (const el of document.querySelectorAll('div,section')) {
      if (el.querySelectorAll('p').length >= 3 && textLen(el) > 400) candidates.push(el);
    }
    let best = null, bestScore = 0;
    for (const el of candidates) {
      const s = score(el);
      if (s > bestScore) { best = el; bestScore = s; }
    }
    return best || document.body;
  }

  const root = window.__W2F_ROOT__ || pickRoot();

  function normTitle(s) {
    return clean(String(s || '')).replace(/[\s\u3000|·—–:：]+/g, '').toLowerCase();
  }
  function metaOf(sel) {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute('content') || '').trim() : '';
  }
  const h1El = document.querySelector('h1');
  const pageTitle = metaOf('meta[property="og:title"]') ||
    (h1El ? clean(h1El.innerText) : '') || clean(document.title);

  // ─── Pre-pass: annotate images, mark junk ────────────────────────────────

  function markJunk(el) {
    if (el.nodeType !== 1) return;
    const tag = el.tagName;
    if (JUNK_TAGS.has(tag)) { el.setAttribute('data-wb-skip', '1'); return; }
    const role = el.getAttribute('role');
    if (role && JUNK_ROLE.test(role)) { el.setAttribute('data-wb-skip', '1'); return; }
    if (el.getAttribute('aria-hidden') === 'true') { el.setAttribute('data-wb-skip', '1'); return; }
    const cls = (el.getAttribute('class') || '') + ' ' + (el.getAttribute('id') || '');
    if (cls.trim() && JUNK_CLASS.test(cls) && el !== root) {
      el.setAttribute('data-wb-skip', '1');
      return;
    }
    // 整页级的页头/页脚容器（只认精确的 id/class，避免误伤 post-header 之类）
    const idAttr = el.getAttribute('id') || '';
    const clsAttr = el.getAttribute('class') || '';
    if (el !== root && (/^(header|footer|topbar|navbar|nav|sidebar|menu)$/i.test(idAttr) ||
        /^(header|footer|topbar|navbar|nav|sidebar|menu)$/i.test(clsAttr.trim()))) {
      el.setAttribute('data-wb-skip', '1');
      return;
    }
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) < 0.05) {
      el.setAttribute('data-wb-skip', '1');
    }
  }

  const all = [root].concat(Array.from(root.querySelectorAll('*')));
  const fontSizes = [];

  for (const el of all) {
    if (el.tagName === 'IMG') {
      // 懒加载站点（微信公众号、知乎等）把真图放在 data-src，src 只是 1px 占位图。
      const lazy = el.getAttribute('data-src') || el.getAttribute('data-original') ||
        el.getAttribute('data-actualsrc') || el.getAttribute('data-lazy-src') ||
        el.getAttribute('data-original-src') || '';
      let src = el.currentSrc || el.src || '';
      if ((!src || src.startsWith('data:')) && lazy) src = lazy;
      if (src) el.setAttribute('data-wb-src', abs(src));
      let nw = el.naturalWidth || 0, nh = el.naturalHeight || 0;
      if (nw <= 1 || nh <= 1) { nw = 0; nh = 0; }
      const r = el.getBoundingClientRect();
      el.setAttribute('data-wb-size', [nw, nh, Math.round(r.width), Math.round(r.height)].join(','));
      continue;
    }
    if (el === root || el.tagName === 'BODY' || el.tagName === 'HTML') continue;

    const cs = getComputedStyle(el);
    // 文档查看器（飞书/语雀/一堂这类）常把标题渲染成带字号的普通 div/span，
    // 记下直接含文字的元素字号，后面按字号反推标题层级。
    const hasDirectText = Array.from(el.childNodes).some(function (n) {
      return n.nodeType === 3 && n.nodeValue && n.nodeValue.trim();
    });
    if (hasDirectText) {
      const fs = parseFloat(cs.fontSize) || 0;
      // 虚拟列表收集时已经记录过原始字号，别用离屏容器里的样式覆盖它
      const existing = parseFloat(el.getAttribute('data-wb-fs') || '0');
      const use = existing || fs;
      if (use) {
        if (!existing) el.setAttribute('data-wb-fs', String(use));
        fontSizes.push(use);
      }
    }

    // 类名里带层级（docx-heading1-block / heading2 / h3）就直接记下来
    if (!el.hasAttribute('data-wb-h')) {
      const hm = /(?:docx-)?heading([1-6])|(?:^|\s)h([1-6])(?:\s|$)/i.exec((el.className || '').toString());
      if (hm) el.setAttribute('data-wb-h', hm[1] || hm[2]);
    }
    const bg = cs.backgroundImage;
    if (bg && bg !== 'none' && bg.indexOf('url(') >= 0) {
      const r = el.getBoundingClientRect();
      if (r.width >= 240 && r.height >= 120 && clean(el.innerText).length < 10) {
        const m = /url\(["']?([^"')]+)["']?\)/.exec(bg);
        if (m && m[1]) {
          const img = document.createElement('img');
          img.setAttribute('data-wb-src', abs(m[1]));
          img.setAttribute('data-wb-size', '0,0,' + Math.round(r.width) + ',' + Math.round(r.height));
          if (el.parentNode) el.parentNode.insertBefore(img, el.nextSibling);
          el.setAttribute('data-wb-skip', '1');
          continue;
        }
      }
    }
    markJunk(el);
  }

  // 出现次数最多的字号即正文基准；明显大于它的短块按标题处理，
  // 具体层级按文档里实际出现的字号档位自适应（最大的档 → ##，其次 ### …）
  let bodyFs = 0;
  if (fontSizes.length) {
    const freq = new Map();
    for (const s of fontSizes) freq.set(s, (freq.get(s) || 0) + 1);
    let best = 0;
    for (const entry of freq) {
      if (entry[1] > best) { best = entry[1]; bodyFs = entry[0]; }
    }
  }
  const headingsAll = fontSizes.filter(function (s) {
    return bodyFs && s >= bodyFs * 1.28;
  });
  const sizeFreq = new Map();
  for (const s of headingsAll) sizeFreq.set(s, (sizeFreq.get(s) || 0) + 1);
  // 只出现一两次的大字号通常是封面标题/标语，不参与层级阶梯
  let headingSizes = Array.from(new Set(headingsAll.filter(function (s) {
    return (sizeFreq.get(s) || 0) >= 3;
  }))).sort(function (a, b) { return b - a; }).slice(0, 4);
  if (!headingSizes.length) {
    headingSizes = Array.from(new Set(headingsAll)).sort(function (a, b) { return b - a; }).slice(0, 3);
  }

  function levelForSize(fs) {
    const i = headingSizes.indexOf(fs);
    if (i >= 0) return Math.min(2 + i, 5);
    // 阶梯里没有这个字号（例如离屏重组时补记的字号）：退回按比例判断
    if (!bodyFs) return 0;
    const r = fs / bodyFs;
    if (r >= 1.9) return 2;
    if (r >= 1.5) return 3;
    if (r >= 1.28) return 4;
    return 0;
  }

  function headingLevel(el, text) {
    if (!bodyFs || !text || text.length > 60) return 0;
    if (/[。！？；]$/.test(text) || text.indexOf('。') >= 0) return 0; // 正文句子不当标题
    let fs = parseFloat(el.getAttribute('data-wb-fs') || '0') || 0;
    for (const d of Array.from(el.querySelectorAll('[data-wb-fs]'))) {
      const v = parseFloat(d.getAttribute('data-wb-fs')) || 0;
      if (v > fs) fs = v;
    }
    return fs ? levelForSize(fs) : 0;
  }

  // ─── Clone ───────────────────────────────────────────────────────────────

  const clone = root.cloneNode(true);
  for (const bad of Array.from(clone.querySelectorAll('[data-wb-skip]'))) bad.remove();

  const lines = [];
  const images = [];
  const seenImgs = new Set();
  const seenLines = new Set();
  const sinkRef = { fn: null, capture: null };

  function sink(line) {
    const t = String(line).replace(/\s+$/, '');
    if (!t.trim()) return;
    const key = t.trim();
    if (seenLines.has(key)) return;
    seenLines.add(key);
    lines.push(t);
  }
  sinkRef.fn = sink;

  function push(line) { sinkRef.fn(line); }

  function addImage(el) {
    const src = el.getAttribute('data-wb-src') || '';
    if (!src) return;
    if (src.startsWith('data:image/svg')) return;
    if (src.startsWith('about:') || src.startsWith('chrome')) return;
    const alt = clean(el.getAttribute('alt') || '');
    const size = (el.getAttribute('data-wb-size') || '0,0,0,0').split(',').map(Number);
    const nw = size[0] || 0, nh = size[1] || 0, rw = size[2] || 0, rh = size[3] || 0;
    const wAttr = parseInt(el.getAttribute('width') || '0', 10);
    const hAttr = parseInt(el.getAttribute('height') || '0', 10);
    const wEff = nw || rw || wAttr, hEff = nh || rh || hAttr;
    if (wEff && hEff && wEff <= 64 && hEff <= 64) return;
    if (wEff && hEff && wEff * hEff < 10000) return; // 图标、二维码边角料
    if (IMG_SKIP.test(src) && wEff < 200) return;
    if (seenImgs.has(src)) return;
    seenImgs.add(src);
    const idx = images.length + 1;
    images.push({ idx: idx, src: src, alt: alt, w: wEff, h: hEff });
    push('{{IMG_' + pad4(idx) + '}}');
  }

  function inline(node, opts) {
    opts = opts || {};
    const skip = opts.skip || new Set();
    let s = '';
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        s += esc(child.nodeValue.replace(/\s+/g, ' '));
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = child.tagName;
      if (skip.has(tag)) continue;
      if (tag === 'BR') { s += '\n'; continue; }
      if (tag === 'IMG') continue;
      if (tag === 'STRONG' || tag === 'B') {
        const inner = inline(child, opts);
        s += inner.trim() ? '**' + inner + '**' : inner;
        continue;
      }
      if (tag === 'EM' || tag === 'I') {
        const inner = inline(child, opts);
        s += inner.trim() ? '*' + inner + '*' : inner;
        continue;
      }
      if (tag === 'DEL' || tag === 'S' || tag === 'STRIKE') {
        const inner = inline(child, opts);
        s += inner.trim() ? '~~' + inner + '~~' : inner;
        continue;
      }
      if (tag === 'CODE') {
        const c = (child.textContent || '').replace(/\s+/g, ' ').replace(/`/g, '\u02cb');
        if (c) s += '`' + c + '`';
        continue;
      }
      if (tag === 'A') {
        const href = child.getAttribute('href') || '';
        const t = inline(child, opts).trim();
        if (!t) continue;
        if (!href || href.startsWith('javascript:') || href.startsWith('#')) { s += t; continue; }
        s += '[' + t + '](' + abs(href) + ')';
        continue;
      }
      s += inline(child, opts);
    }
    return s;
  }

  function hasBlockChild(el) {
    for (const c of el.children) {
      if (BLOCK_TAGS.has(c.tagName)) return true;
    }
    return false;
  }

  function imgOnly(el) {
    if (!el.querySelector('img')) return false;
    return clean(el.textContent).length < 3;
  }

  function walkList(list, ordered, depth) {
    let i = 1;
    for (const li of list.children) {
      if (li.tagName !== 'LI') continue;
      const skip = new Set(['UL', 'OL', 'PRE', 'TABLE', 'FIGURE', 'DIV', 'P', 'BLOCKQUOTE']);
      const t = clean(inline(li, { skip: skip })).replace(/\n+/g, ' ').trim();
      if (t) {
        push('  '.repeat(depth) + (ordered ? i + '. ' : '- ') + t);
      }
      for (const im of Array.from(li.children)) {
        if (im.tagName === 'IMG') addImage(im);
      }
      for (const nested of Array.from(li.children)) {
        if (nested.tagName === 'UL' || nested.tagName === 'OL') {
          walkList(nested, nested.tagName === 'OL', depth + 1);
        }
      }
      i++;
    }
  }

  function walkTable(table) {
    const rows = [];
    for (const tr of Array.from(table.querySelectorAll('tr'))) {
      const cells = [];
      for (const cell of Array.from(tr.children)) {
        if (cell.tagName !== 'TD' && cell.tagName !== 'TH') continue;
        cells.push(clean(inline(cell)).replace(/\n+/g, ' ').replace(/\|/g, '\\|'));
      }
      if (cells.length) rows.push(cells);
    }
    if (!rows.length) return;
    const width = Math.max.apply(null, rows.map(function (r) { return r.length; }));
    const norm = rows.map(function (r) {
      const c = r.slice();
      while (c.length < width) c.push('');
      return c;
    });
    push('| ' + norm[0].join(' | ') + ' |');
    push('| ' + norm[0].map(function () { return '---'; }).join(' | ') + ' |');
    for (let i = 1; i < norm.length; i++) push('| ' + norm[i].join(' | ') + ' |');
  }

  function walk(node, ctx) {
    ctx = ctx || { depth: 0 };
    for (const child of node.childNodes) {
      if (child.nodeType === 3) {
        const t = clean(child.nodeValue);
        if (t.length > 1) push(escLineStart(esc(t)));
        continue;
      }
      if (child.nodeType !== 1) continue;
      const tag = child.tagName;
      if (child.hasAttribute('data-wb-skip')) continue;

      // 块类名已经标明标题层级（飞书文档 docx-heading1-block / heading2 …），优先采信
      const forcedH = child.getAttribute('data-wb-h');
      if (forcedH) {
        let t = clean(inline(child)).replace(/\n+/g, ' ').trim();
        if (t.length < 2) t = clean(child.innerText || '').replace(/\n+/g, ' ').trim();
        if (t && normTitle(t) !== normTitle(pageTitle)) {
          const lvl = Math.min(6, (parseInt(forcedH, 10) || 1) + 1);
          push('#'.repeat(lvl) + ' ' + t);
        }
        for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
        continue;
      }

      if (/^H[1-6]$/.test(tag)) {
        const t = clean(inline(child)).replace(/\n+/g, ' ').trim();
        if (!t) continue;
        if (normTitle(t) === normTitle(pageTitle)) continue; // 标题已经在文档开头写过
        push('#'.repeat(Number(tag[1])) + ' ' + t);
        continue;
      }
      if (tag === 'P') {
        if (imgOnly(child)) {
          for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
          continue;
        }
        if (hasBlockChild(child)) { walk(child, ctx); continue; }
        const t = clean(inline(child)).trim();
        if (t.length > 1) {
          const lvl = normTitle(t) === normTitle(pageTitle) ? 0 : headingLevel(child, t);
          push(lvl ? '#'.repeat(lvl) + ' ' + t : escLineStart(t));
        }
        for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
        continue;
      }
      if (tag === 'FIGURE') {
        for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
        const cap = child.querySelector('figcaption');
        if (cap) {
          const t = clean(inline(cap)).trim();
          if (t) push('*' + t + '*');
        }
        continue;
      }
      if (tag === 'IMG') { addImage(child); continue; }
      if (tag === 'UL' || tag === 'OL') { walkList(child, tag === 'OL', ctx.depth || 0); continue; }
      if (tag === 'BLOCKQUOTE') {
        const capture = [];
        const savedFn = sinkRef.fn;
        sinkRef.fn = function (l) { capture.push(l); };
        walk(child, { depth: 0 });
        sinkRef.fn = savedFn;
        const body = capture.length ? capture : [clean(inline(child))];
        for (const l of body) {
          if (l.trim()) push('> ' + l);
        }
        continue;
      }
      if (tag === 'PRE') {
        const codeEl = child.querySelector('code') || child;
        const cls = (codeEl.getAttribute('class') || '') + ' ' + (child.getAttribute('class') || '');
        const m = /(?:language|lang)-([\w+#-]+)/.exec(cls);
        const raw = (codeEl.textContent || '').replace(/\s+$/, '');
        if (!raw.trim()) continue;
        const fence = raw.indexOf('```') >= 0 ? '~~~~' : '```';
        push(fence + (m ? m[1] : ''));
        for (const l of raw.split('\n')) push(l);
        push(fence);
        continue;
      }
      if (tag === 'TABLE') { walkTable(child); continue; }
      if (tag === 'HR') { push('---'); continue; }
      if (tag === 'DL') {
        for (const d of Array.from(child.children)) {
          if (d.tagName === 'DT') push('**' + clean(inline(d)) + '**');
          else if (d.tagName === 'DD') push(clean(inline(d)));
        }
        continue;
      }
      if (tag === 'DIV' || tag === 'SECTION' || tag === 'ARTICLE' || tag === 'MAIN' ||
          tag === 'HEADER' || tag === 'FOOTER' || tag === 'ASIDE' || tag === 'NAV' ||
          tag === 'ADDRESS' || tag === 'LI') {
        if (hasBlockChild(child)) { walk(child, ctx); continue; }
        if (imgOnly(child)) {
          for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
          continue;
        }
        const t = clean(inline(child)).trim();
        if (t.length > 1) {
          const lvl = normTitle(t) === normTitle(pageTitle) ? 0 : headingLevel(child, t);
          push(lvl ? '#'.repeat(lvl) + ' ' + t : escLineStart(t));
        }
        for (const im of Array.from(child.querySelectorAll('img'))) addImage(im);
        continue;
      }
      walk(child, ctx);
    }
  }

  walk(clone, { depth: 0 });

  // ─── Meta ────────────────────────────────────────────────────────────────

  function meta(sel) {
    const el = document.querySelector(sel);
    return el ? (el.getAttribute('content') || '').trim() : '';
  }

  const title = pageTitle;
  const site = meta('meta[property="og:site_name"]') || location.hostname;
  const author = meta('meta[name="author"]') || meta('meta[property="article:author"]') || meta('meta[name="twitter:creator"]');
  const timeEl = document.querySelector('time[datetime]');
  const published = meta('meta[property="article:published_time"]') || meta('meta[name="pubdate"]') ||
    (timeEl ? timeEl.getAttribute('datetime') : '');

  // ─── Assemble ────────────────────────────────────────────────────────────

  const body = lines.slice();
  if (body.length && clean(body[0]).replace(/^#+\s*/, '') === clean(title)) body.shift();

  const md = [];
  let inFence = false;
  for (let i = 0; i < body.length; i++) {
    const line = body[i];
    if (/^(```|~~~~)/.test(line.trim())) inFence = !inFence;
    md.push(line);
    if (inFence) continue;
    const next = body[i + 1] || '';
    const nextIsList = /^\s*([-*+]|\d+\.)\s/.test(next);
    const thisIsList = /^\s*([-*+]|\d+\.)\s/.test(line);
    const nextIsQuote = /^>\s/.test(next);
    const thisIsQuote = /^>\s/.test(line);
    const nextIsTable = /^\|/.test(next);
    const thisIsTable = /^\|/.test(line);
    if (nextIsList && thisIsList) continue;
    if (nextIsQuote && thisIsQuote) continue;
    if (nextIsTable && thisIsTable) continue;
    md.push('');
  }

  return {
    title: title,
    url: location.href,
    site: site,
    author: author,
    published: published,
    rootTag: root.tagName,
    bodyFontSize: bodyFs,
    debug: {
      rootClass: (root.className || '').toString().slice(0, 80),
      rootId: root.id || '',
      annotated: fontSizes.length,
      fsHistogram: fontSizes.reduce(function (acc, s) { acc[s] = (acc[s] || 0) + 1; return acc; }, {}),
    },
    blockCount: body.length,
    markdown: md.join('\n').replace(/\n{3,}/g, '\n\n').trim(),
    images: images
  };
})();
