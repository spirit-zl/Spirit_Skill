#!/usr/bin/env python3
"""dtk 采集客户端 —— 抖音内容采集的统一入口。

把自建 dtk 实例（Douyin_TikTok_Download_API v5）的 REST 接口包成一组子命令：
解析链接、读作者/作品/评论、回填归档、下载媒体、管理身份池，以及导出 API 清单。

只依赖 Python 标准库。凭据按以下顺序解析：
1. 环境变量 DTK_BASE_URL / DTK_API_KEY
2. ~/.workbuddy/data/douyin-data/credentials.env
3. 兜底 http://127.0.0.1:8000

用法示例：
    python3 dtk.py health
    python3 dtk.py parse "https://v.douyin.com/xxxxxxx/"
    python3 dtk.py posts "https://www.douyin.com/user/MS4wLjABAAAA..." --count 20
    python3 dtk.py --json video 7xxxxxxxxxxxxxxxxxx
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DTK_HOME = Path(os.environ.get("DTK_HOME", "~/.workbuddy/data/douyin-data")).expanduser()
CRED_FILE = DTK_HOME / "credentials.env"

DEFAULT_TIMEOUT = 60
#: 服务端 wait 参数的上限（见实例 openapi.json 里的 maximum）。
MAX_WAIT = 30


# --------------------------------------------------------------------------- #
# 凭据与传输
# --------------------------------------------------------------------------- #
def load_creds() -> dict[str, str]:
    """读取状态码 -> 凭据字典。环境变量优先于文件。"""
    creds: dict[str, str] = {}
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            creds[k.strip()] = v.strip()
    for k in ("DTK_BASE_URL", "DTK_API_KEY"):
        if os.environ.get(k):
            creds[k] = os.environ[k]
    creds.setdefault("DTK_BASE_URL", "http://127.0.0.1:8000")
    return creds


class ApiError(RuntimeError):
    def __init__(self, status: int, payload: object) -> None:
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {json.dumps(payload, ensure_ascii=False)[:400]}")

    @property
    def code(self) -> str:
        """服务端错误码（``UPSTREAM_RISK_CONTROL``、``RATE_LIMITED`` …）。

        拿不到就返回空串 —— 调用方拿它做分支，不该为一个 502 的网关页
        反手抛 ``AttributeError``。
        """
        return _field(self.payload, "code")

    @property
    def message(self) -> str:
        """服务端给的人话描述；没有就退回 ``HTTP <status>``。"""
        return _field(self.payload, "message") or f"HTTP {self.status}"

    @property
    def retry_after(self) -> float | None:
        """服务端建议的等待秒数（``IDENTITY_POOL_EXHAUSTED`` 等会带）。

        没有就给 ``None``，调用方据此决定"等一等"还是"就此打住"。
        """
        payload = self.payload
        if not isinstance(payload, dict):
            return None
        value = payload.get("retry_after")
        if value is None and isinstance(payload.get("details"), dict):
            value = payload["details"].get("retry_after")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        return float(value)


def _field(payload: object, key: str) -> str:
    if isinstance(payload, dict):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


#: 回环地址的请求必须绕开环境里的 HTTP_PROXY / HTTPS_PROXY。
#:
#: 沙箱、CI、公司网络常会给全局代理，而代理通常连不到 127.0.0.1：请求会被
#: 代理自己拒掉，报成 502 或 Connection refused，看起来像"服务没启动"。
#: 只对回环主机名关掉代理，其余地址照常走环境配置。
_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_OPENERS: dict[bool, urllib.request.OpenerDirector] = {}


def opener_for(url: str) -> urllib.request.OpenerDirector:
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host in _LOOPBACK
    if direct not in _OPENERS:
        handlers = [urllib.request.ProxyHandler({})] if direct else []
        _OPENERS[direct] = urllib.request.build_opener(*handlers)
    return _OPENERS[direct]


def http(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    creds: dict[str, str],
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[int, object]:
    """发一个请求，返回 (状态码, 解析后的响应体)。非 2xx 不抛异常。"""
    url = creds["DTK_BASE_URL"].rstrip("/") + path
    clean = {k: v for k, v in (params or {}).items() if v is not None}
    if clean:
        url += "?" + urllib.parse.urlencode(clean, doseq=True)
    headers = {"Accept": "application/json"}
    if creds.get("DTK_API_KEY"):
        headers["X-API-Key"] = creds["DTK_API_KEY"]
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with opener_for(url).open(req, timeout=timeout) as r:
            raw = r.read().decode()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode()
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:800]}
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        # 连接层失败（服务没起、端口不通、代理拦截）在这里就转成统一错误，
        # 让每个调用方都能说一句人话 —— 否则 urllib 的异常会一路穿透到
        # 用户面前，变成一段看不懂的堆栈。
        reason = getattr(e, "reason", None) or e
        raise ApiError(0, {
            "code": "SERVICE_UNREACHABLE",
            "message": (f"{creds['DTK_BASE_URL']} 连不上（{reason}）。"
                        f"服务没起？体检：python3 {Path(__file__).resolve().parent / 'doctor.py'} check"),
        })


def unwrap(status: int, payload: object) -> object:
    """取出统一信封里的 data；失败则抛出 ApiError。"""
    if isinstance(payload, dict) and "success" in payload:
        if payload.get("success"):
            return payload.get("data")
        raise ApiError(status, payload.get("error") or payload)
    if status >= 400:
        raise ApiError(status, payload)
    return payload


def poll_task(task_id: str, creds: dict[str, str], budget: int) -> object:
    """轮询异步任务直到 done/failed，最多 budget 秒。"""
    deadline = time.time() + budget
    last: object = None
    while time.time() < deadline:
        _, payload = http("GET", f"/api/v1/tasks/{task_id}", creds=creds)
        try:
            data = unwrap(200, payload)
        except ApiError:
            time.sleep(1.5)
            continue
        last = data
        state = (data or {}).get("state")
        if state in ("done", "failed", "cancelled", "error"):
            return data
        time.sleep(1.5)
    return last


def request(
    method: str,
    path: str,
    *,
    params: dict | None = None,
    body: dict | None = None,
    creds: dict[str, str],
    wait: float | None = None,
    poll: int = 180,
    no_poll: bool = False,
) -> object:
    """带异步处理的请求：wait 让服务端同步等；202 则（可选）轮询到完成。"""
    params = dict(params or {})
    if wait:
        params["wait"] = min(float(wait), float(MAX_WAIT))
    status, payload = http(method, path, params=params, body=body, creds=creds,
                           timeout=int(wait or 0) + DEFAULT_TIMEOUT)
    data = unwrap(status, payload)
    # 202 表示任务已排进队列，data 里是 task_id
    if status == 202 and isinstance(data, dict) and data.get("task_id"):
        if no_poll:
            return data
        done = poll_task(data["task_id"], creds, poll)
        if isinstance(done, dict) and done.get("data") is not None:
            return done["data"]
        return done
    return data


def num(v: str) -> int:
    try:
        return int(v)
    except ValueError:
        return v  # 交给服务端校验


def flatten_items(data: object) -> tuple[list[dict], str | None]:
    """从各种列表响应里取出条目与下一页游标。"""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)], None
    if not isinstance(data, dict):
        return [], None
    for key in ("items", "posts", "comments", "replies", "results", "users"):
        if isinstance(data.get(key), list):
            nxt = data.get("cursor") or data.get("next_cursor") or data.get("has_more")
            return [x for x in data[key] if isinstance(x, dict)], (
                str(data.get("cursor") or data.get("next_cursor")) if isinstance(nxt, str | int) else None
            )
    return [], None


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_health(a, creds):
    """三个端点各探一次。这是诊断命令 —— 服务不在时要如实显示，不是崩掉。"""
    out = {}
    for p in ("/healthz", "/readyz"):
        try:
            st, payload = http("GET", p, creds=creds, timeout=15)
            out[p] = payload if st == 200 else f"HTTP {st}"
        except ApiError as e:
            out[p] = e.message
    try:
        st, payload = http("GET", "/api/v1/system/status", creds=creds)
        if st == 200:
            try:
                out["/api/v1/system/status"] = unwrap(st, payload)
            except ApiError as e:
                out["/api/v1/system/status"] = str(e)
    except ApiError as e:
        out["/api/v1/system/status"] = e.message
    return out


def cmd_apilist(a, creds):
    st, spec = http("GET", "/openapi.json", creds=creds, params={"lang": a.lang} if a.lang else None)
    if st != 200 or not isinstance(spec, dict):
        raise ApiError(st, spec)
    paths = spec.get("paths", {})
    if a.out:
        Path(a.out).write_text(json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "openapi": spec.get("openapi"),
        "title": (spec.get("info") or {}).get("title"),
        "version": (spec.get("info") or {}).get("version"),
        "path_templates": len(paths),
        "operations": sum(
            1 for p in paths for m in paths[p] if m.lower() in ("get", "post", "put", "patch", "delete")
        ),
        "saved_to": a.out,
    }


def platform_path(creds, plat, suffix, **params):
    return f"/api/v1/{plat}{suffix}", params


def cmd_parse(a, creds):
    return request("POST", "/api/v1/parse", body={"url": a.url, "include_raw": a.include_raw},
                   creds=creds, wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_batch(a, creds):
    body = {"items": [{"url": u} for u in a.urls]}
    return request("POST", "/api/v1/tasks/batch", body=body, creds=creds, poll=a.poll, no_poll=a.no_poll)


def cmd_video(a, creds):
    tgt = a.target
    params = {"url": tgt} if "://" in tgt else {"aweme_id": tgt}
    return request("GET", f"/api/v1/{a.platform}/video", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_user(a, creds):
    tgt = a.target
    params = {"url": tgt} if "://" in tgt else {"sec_user_id": tgt}
    return request("GET", f"/api/v1/{a.platform}/user", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def _author_list(a, creds, suffix: str):
    tgt = a.target
    params: dict = {"count": num(a.count), "cursor": a.cursor}
    if "://" in tgt:
        params["url"] = tgt
    else:
        params["sec_user_id"] = tgt
    return request("GET", f"/api/v1/{a.platform}{suffix}", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_posts(a, creds):
    return _author_list(a, creds, "/user/posts")


def cmd_likes(a, creds):
    return _author_list(a, creds, "/user/likes")


def cmd_reposts(a, creds):
    return _author_list(a, creds, "/user/reposts")


def cmd_search(a, creds):
    """关键词搜索一个平台。

    返回的是**混合流**过滤后的作品页 —— 抖音在搜索结果里混着账号与直播间，
    服务端只保留带作品体的那些，所以 items 常少于请求的 count。
    """
    return request("GET", f"/api/v1/{a.platform}/search/posts",
                   params={"keyword": a.keyword, "count": num(a.count), "cursor": a.cursor},
                   creds=creds, wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_user_collections(a, creds):
    return _author_list(a, creds, "/user/collections")


def cmd_followers(a, creds):
    return _author_list(a, creds, "/user/followers")


def cmd_following(a, creds):
    return _author_list(a, creds, "/user/following")


def cmd_bookmarks(a, creds):
    return _author_list(a, creds, "/user/bookmarks")


def cmd_comments(a, creds):
    tgt = a.target
    params: dict = {"count": num(a.count), "cursor": a.cursor}
    params.update({"url": tgt} if "://" in tgt else {"aweme_id": tgt})
    return request("GET", f"/api/v1/{a.platform}/video/comments", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_replies(a, creds):
    params = {"comment_id": a.comment_id, "count": num(a.count), "cursor": a.cursor}
    return request("GET", f"/api/v1/{a.platform}/video/comments/replies", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_mix(a, creds):
    params = {"mix_id": a.mix_id, "count": num(a.count), "cursor": a.cursor}
    return request("GET", f"/api/v1/{a.platform}/mix/posts", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_collection(a, creds):
    params = {"collection_id": a.collection_id, "count": num(a.count), "cursor": a.cursor}
    return request("GET", f"/api/v1/{a.platform}/collection/posts", params=params, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


def cmd_backfill(a, creds):
    body = {"platform": a.platform, "author_id": a.author_id, "pages": a.pages}
    return request("POST", "/api/v1/archive/backfill", body=body, creds=creds,
                   poll=a.poll, no_poll=a.no_poll)


def cmd_archive(a, creds):
    params = {"platform": a.platform, "q": a.q, "author_uid": a.author_uid,
              "limit": a.limit, "cursor": a.cursor, "kind": a.kind, "stored": a.stored}
    return request("GET", "/api/v1/archive", params=params, creds=creds)


def cmd_archive_stats(a, creds):
    return request("GET", "/api/v1/archive/stats", creds=creds)


def cmd_download(a, creds):
    tgt = a.target
    body: dict = {"skip_existing": a.skip_existing}
    if "://" in tgt:
        body["url"] = tgt
    else:
        body["platform"] = a.platform
        body["content_id"] = tgt
    return request("POST", "/api/v1/downloads", body=body, creds=creds,
                   poll=a.poll, no_poll=a.no_poll)


def cmd_downloads(a, creds):
    return request("GET", "/api/v1/downloads", params={"limit": a.limit, "state": a.state}, creds=creds)


def cmd_task(a, creds):
    return request("GET", f"/api/v1/tasks/{a.task_id}", creds=creds)


def cmd_identity_list(a, creds):
    return request("GET", "/api/v1/admin/identities", creds=creds)


def cmd_identity_import(a, creds):
    jar = Path(a.file).expanduser().read_text(encoding="utf-8").strip()
    body = {
        "platform": a.platform,
        "cookies": jar,
        "user_agent": a.ua,
        "language": a.language,
        "timezone": a.timezone,
        "dry_run": a.dry_run,
    }
    return request("POST", "/api/v1/admin/identities/import", body=body, creds=creds)


def cmd_identity_test(a, creds):
    return request("POST", f"/api/v1/admin/identities/{a.identity_id}/test", body={},
                   creds=creds, poll=a.poll)


def cmd_call(a, creds):
    body = json.loads(a.body) if a.body else None
    params = dict(kv.split("=", 1) for kv in (a.param or []))
    return request(a.method.upper(), a.path, params=params, body=body, creds=creds,
                   wait=a.wait, poll=a.poll, no_poll=a.no_poll)


# --------------------------------------------------------------------------- #
# 输出
# --------------------------------------------------------------------------- #
ITEM_FIELDS = (
    # 内容类
    "aweme_id", "id", "desc", "create_time", "nickname", "unique_id",
    "sec_user_id", "uid", "digg_count", "comment_count", "share_count", "play_count",
    # 身份池 / 任务类
    "platform", "state", "authenticated", "source", "consecutive_fails",
    "last_used_at", "session", "cursor",
)


def fmt_value(v: object) -> str:
    """把字段值压成一行，嵌套对象只显示要点。"""
    if isinstance(v, dict):
        if "verdict" in v:
            return f"{v.get('cookie')}:{v.get('verdict')}"
        return ",".join(f"{k}={v[k]}" for k in list(v)[:3])
    if isinstance(v, list):
        return f"[{len(v)} 项]"
    return str(v)[:60]


def summarize_item(it: dict) -> str:
    """单条目的一行摘要：内容类走专用格式，其余（身份池/任务等）走字段白名单。"""
    is_content = any(k in it for k in ("content_id", "aweme_id", "web_url")) or (
        "stats" in it and ("title" in it or "desc" in it))
    if is_content:
        parts = []
        cid = it.get("content_id") or it.get("aweme_id")
        if cid:
            parts.append(f"id={cid}")
        title = " ".join(str(it.get("title") or it.get("desc") or "").split())
        if title:
            parts.append(f"标题={title[:48]}")
        if it.get("created_at"):
            parts.append(f"发布={str(it['created_at'])[:10]}")
        stats = it.get("stats")
        if isinstance(stats, dict):
            keep = [(k.replace("_count", ""), stats[k]) for k in
                    ("digg_count", "comment_count", "collect_count", "share_count")
                    if stats.get(k) is not None]
            if keep:
                parts.append("互动=" + "/".join(f"{k}{v}" for k, v in keep))
        d = it.get("duration_ms")
        if d:
            s = int(d / 1000)
            parts.append(f"时长={s // 60}:{s % 60:02d}")
        if it.get("web_url"):
            parts.append(f"链接={it['web_url']}")
        if parts:
            return " | ".join(parts)
    row = {k: it[k] for k in ITEM_FIELDS if k in it}
    if not row:
        row = {k: v for k, v in list(it.items())[:6]}
    return " | ".join(f"{k}={fmt_value(v)}" for k, v in row.items())


def render(data: object, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    items, cursor = flatten_items(data)
    if items:
        print(f"条目 {len(items)} 条" + (f"，下一页游标 {cursor}" if cursor else ""))
        for it in items:
            print("  - " + summarize_item(it))
        extra = {k: v for k, v in (data or {}).items() if k not in (
            "items", "posts", "comments", "replies", "results", "users")} if isinstance(data, dict) else {}
        if extra:
            print("\n其余字段：")
            print(json.dumps(extra, ensure_ascii=False, indent=2)[:1500])
        return
    print(json.dumps(data, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="dtk.py",
        description="通过自建 dtk 实例采集抖音/TikTok 内容。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="例：python3 dtk.py parse \"https://v.douyin.com/xxx/\" --wait 30",
    )
    # --json 放在顶层与各子命令两处都能用。
    # 注意：Python 3.7+ 的子解析器会把「自己的默认值」回写到主命名空间，所以子命令侧
    # 必须用 SUPPRESS —— 不传时不占用该属性，顶层已给的 True 才不会被 False 覆盖。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                        help="输出原始 JSON 而不是摘要")
    p.add_argument("--json", action="store_true", help="输出原始 JSON 而不是摘要")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name, fn, help_, platform=True, wait=True, poll=True, no_poll=True):
        s = sub.add_parser(name, help=help_, parents=[common])
        s.set_defaults(fn=fn)
        if platform:
            s.add_argument("--platform", default="douyin", choices=["douyin", "tiktok"])
        if wait:
            s.add_argument("--wait", type=float, default=None,
                           help=f"让服务端同步等待最多 N 秒（上限 {MAX_WAIT}）")
        if poll:
            s.add_argument("--poll", type=int, default=180, help="异步任务轮询预算（秒，默认 180）")
        if no_poll:
            s.add_argument("--no-poll", action="store_true", help="不轮询，直接返回 task_id")
        return s

    add("health", cmd_health, "健康检查", platform=False, wait=False, poll=False, no_poll=False)

    s = add("apilist", cmd_apilist, "导出实例的 API 清单", platform=False,
            wait=False, poll=False, no_poll=False)
    s.add_argument("--out", help="把 openapi.json 存到这个路径")
    s.add_argument("--lang", choices=["zh", "en"], help="文档语言")

    s = add("parse", cmd_parse, "解析任意受支持的链接")
    s.add_argument("url")
    s.add_argument("--include-raw", action="store_true", help="附带平台原始返回")

    s = add("batch", cmd_batch, "批量解析多个链接")
    s.add_argument("urls", nargs="+")

    for name, fn, help_ in (
        ("video", cmd_video, "单个作品（视频/图集）"),
        ("user", cmd_user, "作者主页"),
    ):
        s = add(name, fn, help_)
        s.add_argument("target", help="链接或 ID")

    for name, fn, help_ in (
        ("posts", cmd_posts, "作者作品列表"),
        ("likes", cmd_likes, "作者点赞的作品"),
        ("reposts", cmd_reposts, "作者转发的作品"),
        ("user-collections", cmd_user_collections, "作者收藏夹"),
        ("followers", cmd_followers, "作者的粉丝"),
        ("following", cmd_following, "作者关注的人"),
        ("bookmarks", cmd_bookmarks, "作者收藏的作品"),
    ):
        s = add(name, fn, help_)
        s.add_argument("target", help="链接或 sec_user_id")
        s.add_argument("--count", default=20)
        s.add_argument("--cursor", default=None)

    s = add("search", cmd_search, "关键词搜索作品（仅抖音；需 dtk 补丁）")
    s.add_argument("keyword")
    s.add_argument("--count", default=20)
    s.add_argument("--cursor", default=None)

    s = add("comments", cmd_comments, "作品的评论")
    s.add_argument("target", help="链接或 aweme_id")
    s.add_argument("--count", default=20)
    s.add_argument("--cursor", default=None)

    s = add("replies", cmd_replies, "某条评论下的回复")
    s.add_argument("comment_id")
    s.add_argument("--count", default=20)
    s.add_argument("--cursor", default=None)

    s = add("mix", cmd_mix, "合集/播放列表内的作品")
    s.add_argument("mix_id")
    s.add_argument("--count", default=20)
    s.add_argument("--cursor", default=None)

    s = add("collection", cmd_collection, "某收藏夹内的作品")
    s.add_argument("collection_id")
    s.add_argument("--count", default=20)
    s.add_argument("--cursor", default=None)

    s = add("backfill", cmd_backfill, "把作者历史回填进归档库（采集入库）", wait=False)
    s.add_argument("author_id", help="sec_user_id 或作者 uid")
    s.add_argument("--pages", type=int, default=5)

    s = add("archive", cmd_archive, "检索已归档内容", wait=False, poll=False, no_poll=False)
    s.add_argument("--q", help="关键词")
    s.add_argument("--author-uid")
    s.add_argument("--kind")
    s.add_argument("--stored", help="是否已存媒体")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--cursor")

    add("archive-stats", cmd_archive_stats, "归档总量", platform=False,
        wait=False, poll=False, no_poll=False)

    s = add("download", cmd_download, "下载作品媒体（无水印）", wait=False)
    s.add_argument("target", help="链接或 content_id")

    s = add("downloads", cmd_downloads, "下载记录", platform=False,
            wait=False, poll=False, no_poll=False)
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--state")

    s = add("task", cmd_task, "查询任务状态", platform=False,
            wait=False, poll=False, no_poll=False)
    s.add_argument("task_id")

    add("identity-list", cmd_identity_list, "列出身份池", platform=False,
        wait=False, poll=False, no_poll=False)

    s = add("identity-import", cmd_identity_import, "导入登录态 cookie（身份池）",
            wait=False, poll=False, no_poll=False)
    s.add_argument("file", help="存放 cookie 的文本文件（Cookie 头 / JSON / name=value 均可）")
    s.add_argument("--ua", dest="ua", required=False, default=os.environ.get("DOUYIN_UA"),
                   help="导出 cookie 的那个浏览器的确切 User-Agent（缺失会拒绝入库）")
    s.add_argument("--language", default="zh-CN")
    s.add_argument("--timezone", default="Asia/Shanghai")
    s.add_argument("--dry-run", action="store_true", help="只体检不写入")

    s = add("identity-test", cmd_identity_test, "对身份发起一次真实探测", platform=False,
            wait=False)
    s.add_argument("identity_id")

    s = add("call", cmd_call, "直接调用任意接口", platform=False, wait=True)
    s.add_argument("method")
    s.add_argument("path")
    s.add_argument("body", nargs="?", help="JSON 请求体")
    s.add_argument("--param", action="append", help="查询参数 k=v，可重复")

    return p


def main() -> int:
    a = build_parser().parse_args()
    creds = load_creds()
    try:
        data = a.fn(a, creds)
    except ApiError as e:
        print(f"调用失败：{e.code + ' —— ' if e.code else ''}{e.message}", file=sys.stderr)
        if e.status == 401:
            print("提示：检查 credentials.env 里的 DTK_API_KEY 是否有效。", file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"连不上 {creds['DTK_BASE_URL']}：{e}", file=sys.stderr)
        print(f"提示：体检 python3 {Path(__file__).resolve().parent / 'doctor.py'} check；"
              "启动 $DTK_HOME/service.sh start", file=sys.stderr)
        return 2
    render(data, a.json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
