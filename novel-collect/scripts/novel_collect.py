#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
novel_collect.py —— 小说全本采集：给一个任意章节链接，自动抓全本并合并成单个 Markdown。

用法：
    python3 novel_collect.py <章节链接> [-o 输出目录] [--workers 6]
    python3 novel_collect.py <章节链接> --refresh-toc      # 重新抓目录
    python3 novel_collect.py <章节链接> --check-only       # 只做自检，不写文件
    python3 novel_collect.py <章节链接> --limit 20         # 只抓前 20 章（试跑）

设计要点：
1. 站点适配器（Adapter）按域名自动选择，新增站点只需继承 BaseSite。
2. 每章落盘缓存（cache/<book>/<chapter_id>.txt），中断可续跑。
3. 正文清洗集中在 clean_text()，去掉「本章未完」「阅读网址」等站点广告。
4. 合并格式：`# 书名` + 每章 `## 章节名` + 纯文本段落，无其他 Markdown 语法。
5. 合并后自动自检（章节数/空章/字数分布/噪声残留）。
"""
import os
import re
import sys
import json
import time
import gzip
import base64
import argparse
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

DEFAULT_DATA = os.path.expanduser("~/.workbuddy/data/novel-collect")

# ---------------------------------------------------------------- 网络

def http_get(url, referer=None, timeout=30, retries=4):
    """带重试的 GET。空body/非200 都算失败并重试。"""
    last = None
    for i in range(retries):
        try:
            headers = {"User-Agent": UA, "Accept-Encoding": "gzip, deflate",
                       "Accept": "text/html,application/xhtml+xml,*/*"}
            if referer:
                headers["Referer"] = referer
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = r.read()
                enc = (r.headers.get("Content-Encoding") or "").lower()
                if "gzip" in enc:
                    data = gzip.decompress(data)
                elif "deflate" in enc:
                    try:
                        data = gzip.decompress(data)
                    except Exception:
                        import zlib
                        data = zlib.decompress(data, -zlib.MAX_WBITS)
                if not data or len(data) < 200:
                    raise IOError("empty body (%d bytes)" % len(data))
                return data
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def decode_html(data):
    """站点编码不一致：GBK 系优先按 gb18030，再退回 utf-8。"""
    head = data[:2048].lower()
    if b"charset=gbk" in head or b"charset=gb2312" in head or b"charset=gb18030" in head:
        order = ("gb18030", "utf-8")
    else:
        order = ("utf-8", "gb18030")
    for enc in order:
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 清洗

# 站点广告 / 截断提示，逐条正则删除（多为行尾内联，因此不按整行匹配）
NOISE_PATTERNS = [
    r'\[笔趣看\s*[^\]]*\]百度搜索[^。！\n]*',
    r'百度搜索[“"][^”"]{0,24}[”"]手机阅读[:：]\s*\S+',
    r'笔趣阁阅读网址[:：]\s*\S+',
    r'最新网址[:：]\s*\S+',
    r'最新最快更新[^。\n]*',
    r'手机版阅读网址[:：]\s*\S+',
    r'请记住本书首发域名[:：]\s*\S+',
    r'一秒记住[^。！\n]{0,30}',
    r'手机用户请浏览[^。\n]*',
    r'\(本章未完[,，]?\s*请翻页\)',
    r'（本章未完[,，]?\s*请翻页）',
    r'本章未完[，,]?\s*请?点击?下一页?继续?阅读[。.]?',
    r'本章未完[，,]\s*请翻页',
    r'请关闭浏览器的畅读模式[^。]*。?',
    r'原网页地址[:：]\s*\S+',
    r'章节报错\s*[\(（]\s*免登录\s*[\)）]',
    r'（未完待续）',
    r'\(未完待续\)',
    r'本站所有小说[^。]*。',
    r'加入书签[，,]?\s*方便下次阅读[!！]?',
    r'如果觉得《[^》]*》好看[^。]*。?',
    r'如果您中途有事离开[^。]*。?',
    r'88106推荐各位书友阅读[:：][^。\n]*',
    r'\(88106\s*\S*\)',
    r'（88106\s*\S*）',
    r'请把本站网址推荐给您的朋友吧[^。\n]*',
    r'\bread[0-9]+\(\);?',
    r'上一页\s*&nbsp;\s*[\S]{1,20}\s*&nbsp;\s*下一页',
    # 空章节占位页
    r'\(?（?1\s*/\s*1）?\)?$',
    # 裸「未完待续」也是站点标记（多在正文末尾）
    r'未完待续[。！？!?]?',
    # 中文/数字站点尾部的拉丁字母碎片（广告被截断后的残留）
    r'(?<=[。！？…”）)])\s*[A-Z]{1,3}\s*$',
]

_DOMAIN_ONLY = re.compile(r'^[\w\-.:/]*\.(?:com|net|cc|org|cn|info|la|tv|me)\b[\w\-./]*$', re.I)


def clean_text(text):
    """清洗单章正文，返回段落列表。输出保证可直接放进 Markdown 而不触发格式。"""
    if not text:
        return []
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    # 站点屏蔽词占位 <i>*</i> -> *
    text = re.sub(r'</?i>', '', text)
    # 连续反引号（乱码噪声）直接删
    text = re.sub(r'`+', '', text)
    for pat in NOISE_PATTERNS:
        text = re.sub(pat, '', text, flags=re.I)
    paras = []
    for raw in text.split('\n'):
        # 去缩进（全角空格 / 半角空格 / tab）
        s = raw.replace('\u3000', ' ').strip()
        s = re.sub(r'^[\s]+', '', s)
        if not s:
            continue
        if _DOMAIN_ONLY.match(s):
            continue
        # 纯符号行
        if re.fullmatch(r'[\s\*\-—_=·。，,\.!！?？…~"“”\'’]+', s):
            continue
        s = re.sub(r'\s+$', '', s)
        if len(s) <= 1 and not re.match(r'[\u4e00-\u9fa5]', s):
            continue
        # Markdown 无害化：* -> 全角＊（避免 **粗体** / *斜体* 被渲染）
        s = s.replace('*', '＊')
        # 行首的 # > | 等会被解析成结构，加全角转换
        if s[:1] in ('#', '>', '|'):
            s = '　' + s
        paras.append(s)
    return paras


def strip_html(fragment):
    fragment = re.sub(r'<script.*?</script>', '', fragment, flags=re.S | re.I)
    fragment = re.sub(r'<br\s*/?>', '\n', fragment, flags=re.I)
    fragment = re.sub(r'</p>', '\n', fragment, flags=re.I)
    fragment = fragment.replace('&nbsp;', ' ').replace('&amp;', '&')
    fragment = fragment.replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"')
    fragment = re.sub(r'<[^>]+>', '', fragment)
    return fragment


def tidy_tail(paras):
    """章末清理：去掉尾部广告碎片（悬空逗号、孤立拉丁字母），裁到最后一个句子边界。
    只在结尾明显不是句末时才动手，避免误删正常断句。"""
    if not paras:
        return paras
    ENDS = '。！？…”』」）)！?'
    last = paras[-1].rstrip()
    if last and last[-1] in ENDS:
        return paras
    # 去掉纯字母/符号尾巴
    trimmed = re.sub(r'[\sA-Za-z0-9\.\)\(（，,、：:；;…]+$', '', last)
    pos = -1
    for m in re.finditer('[%s]' % ENDS, trimmed):
        pos = m.end()
    if pos > 50:
        paras = paras[:-1] + [trimmed[:pos]]
    elif trimmed:
        paras = paras[:-1] + [trimmed]
    return paras


# ---------------------------------------------------------------- 站点适配器

class BaseSite(object):
    name = "base"
    hosts = ()

    def __init__(self, url, data_dir):
        self.url = url
        self.data_dir = data_dir

    @classmethod
    def matches(cls, url):
        host = urllib.parse.urlparse(url).netloc.lower()
        return any(host == h or host.endswith("." + h) for h in cls.hosts)

    def book(self):
        raise NotImplementedError

    def chapters(self):
        raise NotImplementedError

    def chapter_paras(self, ch):
        raise NotImplementedError


class Zhdu7Site(BaseSite):
    """zhdu7.net / 公益书屋 系。目录在 /txtlist/<book>.html，正文为 base64(str_decode)。"""
    name = "zhdu7"
    hosts = ("zhdu7.net",)

    def __init__(self, url, data_dir):
        BaseSite.__init__(self, url, data_dir)
        m = re.search(r'/txt/(\d+)/', url)
        if not m:
            raise ValueError("无法从链接解析书籍 ID: " + url)
        self.book_id = m.group(1)
        p = urllib.parse.urlparse(url)
        self.origin = "%s://%s" % (p.scheme, p.netloc)

    def book(self):
        toc_url = "%s/txtlist/%s.html" % (self.origin, self.book_id)
        html = decode_html(http_get(toc_url, referer=self.url))
        title = ""
        m = re.search(r'<h1[^>]*>\s*<a[^>]*>(.*?)</a>', html, re.S)
        if m:
            title = strip_html(m.group(1)).strip()
        if not title:
            m = re.search(r'<title>(.*?)</title>', html, re.S)
            if m:
                title = re.split(r'[章节目录,，]', strip_html(m.group(1)))[0].strip()
        author = ""
        m = re.search(r'<a href="[^"]*/author/\d+/"[^>]*>(.*?)</a>', html, re.S)
        if m:
            author = strip_html(m.group(1)).strip()
        total = 0
        m = re.search(r'共\s*(\d+)\s*）', html)
        if m:
            total = int(m.group(1))
        return {"title": title, "author": author,
                "toc_url": toc_url, "total": total}

    def chapters(self):
        toc_url = "%s/txtlist/%s.html" % (self.origin, self.book_id)
        html = decode_html(http_get(toc_url, referer=self.url))
        # 目录分页：/txtlist/<id>.html, /txtlist/<id>/2.html ...
        pages = [toc_url]
        opts = re.findall(r'<option value="(/txtlist/%s/\d+\.html)"' % self.book_id, html)
        pages.extend(self.origin + o for o in dict.fromkeys(opts))
        out, seen = [], set()
        for i, page in enumerate(pages):
            if i > 0:
                try:
                    html = decode_html(http_get(page, referer=toc_url))
                except Exception as e:
                    sys.stderr.write("[WARN] 目录页失败 %s: %s\n" % (page, e))
                    continue
            links = re.findall(
                r'href="(?:%s)?(/txt/%s/(\d+)\.html)"[^>]*>\s*<dd>(.*?)</dd>' %
                (re.escape(self.origin), self.book_id), html, re.S)
            for href, cid, title in links:
                if cid in seen:
                    continue
                seen.add(cid)
                out.append({"id": cid, "title": strip_html(title).strip(),
                            "url": self.origin + href})
        return out

    def chapter_paras(self, ch):
        url = ch["url"]
        paras, page, hops = [], url, 0
        while page and hops < 30:
            hops += 1
            html = decode_html(http_get(page, referer=url))
            body = self._extract(html)
            if body:
                paras.extend(clean_text(strip_html(body)))
            # 仅当下一页仍是本章（_N.html）才继续
            m = re.search(r'<a[^>]*href="([^"]+)"[^>]*id="next_url"[^>]*>(.*?)</a>', html, re.S)
            if not m:
                break
            href, label = m.group(1), strip_html(m.group(2))
            if "下一章" in label or "没有了" in label or not href.startswith("/txt/"):
                break
            page = self.origin + href
        return paras

    @staticmethod
    def _extract(html):
        # 优先 base64 版（不含广告段落）
        m = re.search(r'str_decode\("([^"]+)"\)', html)
        if m:
            try:
                return base64.b64decode(m.group(1)).decode("utf-8", "ignore")
            except Exception:
                pass
        m = re.search(r'<div id="booktxt"[^>]*>(.*?)</div>', html, re.S)
        if m:
            return m.group(1)
        m = re.search(r'<div id="chaptercontent"[^>]*>(.*?)</div>\s*<div', html, re.S)
        return m.group(1) if m else ""


class BlqukanSite(BaseSite):
    """wap.blqukan.cc / 笔趣看 系。目录 /4/<book>_<page>/，正文 #chaptercontent，GBK。"""
    name = "blqukan"
    hosts = ("blqukan.cc",)

    def __init__(self, url, data_dir):
        BaseSite.__init__(self, url, data_dir)
        m = re.search(r'^(https?://[^/]+)/(\d+)/(\d+)/', url)
        if not m:
            raise ValueError("无法从链接解析书籍路径: " + url)
        self.origin, self.g1, self.book_id = m.group(1), m.group(2), m.group(3)
        self.base = "/%s/%s" % (self.g1, self.book_id)

    def _toc_pages(self):
        html = decode_html(http_get(self.origin + self.base + "/", referer=self.url))
        opts = re.findall(r'<option value="(%s_\d+/)"' % re.escape(self.base), html)
        pages = [self.base + "/"] + list(dict.fromkeys(opts))
        return html, pages

    def book(self):
        html, _ = self._toc_pages()
        title = ""
        m = re.search(r'<dt class="name">(.*?)</dt>', html, re.S)
        if m:
            title = strip_html(m.group(1)).strip()
        if not title:
            m = re.search(r'<title>(.*?)(?:最新章节|无弹窗|_)', html, re.S)
            title = strip_html(m.group(1)).strip() if m else ""
        author = ""
        m = re.search(r'作者[:：]\s*([^<\s]+)', html)
        if m:
            author = m.group(1).strip()
        total = 0
        m = re.search(r'共\s*(\d+)\s*章', html)
        if m:
            total = int(m.group(1))
        return {"title": title, "author": author,
                "toc_url": self.origin + self.base + "/", "total": total}

    def chapters(self):
        html, pages = self._toc_pages()
        out, seen = [], set()
        for i, page in enumerate(pages):
            if i > 0:
                try:
                    html = decode_html(http_get(self.origin + page, referer=self.url))
                except Exception as e:
                    sys.stderr.write("[WARN] 目录页失败 %s: %s\n" % (page, e))
                    continue
            block = re.search(r'最新章节列表</dt>(.*?)</dl>', html, re.S)
            scope = block.group(1) if block else html
            for cid, title in re.findall(
                    r'href\s*=\s*"%s/(\d+)\.html"\s*>([^<]+)</a>' % re.escape(self.base), scope):
                if cid in seen:
                    continue
                seen.add(cid)
                out.append({"id": cid, "title": strip_html(title).strip(),
                            "url": "%s%s/%s.html" % (self.origin, self.base, cid)})
        return out

    def chapter_paras(self, ch):
        url = ch["url"]
        html = decode_html(http_get(url, referer=self.url))
        m = re.search(r'第\(1/(\d+)\)页', html)
        npages = int(m.group(1)) if m else 1
        chunks = [self._extract(html)]
        for k in range(2, npages + 1):
            sub = "%s%s/%s_%d.html" % (self.origin, self.base, ch["id"], k)
            try:
                chunks.append(self._extract(decode_html(http_get(sub, referer=url))))
            except Exception as e:
                sys.stderr.write("[WARN] %s 子页 %d 失败: %s\n" % (ch["id"], k, e))
        paras = []
        for c in chunks:
            paras.extend(clean_text(strip_html(c)))
        # 去掉首行重复的章节标题
        if paras and paras[0].replace(" ", "") == ch["title"].replace(" ", ""):
            paras = paras[1:]
        return paras

    @staticmethod
    def _extract(html):
        m = re.search(r'id="chaptercontent"[^>]*>(.*?)</div>', html, re.S)
        if not m:
            return ""
        txt = m.group(1)
        txt = re.sub(r'第\(\d+/\d+\)页', '', txt)
        return txt


class IbxswSite(BaseSite):
    """ibxsw.com / 不二读书 系。目录 /book/<id>.html；正文 #booktxt，含提示块噪声。
    特点：目录里可能有大段章节「无链接」（站点只给部分章节超链接），
    但其正文页真实存在，ID 落在相邻已知章节之间 —— 用 probe_missing() 区间探测补齐。"""
    name = "ibxsw"
    hosts = ("ibxsw.com",)

    TITLE_RE = re.compile(r'<title>(.*?)</title>', re.S)

    def __init__(self, url, data_dir):
        BaseSite.__init__(self, url, data_dir)
        m = re.search(r'/book/(\d+)/\d+\.html', url) or re.search(r'/book/(\d+)\.html', url)
        if not m:
            raise ValueError("无法从链接解析书籍 ID: " + url)
        self.book_id = m.group(1)
        p = urllib.parse.urlparse(url)
        self.origin = "%s://%s" % (p.scheme, p.netloc)

    def book(self):
        html = decode_html(http_get(self.origin + "/book/%s.html" % self.book_id,
                                    referer=self.url, timeout=25))
        title = ""
        m = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.S)
        if m:
            title = re.sub(r'\(.*?\)|（.*?）', '', strip_html(m.group(1))).strip()
        if not title:
            m = self.TITLE_RE.search(html)
            if m:
                title = re.split(r'[\(（]', strip_html(m.group(1)))[0].strip()
        author = ""
        m = re.search(r'/author/\d+/"[^>]*title="作者[:：](?:[^"]*)"[^>]*>(.*?)</a>', html, re.S)
        if not m:
            m = re.search(r'作者[:：]\s*<[^>]*>\s*([^<]{1,20})', html)
        if m:
            author = strip_html(m.group(1)).strip()
        total = 0
        m = re.search(r'共\s*(\d+)\s*章', html)
        if m:
            total = int(m.group(1))
        return {"title": title, "author": author,
                "toc_url": self.origin + "/book/%s.html" % self.book_id, "total": total}

    def chapters(self):
        html = decode_html(http_get(self.origin + "/book/%s.html" % self.book_id,
                                    referer=self.url, timeout=25))
        i = html.find("list-chapterAll")
        panel = html[i:] if i >= 0 else html
        dds = re.findall(r'<dd class="col-md-3">(.*?)</dd>', panel, re.S)
        out = []
        for k, d in enumerate(dds):
            m = re.search(r'/book/%s/(\d+)\.html' % self.book_id, d)
            title = strip_html(d).strip()
            cid = m.group(1) if m else None
            out.append({"id": cid, "title": title,
                        "url": ("%s/book/%s/%s.html" % (self.origin, self.book_id, cid))
                        if cid else None})
        return out

    # ---- 目录缺链接时的区间探测 ----
    def _page_meta(self, html):
        """(章节名, 当前页, 总页数)；空页返回 (None,0,0)。"""
        m = self.TITLE_RE.search(html)
        if not m:
            return None, 0, 0
        t = m.group(1).strip()
        mm = re.search(r'_([^_]+?)（(\d+)\s*/\s*(\d+)）_', t)
        if not mm:
            return None, 0, 0
        name = mm.group(1).strip()
        if not name:
            return None, 0, 0
        return name, int(mm.group(2)), int(mm.group(3))

    def probe_missing(self, chapters, workers=12):
        """对 id 为空的章节，在前后已知 id 之间探测真实页面，回填 id。"""
        anchors = [(k, int(c["id"])) for k, c in enumerate(chapters) if c["id"]]
        anchors.sort()
        if not anchors:
            return chapters
        cands = []
        for k, c in enumerate(chapters):
            if c["id"]:
                continue
            prev_id = next((i for s, i in reversed(anchors) if s < k), None)
            next_id = next((i for s, i in anchors if s > k), None)
            if prev_id is None or next_id is None:
                continue
            for cid in range(prev_id + 1, next_id):
                cands.append(cid)
        cands = list(dict.fromkeys(cands))
        if not cands:
            return chapters
        print("目录缺链接 %d 章，需探测 %d 个候选 ID…"
              % (sum(1 for c in chapters if not c["id"]), len(cands)))

        found = {}

        def work(cid):
            try:
                html = decode_html(http_get(
                    "%s/book/%s/%d.html" % (self.origin, self.book_id, cid),
                    referer=self.url, timeout=10))
                name, cur, tot = self._page_meta(html)
                return cid, name, tot
            except Exception:
                return cid, None, 0

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(work, c): c for c in cands}
            n = 0
            for fut in as_completed(futs):
                cid, name, tot = fut.result()
                n += 1
                if name:
                    found[cid] = name
                if n % 200 == 0:
                    print("  探测 %d/%d（命中 %d）" % (n, len(cands), len(found)))

        def nt(s):
            return re.sub(r'[\s\u3000]+', '', s or "")

        for c in chapters:
            if c["id"]:
                continue
            want = nt(c["title"])
            hit = [cid for cid, nm in found.items() if nt(nm) == want]
            if hit:
                cid = min(hit)
                c["id"] = str(cid)
                c["url"] = "%s/book/%s/%d.html" % (self.origin, self.book_id, cid)
        return chapters

    def chapter_paras(self, ch):
        cid = ch["id"]
        first = decode_html(http_get(ch["url"], referer=self.url, timeout=10))
        name, cur, tot = self._page_meta(first)
        if tot == 0:
            return []
        paras = clean_text(strip_html(self._body(first)))
        for p in range(2, tot + 1):
            try:
                h = decode_html(http_get("%s/book/%s/%s_%d.html"
                                         % (self.origin, self.book_id, cid, p),
                                         referer=ch["url"], timeout=10))
                paras.extend(clean_text(strip_html(self._body(h))))
            except Exception as e:
                sys.stderr.write("[WARN] %s 第%d页失败: %s\n" % (cid, p, e))
        # 去掉首行重复标题
        if paras and paras[0].replace(" ", "") == ch["title"].replace(" ", ""):
            paras = paras[1:]
        return tidy_tail(paras)

    @staticmethod
    def _body(html):
        """正文提取：**必须**优先用 str_decode 的 base64 版。
        本站在可见的 <div id="booktxt"> 里只给截断片段（约 350 字/页），
        完整内容在脚本 document.getElementById('booktxt').innerHTML = str_decode("<b64>")。
        直接用可见 div 会导致每章只剩三分之一、且结尾停在半句。"""
        best = ""
        for pat in (r'str_decode\("([^"]+)"\)', r"str_decode\('([^']+)'\)"):
            for m in re.finditer(pat, html):
                s = m.group(1)
                s = re.sub(r'[^A-Za-z0-9+/=]', '', s)
                s += '=' * (-len(s) % 4)
                try:
                    d = base64.b64decode(s).decode("utf-8", "ignore")
                except Exception:
                    continue
                if len(d) > len(best):
                    best = d
        i = html.find('<div id="booktxt">')
        j = html.find('<div class="col-md-4', i)
        vis = html[i:j] if (i > 0 and j > i) else ""
        # 注意：要按「纯文本长度」比较，不能比原始 HTML（base64 版带标签更短但正文更全）
        cand = best if len(re.sub(r'<[^>]+>', '', best)) >= len(re.sub(r'<[^>]+>', '', vis)) else vis
        cand = re.sub(r'<div data-id="(?:9997|9999)">.*?</div>', '', cand, flags=re.S)
        return cand


SITES = [Zhdu7Site, BlqukanSite, IbxswSite]


def pick_site(url, data_dir):
    for cls in SITES:
        if cls.matches(url):
            return cls(url, data_dir)
    raise SystemExit("暂不支持该站点：%s\n已支持：%s" %
                     (url, ", ".join(c.name for c in SITES)))


# ---------------------------------------------------------------- 主流程

def safe_name(name):
    return re.sub(r'[\\/:*?"<>|\s]+', "_", name).strip("_") or "novel"


def download_all(site, chapters, cache_dir, workers=6):
    os.makedirs(cache_dir, exist_ok=True)
    todo = [c for c in chapters
            if not (os.path.exists(os.path.join(cache_dir, c["id"] + ".txt"))
                    and os.path.getsize(os.path.join(cache_dir, c["id"] + ".txt")) > 0)]
    print("需下载 %d / 共 %d 章" % (len(todo), len(chapters)))
    fails, done = [], 0
    if not todo:
        return fails

    def work(ch):
        path = os.path.join(cache_dir, ch["id"] + ".txt")
        try:
            paras = site.chapter_paras(ch)
            if not paras:
                return ch["id"], False, "正文为空"
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write("\n".join(paras))
            os.replace(tmp, path)
            return ch["id"], True, "ok"
        except Exception as e:
            return ch["id"], False, str(e)[:80]

    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, c): c for c in todo}
        for fut in as_completed(futs):
            cid, ok, msg = fut.result()
            done += 1
            if not ok:
                fails.append((cid, msg))
                print("  [FAIL] %s %s" % (cid, msg))
            elif done % 100 == 0:
                print("  进度 %d/%d" % (done, len(todo)))
    return fails


def merge(book, chapters, cache_dir, out_path):
    """合并为单个 Markdown。缓存正文在写入前会再次清洗，保证旧缓存也干净。"""
    missing = []
    with open(out_path, "w", encoding="utf-8") as out:
        out.write("# %s\n\n" % book["title"])
        for ch in chapters:
            path = os.path.join(cache_dir, ch["id"] + ".txt")
            body = ""
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    body = f.read().strip()
            # 旧缓存可能未清洗/含旧噪声，统一再过一遍 clean_text
            if body:
                paras = tidy_tail(clean_text(body))
                body = "\n\n".join(paras)
            if not body:
                missing.append(ch["title"])
            out.write("## %s\n\n" % ch["title"])
            if body:
                out.write(body)
                out.write("\n\n")
    return missing


def self_check(out_path, book, chapters, cache_dir):
    """返回自检报告（字符串列表）与是否通过。"""
    rep, ok = [], True
    raw = open(out_path, encoding="utf-8").read()
    titles = re.findall(r'^## (.+)$', raw, re.M)
    h1 = re.findall(r'^# (.+)$', raw, re.M)

    rep.append("文件：%s（%.2f MB）" % (os.path.basename(out_path),
                                       os.path.getsize(out_path) / 1024.0 / 1024.0))
    rep.append("书名标题：# %s" % (h1[0] if h1 else "（缺失）"))
    if not h1:
        ok = False

    rep.append("章节标题数：%d（目录章节数：%d）" % (len(titles), len(chapters)))
    if len(titles) != len(chapters):
        ok = False
        rep.append("  !! 章节数不一致，缺失 %d 章" % (len(chapters) - len(titles)))

    # 逐章字数
    parts = re.split(r'^## .+$', raw, flags=re.M)
    bodies = [p.strip() for p in parts[1:]] if len(parts) > 1 else []
    lens = [len(b) for b in bodies]
    if lens:
        empty = [titles[i] for i, l in enumerate(lens) if l < 50]
        rep.append("总字数：%d（约 %.1f 万字）" % (sum(lens), sum(lens) / 10000.0))
        rep.append("单章字数：最短 %d / 平均 %d / 最长 %d"
                   % (min(lens), sum(lens) // len(lens), max(lens)))
        if empty:
            ok = False
            rep.append("  !! 过短章节 %d 个：%s" % (len(empty), "、".join(empty[:5])))
        else:
            rep.append("过短章节（<50字）：0")

    # 噪声残留
    residue = []
    for pat in (r'本章未完', r'阅读网址', r'biqukan\.com', r'blqukan\.cc',
                r'zhdu7\.net', r'请翻页', r'原网页地址', r'未完待续', r'百度搜索'):
        n = len(re.findall(pat, raw))
        if n:
            residue.append("%s × %d" % (pat, n))
    if residue:
        ok = False
        rep.append("  !! 噪声残留：%s" % "；".join(residue))
    else:
        rep.append("噪声残留：未发现")

    # 格式检查：正文只允许纯文本，标题只允许 # / ##
    # 判据——是否会出现渲染效果。原文自带的 [***] 屏蔽标记已转全角，不算格式。
    bad = []
    body_only = re.sub(r'^#{1,2} .+$', '', raw, flags=re.M)
    for name, pat in (("粗体(**)", r'\*\*'), ("斜体(*)成对", r'(?<![\w*])\*(?![\s*])[^*\n]{1,80}\*(?![\w*])'),
                      ("链接", r'\[[^\]\n]{0,60}\]\('), ("图片", r'!\['),
                      ("引用", r'^>\s'), ("代码块", r'`'),
                      ("分隔线", r'^\s*(?:---+|\*\*\*+|___+)\s*$'),
                      ("表格", r'^\s*\|.*\|'),
                      ("HTML标签", r'</?[a-zA-Z][^>\n]{0,40}>')):
        n = len(re.findall(pat, body_only, re.M))
        if n:
            bad.append("%s × %d" % (name, n))
    if bad:
        rep.append("  !! 存在额外格式：%s" % "；".join(bad))
        ok = False
    else:
        rep.append("格式：纯文本，仅 # / ## 为标题")

    # 空章节文件
    if os.path.isdir(cache_dir):
        badf = [f for f in os.listdir(cache_dir)
                if f.endswith(".txt") and os.path.getsize(os.path.join(cache_dir, f)) < 20]
        if badf:
            ok = False
            rep.append("  !! 空缓存文件 %d 个" % len(badf))

    return rep, ok


def main():
    ap = argparse.ArgumentParser(description="小说全本采集并合并为 Markdown")
    ap.add_argument("url", help="任意一个章节的链接")
    ap.add_argument("-o", "--outdir", default=os.getcwd(), help="输出目录")
    ap.add_argument("--data-dir", default=DEFAULT_DATA, help="缓存根目录")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0, help="只抓前 N 章（试跑）")
    ap.add_argument("--refresh-toc", action="store_true", help="忽略缓存的目录")
    ap.add_argument("--check-only", action="store_true")
    args = ap.parse_args()

    site = pick_site(args.url, args.data_dir)
    book = site.book()
    print("书名：%s ｜ 作者：%s ｜ 站点：%s" % (book["title"], book["author"] or "未知", site.name))

    book_dir = os.path.join(args.data_dir, safe_name(book["title"]))
    os.makedirs(book_dir, exist_ok=True)
    toc_file = os.path.join(book_dir, "chapters.json")
    if os.path.exists(toc_file) and not args.refresh_toc:
        with open(toc_file, encoding="utf-8") as f:
            chapters = json.load(f)
        print("复用章节清单：%d 章" % len(chapters))
    else:
        chapters = site.chapters()
        with open(toc_file, "w", encoding="utf-8") as f:
            json.dump(chapters, f, ensure_ascii=False, indent=1)
        print("已抓目录：%d 章" % len(chapters))
    if not chapters:
        raise SystemExit("章节清单为空，站点结构可能已变更")

    # 目录里若有大段章节缺链接（ibxsw 这类站常见），用 ID 区间探测补齐
    no_id = sum(1 for c in chapters if not c.get("id"))
    if no_id and hasattr(site, "probe_missing"):
        print("章节清单有 %d 章缺链接，开始探测补齐…" % no_id)
        chapters = site.probe_missing(chapters)
        with open(toc_file, "w", encoding="utf-8") as f:
            json.dump(chapters, f, ensure_ascii=False, indent=1)
        left = sum(1 for c in chapters if not c.get("id"))
        print("补齐后仍缺链接：%d 章" % left)
    chapters = [c for c in chapters if c.get("id")]

    if args.limit:
        chapters = chapters[:args.limit]

    cache_dir = os.path.join(book_dir, "cache")
    if not args.check_only:
        fails = download_all(site, chapters, cache_dir, args.workers)
        if fails:
            print("失败 %d 章，重试一次…" % len(fails))
            download_all(site, chapters, cache_dir, max(2, args.workers // 2))

    out_path = os.path.join(args.outdir, safe_name(book["title"]) + ".md")
    missing = merge(book, chapters, cache_dir, out_path)
    if missing:
        print("[WARN] %d 章正文缺失：%s" % (len(missing), "、".join(missing[:5])))

    rep, ok = self_check(out_path, book, chapters, cache_dir)
    print("\n===== 自检 =====")
    for line in rep:
        print(line)
    print("结论：" + ("通过 ✅" if ok else "存在问题 ⚠️"))
    print("输出：%s" % out_path)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
