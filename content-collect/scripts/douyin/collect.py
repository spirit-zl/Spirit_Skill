#!/usr/bin/env python3
"""统一采集入口 —— 三种输入，一份 13 字段的作品列表。

输入是什么由内容自己决定，调用方不用先说：

* **账号主页链接** → 该账号的作品列表
* **作品链接**     → 这一条作品 + 该账号的其余作品
* **搜索关键词**   → 关键词的搜索结果

三种输入产出同一种东西：一组行，每行固定 13 个字段（见 :data:`FIELDS`）。
行可以直接交给 ``feishu_bitable.py`` 写进多维表格，也可以 ``--json`` 拿走
自己用。

只依赖标准库，传输层复用同目录的 ``dtk.py``。凭据解析顺序见 dtk.py 头部注释。

用法示例：
    python3 collect.py "https://www.douyin.com/user/MS4wLjABAAAA..."
    python3 collect.py "https://v.douyin.com/xxxxxxx/" --count 100
    python3 collect.py "操盘手" --json > rows.json
    python3 collect.py "操盘手" --json --out rows.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import dtk  # noqa: E402  （同目录，path 已在上一行铺好）
import cookie as cookies  # noqa: E402  （登录态：采集前自动确认，必要时自动抓取）

#: 这些错误码在说「登录态不行」，而不是「这条内容/这个账号有问题」。
#:
#: 遇到它们，值得去浏览器抓一份新 cookie 再试一次 —— 用户浏览器里明明登录着，
#: 池子里那份却过期或被判定成机器。只重试一次：刷新换的是身份，不是平台策略，
#: 第二次还是同样的错，说明问题不在 cookie。
IDENTITY_ERRORS = frozenset({
    "IDENTITY_POOL_EXHAUSTED",
    "UPSTREAM_RISK_CONTROL",
})

#: 作品列表的 13 个字段，顺序即默认展示顺序。
#:
#: 这组名字是**契约**：写进多维表格的列名与它逐字一致，而且下游按列名认列，
#: 所以名字可以复述、不可以改；列在表里的前后顺序则可以随便调。
FIELDS: tuple[str, ...] = (
    "账号名称",
    "账号主页链接",
    "搜索关键词",
    "发布日期",
    "作品标题",
    "作品标签",
    "作品文案",
    "点赞",
    "评论",
    "收藏",
    "分享",
    "作品链接",
    "视频链接",
)

#: 每页最多取多少条。
#:
#: 两个不同的数，别合并：抖音的作者作品列表吃到 50，但**搜索接口只到 30**。
#: 实测 count=30 是完整一页，count=31 返回的是空页 + has_more=false ——
#: 那不是"没有了"，是"要多了"，读起来却和"这个关键词没有结果"一模一样。
#: 服务端也按这个数拒绝（见 dtk 的 platforms/paging.py），两边保持一致。
PAGE_SIZE = 50
SEARCH_PAGE_SIZE = 30

#: 每页最少要这么多。
#:
#: 不是优化，是必需的：搜索接口对小 count 的反应同样是**返回空页**而不是少给
#: 几条。实测 count=1 时第二页一条都不给，看着像"没有更多了"。要少了等于要不到。
MIN_PAGE = 20

#: 这类错误是"现在不行、待会儿也许行"，服务端带 retry_after，值得等一次。
#:
#: 两个反例，都不在这里：
#:
#: * ``IDENTITY_POOL_EXHAUSTED`` —— 它带的 retry_after 是"同步等待上限"
#:   （配置值），不是身份的冷却剩余时间。身份被风控后按指数退避冷却
#:   （60 秒起、每次命中翻倍、上限 6 小时），等 10 秒毫无意义。
#: * ``UPSTREAM_RISK_CONTROL`` —— 等多久都会被再拦一次（实测搜索第二页
#:   等 3 分钟仍被拦），等待只是把身份反复送进冷却。
WAIT_AND_RETRY = frozenset({
    "RATE_LIMITED",              # 本地配额，retry_after 就是窗口剩余时间
    "ENDPOINT_CIRCUIT_OPEN",     # 接口熔断，retry_after 就是熔断时长
})

#: 等待的上限与重试的次数上限。都是小数字：翻页是尽力而为，
#: 让用户对着一个卡住的终端等半分钟，不如拿到手的数据加一句说明。
#: 只重试一次也是有依据的 —— 实测关键词搜索第二页等 10 秒再打仍被拦，
#: 多等一轮只是把身份多送进冷却一次。
MAX_WAIT_RETRY = 30.0
MAX_RETRIES = 1

#: 北京时间。平台给的是 UTC，而看表的人在中国。
BEIJING = timezone(timedelta(hours=8))

#: 一段文字里可能夹着链接（App 分享出来的整段文案就是这样）。
_URL_IN_TEXT = re.compile(r"https?://[^\s\u4e00-\u9fff，。；！？、（）【】]+")

#: 裸的 sec_user_id，用户也可能直接粘这个。
_SEC_UID = re.compile(r"^MS4wLjABAAAA[\w-]{10,}$")


class CollectError(RuntimeError):
    """采集无法继续，且原因不需要堆栈。"""


# --------------------------------------------------------------------------- #
# 输入识别
# --------------------------------------------------------------------------- #
def extract_url(text: str) -> str | None:
    """从输入里取出第一个链接，没有则 None。

    分享文案形如「7.43 复制打开抖音，看看【某某】https://v.douyin.com/xxx/」，
    直接整段粘进来是常规操作，所以先剥出链接再判断。
    """
    match = _URL_IN_TEXT.search(text)
    return match.group(0) if match else None


def classify(text: str) -> tuple[str, str]:
    """判断输入是哪一种，返回 ``(kind, value)``。

    ``kind`` 取 ``url`` / ``sec_user_id`` / ``keyword``。这里的判断故意只做
    "是不是链接"这一件事，链接到底是主页还是作品，交给服务端的 ``/parse``
    回答 —— 短链、分享文案、平台改版都归它管，客户端不该抄一份规则。
    """
    text = text.strip()
    if not text:
        raise CollectError("输入为空")
    url = extract_url(text)
    if url is not None:
        return "url", url
    if _SEC_UID.match(text):
        return "sec_user_id", text
    return "keyword", text


def target_of(text: str, creds: dict[str, str], platform: str = "douyin") -> dict[str, str]:
    """把输入解析成"要采谁/采什么"。

    返回的字典至少含 ``kind``（``author`` / ``content`` / ``keyword``），
    链接输入还含 ``sec_user_id`` 与 ``nickname`` 等。
    """
    how, value = classify(text)
    if how == "keyword":
        return {"kind": "keyword", "keyword": value, "input": text}

    if how == "sec_user_id":
        # 裸 ID 没有昵称，表名要用它，所以顺手把主页读回来。
        profile = patient(
            lambda: dtk.request(
                "GET", f"/api/v1/{platform}/user",
                params={"sec_user_id": value}, creds=creds, wait=dtk.MAX_WAIT, poll=180,
            ),
            label="读取账号主页",
        )
        author = profile if isinstance(profile, dict) else {}
        return {
            "kind": "author",
            "input": text,
            "sec_user_id": value,
            "nickname": author.get("nickname") or "",
            "author_url": author.get("web_url") or "",
        }

    parsed = patient(
        lambda: dtk.request("POST", "/api/v1/parse", body={"url": value}, creds=creds,
                            wait=dtk.MAX_WAIT, poll=180),
        label="解析链接",
    )
    if not isinstance(parsed, dict):
        raise CollectError(f"服务端没能解析这个链接：{value}")

    # 作者对象带 sec_uid，作品对象带 content_id —— 这是归一化契约里唯一
    # 稳定的一组区分点，不依赖哪个字段为 None。
    if parsed.get("sec_uid"):
        return {
            "kind": "author",
            "input": text,
            "url": value,
            "sec_user_id": parsed["sec_uid"],
            "nickname": parsed.get("nickname") or "",
            "author_url": parsed.get("web_url") or "",
        }
    if parsed.get("content_id"):
        author = parsed.get("author") or {}
        sec_uid = author.get("sec_uid")
        if not sec_uid:
            raise CollectError(
                f"这个作品没带作者信息，无法顺着它取账号的作品列表：{value}"
            )
        return {
            "kind": "content",
            "input": text,
            "url": value,
            "sec_user_id": sec_uid,
            "nickname": author.get("nickname") or "",
            "author_url": author.get("web_url") or "",
            "content": parsed,
        }
    raise CollectError(f"这个链接既不是作者也不是作品：{value}")


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def _page(payload: object) -> tuple[list[dict], str | None, bool]:
    if not isinstance(payload, dict):
        raise CollectError(f"列表响应不是对象：{type(payload).__name__}")
    items = [x for x in (payload.get("items") or []) if isinstance(x, dict)]
    cursor = payload.get("cursor")
    return items, (str(cursor) if cursor is not None else None), bool(payload.get("has_more"))


def author_posts_page(creds, platform, sec_user_id, cursor, count):
    payload = dtk.request(
        "GET", f"/api/v1/{platform}/user/posts",
        params={"sec_user_id": sec_user_id, "cursor": cursor, "count": count},
        creds=creds, wait=dtk.MAX_WAIT, poll=180,
    )
    return _page(payload)


def search_page(creds, platform, keyword, cursor, count):
    payload = dtk.request(
        "GET", f"/api/v1/{platform}/search/posts",
        params={"keyword": keyword, "cursor": cursor, "count": count},
        creds=creds, wait=dtk.MAX_WAIT, poll=180,
    )
    return _page(payload)


def page_size(limit: int | None, have: int, ceiling: int) -> int:
    """这一页要多少条。

    够用就好，但不少于 :data:`MIN_PAGE`（要少了等于要不到），
    也不超过 ``ceiling``（要多了等于没有结果）。
    """
    if limit is None:
        return ceiling
    return min(ceiling, max(limit - have, MIN_PAGE))


def patient(call, *, label: str):
    """执行一次取数调用；对"等一会儿就好"的错误，按服务端说的等一次再试。

    本地限流和接口熔断都会在错误体里带一个**可信的** ``retry_after``
    （窗口剩余时间、熔断时长），这类错是节拍不是失败。只等一次：第二次
    还是拦，说明不是节拍问题，继续等没有意义。哪些错误值得等、哪些不值得，
    见 :data:`WAIT_AND_RETRY` 的注释 —— 身份冷却不在其中。
    """
    attempt = 0
    while True:
        try:
            return call()
        except dtk.ApiError as exc:
            wait = exc.retry_after
            if (exc.code in WAIT_AND_RETRY and wait is not None and 0 < wait <= MAX_WAIT_RETRY
                    and attempt < MAX_RETRIES):
                attempt += 1
                print(f"  · {label}：{exc.code}，按服务端说的等 {wait:g} 秒后重试"
                      f"（第 {attempt} 次）", file=sys.stderr)
                time.sleep(wait)
                continue
            raise


def drain(fetch, *, limit: int | None, max_pages: int | None, ceiling: int,
          label: str) -> list[dict]:
    """把一个分页接口翻到底，返回原始条目。

    ``limit`` 是"要多少条作品"而不是"翻几页"：每页请求多少由剩余额度和
    接口自己的 ``ceiling`` 决定，取够了就不再发请求。``cursor`` 不前进时立即
    停 —— 那意味着服务端在让调用方原地打转，继续翻只会把同一个页面重复采回来。

    平台中途把某一页拦下时（风控、或身份全在冷却），已采到的部分照常返回并
    打印警告：前面几页是真的，把真实数据丢给一个异常比少给几页更糟。一页都
    没到手时不再兜底，原样抛出 —— 空结果配一个"被拦截"的事实，不能报成
    "采到了 0 条"。
    """
    items: list[dict] = []
    cursor: str | None = None
    pages = 0
    seen: set[str] = set()
    while True:
        if limit is not None and len(items) >= limit:
            break
        if max_pages is not None and pages >= max_pages:
            print(f"  ! {label}：达到 --max-pages={max_pages}，提前停止", file=sys.stderr)
            break
        try:
            batch, cursor, has_more = patient(
                lambda: fetch(cursor, page_size(limit, len(items), ceiling)),
                label=label,
            )
        except dtk.ApiError as exc:
            # 走到这里，说明"等一等"已经用过了还是不行。
            if not items:
                raise
            print(f"  ! {label}：第 {pages + 1} 页被拦下（{exc.code}）——"
                  f"停止翻页，已采到的 {len(items)} 条保留", file=sys.stderr)
            break
        pages += 1
        items.extend(batch)
        print(f"  · {label}：第 {pages} 页 +{len(batch)} 条（累计 {len(items)}）",
              file=sys.stderr)
        if not has_more or not batch:
            if not batch and limit is not None and len(items) < limit:
                # 要多了会被服务端当成"没有结果"，空页的两个原因得说清楚。
                print(f"  ! {label}：这一页是空的。可能是要多了（每页上限 {ceiling}），"
                      f"也可能确实到底了 —— 已采 {len(items)} 条，目标 {limit} 条",
                      file=sys.stderr)
            break
        if cursor is None or cursor in seen:
            print(f"  ! {label}：游标没有前进，停止翻页以免重复采集", file=sys.stderr)
            break
        seen.add(cursor)
    return items if limit is None else items[:limit]


# --------------------------------------------------------------------------- #
# 13 字段
# --------------------------------------------------------------------------- #
def published_at(value: object) -> str:
    """UTC 时间戳转北京时间 ``YYYY-MM-DD HH:MM:SS``；拿不到就留空。"""
    if not isinstance(value, str) or not value:
        return ""
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def video_url(item: dict) -> str:
    """作品的视频直链。

    抖音给的是**带签名的临时地址**，过一段时间就失效 —— 它记的是"采下来
    那一刻能播的地址"，不是永久链接。永久入口是「作品链接」。
    """
    media = item.get("media") or {}
    video = media.get("video") or {}
    url = video.get("url")
    if isinstance(url, str) and url:
        return url
    for candidate in video.get("urls") or []:
        if isinstance(candidate, str) and candidate:
            return candidate
    return ""


def profile_url(item: dict) -> str:
    author = item.get("author") or {}
    if author.get("web_url"):
        return str(author["web_url"])
    sec_uid = author.get("sec_uid")
    return f"https://www.douyin.com/user/{sec_uid}" if sec_uid else ""


def to_row(item: dict, *, keyword: str = "") -> dict:
    """一个归一化作品 → 一行 13 字段。"""
    stats = item.get("stats") or {}
    tags = [t for t in (item.get("tags") or []) if isinstance(t, str) and t]
    return {
        "账号名称": (item.get("author") or {}).get("nickname") or "",
        "账号主页链接": profile_url(item),
        "搜索关键词": keyword,
        "发布日期": published_at(item.get("created_at")),
        "作品标题": item.get("title") or "",
        "作品标签": "、".join(tags),
        "作品文案": item.get("description") or "",
        "点赞": stats.get("digg_count"),
        "评论": stats.get("comment_count"),
        "收藏": stats.get("collect_count"),
        "分享": stats.get("share_count"),
        "作品链接": item.get("web_url") or "",
        "视频链接": video_url(item),
    }


def dedupe(rows: list[dict]) -> list[dict]:
    """按作品链接去重，先出现的留下。

    作品链接是这批数据里唯一稳定且唯一的身份 —— 作品 id 藏在链接尾部，
    同一条作品从主页、搜索、作品链接三个入口进来，在这里合成一行。
    """
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        key = row.get("作品链接") or ""
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


def table_name(target: dict) -> str:
    """多维表格的命名。

    规则是「账号名称/搜索关键词」，但三种输入不会同时给出这两样：给的是
    账号（或作品）时表名就是账号名称，给的是关键词时表名是「搜索-关键词」。
    """
    if target["kind"] == "keyword":
        return f"搜索-{target['keyword']}"
    return target.get("nickname") or "未知账号"


# --------------------------------------------------------------------------- #
# 登录态
# --------------------------------------------------------------------------- #
def preflight(*, auto: bool, margin_days: int, quiet: bool) -> dict:
    """采集前确认「现在能采」，需要时自动从本机 Chrome 抓一份 cookie。

    为什么放在采集入口而不是让用户先跑 ``cookie.py``：cookie 失效的时刻是
    平台决定的，不是用户决定的。等到采集报 ``IDENTITY_POOL_EXHAUSTED`` 再
    让用户去手工刷新，等于每次都要用户先知道「今天该换 cookie 了」——
    而他浏览器里本来就登录着，这件事应该自己完成。
    """
    if not auto:
        return {"state": "skipped"}
    return cookies.ensure(auto=True, margin_days=margin_days, quiet=quiet)


def collect_with_refresh(*, text: str, creds, platform: str, limit: int | None,
                         max_pages: int | None, quiet: bool, allow_refresh: bool):
    """跑一次采集；撞上登录态类错误时，抓一份新 cookie 再试一次。

    只重试一次，而且只对 :data:`IDENTITY_ERRORS` 里的码 —— 关键词搜索第二页
    的 ``UPSTREAM_RISK_CONTROL`` 也在里面，那种情况换身份确实可能过去，但
    再不过就是平台在拦，多试没有意义。
    """
    def run() -> dict:
        return collect(text, creds=creds, platform=platform, limit=limit,
                       max_pages=max_pages, quiet=quiet)

    try:
        return run()
    except dtk.ApiError as exc:
        if not allow_refresh or exc.code not in IDENTITY_ERRORS:
            raise
        print(f"  · {exc.code}：像是登录态失效，从本机 Chrome 抓一份后重试一次",
              file=sys.stderr)
        try:
            cookies.refresh(keychain_timeout=cookies.AUTO_KEYCHAIN_TIMEOUT,
                            log=lambda m: print(f"    {m}", file=sys.stderr))
        except (cookies.CookieError, dtk.ApiError) as refresh_exc:
            print(f"  ! 自动获取 cookie 失败：{refresh_exc}", file=sys.stderr)
            raise exc from refresh_exc
        return run()


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def collect(text: str, *, creds, platform: str, limit: int | None,
            max_pages: int | None, quiet: bool = False) -> dict:
    def note(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    note(f"识别输入：{text}")
    target = target_of(text, creds, platform)
    kind = target["kind"]
    note(f"判定为：{kind}")

    rows: list[dict] = []
    keyword = ""

    if kind == "keyword":
        keyword = target["keyword"]
        note(f"搜索关键词「{keyword}」，逐页取到没有更多为止")
        # 作品链接那一列在搜索模式下也要能定位到人，所以先取原始条目再转行。
        items = drain(
            lambda cursor, want: search_page(creds, platform, keyword, cursor, want),
            limit=limit, max_pages=max_pages, ceiling=SEARCH_PAGE_SIZE,
            label=f"搜索 {keyword}",
        )
        rows = [to_row(item, keyword=keyword) for item in items]
    else:
        # 作品链接输入时先把它自己放进来：它可能已被删除、或是转发，
        # 不一定出现在作者的作品列表里，但用户明确指了它。
        if kind == "content":
            rows.append(to_row(target["content"], keyword=keyword))
        # 这里按 limit 取而不是按"还差几条"：目标作品多半本来就在作者的作品
        # 列表里，两条来源会去重成一条。少要一条会让 --count 5 只采到 4 条。
        items = drain(
            lambda cursor, want: author_posts_page(
                creds, platform, target["sec_user_id"], cursor, want),
            limit=limit, max_pages=max_pages, ceiling=PAGE_SIZE,
            label=target.get("nickname") or "作品列表",
        )
        rows.extend(to_row(item, keyword=keyword) for item in items)

    rows = dedupe(rows)
    if limit is not None:
        rows = rows[:limit]

    return {
        "input": text,
        "input_kind": kind,
        "platform": platform,
        "keyword": keyword,
        "account_name": target.get("nickname") or "",
        "account_url": target.get("author_url") or "",
        "table_name": table_name(target),
        "collected_at": datetime.now(UTC).astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "fields": list(FIELDS),
        "rows": rows,
    }


def render(plan: dict) -> str:
    """给人看的摘要：一张窄表 + 计数。完整数据用 --json。"""
    lines = [
        f"输入：{plan['input']}",
        f"判定：{plan['input_kind']}"
        + (f"（关键词：{plan['keyword']}）" if plan["keyword"] else "")
        + (f"（账号：{plan['account_name']}）" if plan["account_name"] else ""),
        f"表格名：{plan['table_name']}",
        f"采集时间：{plan['collected_at']}",
        f"作品数：{plan['count']}",
        "",
        "| # | 发布日期 | 账号 | 作品标题 | 赞 | 评 | 藏 | 转 | 作品链接 |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for index, row in enumerate(plan["rows"], 1):
        title = (row["作品标题"] or "").replace("|", "｜")
        if len(title) > 34:
            title = title[:34] + "…"
        lines.append(
            f"| {index} | {row['发布日期']} | {row['账号名称']} | {title} "
            f"| {row['点赞']} | {row['评论']} | {row['收藏']} | {row['分享']} "
            f"| {row['作品链接']} |"
        )
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="collect.py",
        description="三种输入（账号主页链接 / 作品链接 / 搜索关键词）统一采集为 13 字段作品列表。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            '  python3 collect.py "https://www.douyin.com/user/MS4wLjABAAAA..."\n'
            '  python3 collect.py "操盘手" --count 100 --json > rows.json\n'
        ),
    )
    p.add_argument("input", help="主页链接、作品链接或搜索关键词")
    p.add_argument("--platform", default="douyin", choices=["douyin", "tiktok"],
                   help="关键词搜索作用于哪个平台（链接输入由链接自己决定）")
    p.add_argument("--count", type=int, default=None,
                   help="最多采多少条作品；不传则不设上限，翻到没有更多为止")
    p.add_argument("--max-pages", type=int, default=None,
                   help="翻页上限，仅作保险绳（默认不限制）")
    p.add_argument("--out", help="把 JSON 结果写到这个路径")
    p.add_argument("--json", action="store_true", help="输出 JSON 而不是摘要")
    p.add_argument("--no-auto-cookie", action="store_true",
                   help="不自动检查/抓取登录态（默认会自动：浏览器登录着抖音就不用管 cookie）")
    p.add_argument("--cookie-margin-days", type=int, default=cookies.EXPIRY_MARGIN_DAYS,
                   help=f"登录态距到期不足这么多天就提前换一份（默认 {cookies.EXPIRY_MARGIN_DAYS}）")
    return p


def main() -> int:
    args = build_parser().parse_args()
    creds = dtk.load_creds()
    auto_cookie = not args.no_auto_cookie

    guard = preflight(auto=auto_cookie, margin_days=args.cookie_margin_days,
                      quiet=args.json)
    if guard.get("state") == "blocked":
        print(f"采集失败：没有可用的登录态（{guard.get('reason') or '原因未知'}）。", file=sys.stderr)
        print("  Chrome 里登录抖音后重跑即可；手动路径：python3 "
              f"{Path(__file__).resolve().parent / 'cookie.py'} refresh --test", file=sys.stderr)
        return 2

    try:
        plan = collect_with_refresh(
            text=args.input, creds=creds, platform=args.platform,
            limit=args.count, max_pages=args.max_pages, quiet=args.json,
            allow_refresh=auto_cookie,
        )
    except dtk.ApiError as exc:
        # 服务端的长 JSON 对用户没用，错误码加一句人话才是有用的部分。
        print(f"采集失败：{exc.code or exc.status} —— {exc.message}", file=sys.stderr)
        if exc.code == "IDENTITY_POOL_EXHAUSTED":
            # 这是最容易被误读成"服务坏了"的一种 —— 它其实在说"这个身份
            # 刚被风控，正在按指数退避冷却"，等 10 秒是不够的。
            print("  身份在风控冷却中（冷却时长随命中次数翻倍，最长 6 小时）。"
                  "稍后再试，或先换一个身份：dtk.py identity-list", file=sys.stderr)
            if auto_cookie:
                print("  （已经自动抓过一次 cookie 还是这样：说明拦的是这台机器的网络，"
                      "不是登录态 —— 等冷却，别连着重试）", file=sys.stderr)
        return 2
    except (urllib.error.URLError, OSError) as exc:
        # 连接层的兜底（正常路径上 dtk.http() 已把它转成 ApiError）。
        print(f"采集失败：连不上服务（{exc}）。", file=sys.stderr)
        print(f"  先体检：python3 {Path(__file__).resolve().parent / 'doctor.py'} check",
              file=sys.stderr)
        return 2
    except CollectError as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2

    payload = json.dumps(plan, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(payload + "\n", encoding="utf-8")
        print(f"已写入 {args.out}（{plan['count']} 条）", file=sys.stderr)
    print(payload if args.json else render(plan))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
