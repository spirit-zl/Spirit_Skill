#!/usr/bin/env python3
"""微信公众号文章抓取 / 解析 / 清洗核心（纯标准库）。

对外三个入口：
    fetch(url)      抓 HTML（先直连，失败再走缓存/备用 UA）
    parse(html)     解析出 标题 / 发表日期 / 公众号 / 作者 / 正文
    clean(text)     去掉正文里的广告、自我介绍、二维码引导等与内容无关的部分

设计要点：
- 不依赖第三方下载服务：mp.weixin.qq.com 的公开文章页可直接抓，带微信 UA 成功率最高。
- 解析失败不抛"成功"：没有标题就是失败，交给上层记 failed。
- 清洗规则集中在本文件底部，改规则只动一处。
"""

from __future__ import annotations

import html as htmllib
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))

# 微信内置浏览器 UA。桌面 UA 常被要求"请在微信客户端打开"，移动端 UA 最稳。
WECHAT_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Mobile/15E148 MicroMessenger/8.0.40(0x18002832) "
    "NetType/WIFI Language/zh_CN"
)
FALLBACK_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# 失效页特征：命中这些说明链接不可用，而不是"下载成功但内容为空"
DEAD_MARKERS = [
    ("该内容已被发布者删除", "文章已被发布者删除"),
    ("此内容因违规无法查看", "文章因违规被屏蔽"),
    ("该公众号已迁移", "公众号已迁移，链接失效"),
    ("此帐号已自主注销", "公众号已注销"),
    ("参数错误", "链接无效（服务端返回参数错误页）"),
    ("环境异常", "微信要求人机验证（环境异常），稍后重试"),
    ("请在微信客户端打开链接", "需要微信客户端才能打开"),
]


class WxError(RuntimeError):
    pass


# ────────────────────────────── 抓取 ──────────────────────────────

def _http_get(url: str, ua: str, timeout: int = 45) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Referer": "https://mp.weixin.qq.com/",
        },
    )
    # 回环地址绕开全局代理（企业网络里常见）
    opener = urllib.request.build_opener()
    if "127.0.0.1" in url or "localhost" in url:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as resp:
        raw = resp.read()
    for enc in ("utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "ignore")


def fetch(url: str, retries: int = 2) -> str:
    """抓文章 HTML。直连为主，UA 依次降级。"""
    last = None
    for attempt in range(retries + 1):
        for ua in (WECHAT_UA, FALLBACK_UA):
            try:
                h = _http_get(url, ua)
            except urllib.error.HTTPError as exc:
                last = f"HTTP {exc.code}"
                continue
            except Exception as exc:  # noqa: BLE001 - 网络错误统一降级
                last = f"{type(exc).__name__}: {exc}"
                continue
            if 'id="js_content"' in h or "js_content" in h:
                return h
            for marker, why in DEAD_MARKERS:
                if marker in h:
                    raise WxError(f"抓取失败：{why}")
            last = "页面里没有正文容器（可能是验证页或非文章页）"
        if attempt < retries:
            time.sleep(2 + attempt * 2)
    raise WxError(f"抓取失败：{last}")


# ────────────────────────────── 解析 ──────────────────────────────

def _meta(html: str, prop: str) -> str | None:
    m = re.search(
        r'<meta[^>]+property=["\']%s["\'][^>]+content=["\']([^"\']*)' % re.escape(prop),
        html,
        re.I,
    )
    if not m:
        m = re.search(
            r'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']%s["\']' % re.escape(prop),
            html,
            re.I,
        )
    return htmllib.unescape(m.group(1)).strip() if m else None


def _jsvar(html: str, name: str) -> str | None:
    # 兼容 var x = "y" 与 var x = htmlDecode("y") 两种写法
    m = re.search(
        r"var\s+%s\s*=\s*(?:htmlDecode\()?\s*(['\"])(.*?)\1" % re.escape(name), html
    )
    if m:
        return htmllib.unescape(m.group(2)).strip()
    return None


def extract_container(html: str) -> str:
    """截出 id=js_content 的 div（含嵌套 div 的正确收尾）。"""
    m = re.search(r'<div[^>]*id=["\']js_content["\']', html)
    if not m:
        raise WxError("解析失败：找不到正文容器 js_content")
    start = m.start()
    depth = 0
    for tag in re.finditer(r"<(/?)div\b[^>]*>", html[start:], re.I):
        if tag.group(1):
            depth -= 1
            if depth == 0:
                return html[start:start + tag.end()]
        else:
            depth += 1
    return html[start:]


def container_to_text(seg: str) -> str:
    seg = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", "", seg, flags=re.S | re.I)
    # 公众号名片 / 视频号卡片 / 小程序卡片：与正文无关，整块丢
    seg = re.sub(r"<mp-[a-z-]+\b[^>]*>.*?</mp-[a-z-]+>", "", seg, flags=re.S | re.I)
    seg = re.sub(r"<mp-[a-z-]+\b[^>]*/?>", "", seg, flags=re.I)
    seg = re.sub(r"<img\b[^>]*>", "\n【图】\n", seg, flags=re.I)
    seg = re.sub(r"<br\s*/?>", "\n", seg, flags=re.I)
    seg = re.sub(r"</(p|section|div|h[1-6]|li|blockquote|tr|figure|pre)\s*>", "\n", seg, flags=re.I)
    seg = re.sub(r"<[^>]+>", "", seg)
    text = htmllib.unescape(seg)
    text = text.replace("\u200b", "").replace("\xa0", " ").replace("\ufeff", "")
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n")]
    return "\n".join(ln for ln in lines if ln)


def parse_date(html: str) -> str | None:
    """返回 YYYY-MM-DD（北京时间）。"""
    cands = []
    for key in ("ct", "create_time", "msg_ct"):
        v = _jsvar(html, key)
        if v and v.isdigit():
            cands.append(int(v))
    m = re.search(r'id=["\']publish_time["\'][^>]*>\s*([0-9]{4}-[0-9]{2}-[0-9]{2})', html)
    if m:
        return m.group(1)
    m = re.search(r'var\s+publish_time\s*=\s*["\']([0-9]{4}-[0-9]{2}-[0-9]{2})', html)
    if m:
        return m.group(1)
    if cands:
        ts = min(cands) if min(cands) > 10**9 else max(cands)
        return datetime.fromtimestamp(ts, BEIJING).strftime("%Y-%m-%d")
    return None


def parse(html: str) -> dict:
    title = _meta(html, "og:title") or _jsvar(html, "msg_title")
    if not title:
        raise WxError("解析失败：取不到文章标题")
    # 公众号名：var nickname 最准；其次正文里插入的名片 data-nickname
    account = _jsvar(html, "nickname")
    if not account:
        m = re.search(r'data-nickname=["\']([^"\']+)', html)
        account = htmllib.unescape(m.group(1)) if m else ""
    m = re.search(r'id=["\']js_name["\'][^>]*>\s*([^<\s][^<]*)<', html)
    if not account and m:
        account = htmllib.unescape(m.group(1)).strip()
    author = _jsvar(html, "author") or ""
    body_raw = container_to_text(extract_container(html))
    return {
        "标题": title,
        "公众号": account or "",
        "作者": author,
        "日期": parse_date(html),
        "正文原文": body_raw,
        "链接": _meta(html, "og:url") or "",
    }


# ────────────────────────────── 清洗 ──────────────────────────────

# 出现在正文里就整行删掉的广告 / 引导语（不是截断，是逐行过滤）
NOISE_LINE_PATTERNS = [
    r"^点击上方.{0,12}关注",
    r"^点上方.{0,12}关注",
    r"^点击蓝字.{0,10}关注",
    r"^关注(我们|公众号|我)[，,。!！]?$",
    r"^(长按|扫描|扫码|识别).{0,10}(二维码|图片)",
    r"长按.{0,6}二维码",
    r"^请?(扫码|长按).{0,10}(关注|添加)",
    r"^如添加频繁",
    r"^\(?勿重复添加\)?$",
    r"^[（(]?或加微信",
    r"^微信扫一扫",
    r"^预览时标签不可点",
    r"^不喜欢$",
    r"^知道了$",
    r"^取消$",
    r"^允许$",
    r"^分析$",
    r"^使用小程序$",
    r"^微信扫一扫可打开此内容",
    r"^使用完整服务$",
    r"^关注该公众号$",
    r"^【图】$",
    r"^[—\-–]{0,3}(END|end|全文完|未完待续)[—\-–]{0,3}$",
    r"^本文(仅代表|系).{0,30}(观点|转载|授权)",
    r"^(免责声明|版权声明|特别声明)[：:]",
    r"^\s*(分享|点赞|在看|收藏)\s*$",
    r"^\s*[.。·•]{2,}\s*$",
    r"^(往期|推荐|精选)(文章|阅读|推荐|内容)",
    r"^(更多|相关)文章",
    r"^喜欢(此|这篇)文章",
    r"^欢迎(转发|分享|点赞)",
]

# 命中即从该行开始整体截断（只在其后 40% 区间生效，避免误伤正文里的一句话）
TAIL_CUT_PATTERNS = [
    r"^加微信",
    r"^备注[：:]",
    r"^不懂人性.{0,20}关注我",
    r"^不喂鸡汤",
    r"^来我朋友圈",
    r"^\d+[、.]\s*怎么.{2,10}$",
    r"^现在上车",
    r"^请长按",
    r"^识别二维码",
    r"^如添加频繁",
    r"^请扫码添加",
    r"^（或加微信",
    r"^[（(]?真正的干货",
    r"^[—\-–]{0,3}END[—\-–]{0,3}$",
    r"^往期(回顾|推荐)",
    r"^推荐阅读$",
    r"^精彩推荐",
    r"^长按识别",
    r"^点击.{0,6}关注.{0,10}公众号",
    r"^—{3,}$",
]


def _compile(pats):
    return [re.compile(p) for p in pats]


_NOISE_RE = _compile(NOISE_LINE_PATTERNS)
_TAIL_RE = _compile(TAIL_CUT_PATTERNS)


def clean(text: str, title: str = "") -> str:
    """去广告、去自我介绍、去重复标题。"""
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
    # 开头重复的标题行（公众号正文常把标题又排一遍）
    while lines and title and re.sub(r"\s+", "", lines[0]) == re.sub(r"\s+", "", title):
        lines.pop(0)

    # 尾部广告区：只在后半段找截断点
    n = len(lines)
    cut_at = None
    for idx in range(int(n * 0.5), n):
        if any(p.search(lines[idx]) for p in _TAIL_RE):
            cut_at = idx
            break
    if cut_at is not None:
        lines = lines[:cut_at]

    # 逐行过滤噪声
    kept = [ln for ln in lines if not any(p.search(ln) for p in _NOISE_RE)]

    # 连续【图】只留一个；去首尾【图】
    out = []
    for ln in kept:
        if ln == "【图】" and out and out[-1] == "【图】":
            continue
        out.append(ln)
    while out and out[0] == "【图】":
        out.pop(0)
    while out and out[-1] == "【图】":
        out.pop()

    return "\n".join(out).strip()


# ────────────────────────────── 归档 ──────────────────────────────

def safe_filename(name: str, limit: int = 80) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip(" .")
    return name[:limit] or "untitled"


def save_archive(html: str, meta: dict, root: str) -> str:
    """按 <公众号>/[YYYYMMDD]标题.html 归档，同名覆盖（重采幂等）。"""
    folder = os.path.join(root, safe_filename(meta.get("公众号") or "未知公众号"))
    os.makedirs(folder, exist_ok=True)
    date = (meta.get("日期") or "").replace("-", "")
    stem = f"[{date}]{safe_filename(meta['标题'])}" if date else safe_filename(meta["标题"])
    path = os.path.join(folder, stem + ".html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    return path
