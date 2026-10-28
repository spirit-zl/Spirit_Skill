#!/usr/bin/env python3
"""环境体检与引导安装 —— 换一台电脑时的第一个入口。

两条子命令：

    python3 doctor.py check      # 只读体检：逐项检查前置条件，缺什么、怎么补
    python3 doctor.py install    # 引导安装：把能自动化的环节逐步做掉

``check`` 永远不写任何东西，退出码 0 = 没有关键缺失、1 = 有。
``install`` 每一步都先探测「是不是已经做过」，做过就跳过；执行前打印将要
做什么并请求确认（``--yes`` 跳过确认；非交互环境必须显式加 ``--yes``）。

路径约定与 dtk.py / collect.py 一致：``DTK_HOME`` 环境变量（默认
``~/.workbuddy/data/douyin-data``）是技能的数据根。dtk 源码仓库的位置按
「环境变量 DTK_REPO > credentials.env 里的 DTK_REPO > 常见位置」解析，
``install`` 找不到时会克隆到 ``$DTK_HOME/repo``。

只依赖 Python 标准库。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------- #
# 常量与路径
# --------------------------------------------------------------------------- #
SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
#: 打印修复指引时用完整路径 —— 用户看到输出时多半不在这个目录里。
ME = Path(__file__).resolve()
DATA_HOME = Path(os.environ.get("DTK_HOME", "~/.workbuddy/data/douyin-data")).expanduser()
CRED_FILE = DATA_HOME / "credentials.env"
COMPOSE_FILE = DATA_HOME / "compose.yml"
RUN_DIR = DATA_HOME / "run"
STACK_SH = SCRIPTS / "stack.sh"
PATCH_FILE = SKILL_DIR / "references" / "patches" / "dtk-search-posts.patch"

REPO_URL = "https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git"
#: 补丁基于上游这个 commit 生成。clone 后必须 checkout 到它，否则补丁大概率
#: 打不上 —— 补丁是行级 diff，上游任何一次无关改动都可能让上下文对不上。
PINNED_COMMIT = "4f0bed8483c35a980315d9c7b3a1d4a1119ad2b2"
REPO_MIRRORS = (
    "https://ghfast.top/https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git",
    "https://gitclone.com/github.com/Evil0ctal/Douyin_TikTok_Download_API.git",
)

PG_IMAGE = "timescale/timescaledb-ha:pg17"
REDIS_IMAGE = "redis:8-alpine"
PG_CONTAINER = "dtk-data-postgres-1"
REDIS_CONTAINER = "dtk-data-redis-1"
PG_PORT = 55432
REDIS_PORT = 56379
PG_USER = "dtk"
PG_PASSWORD = "dtk_test_password"
PG_DATABASE = "dtk_test"

#: 端到端验证读一个公开账号的主页。选账号而不是单个作品：作品会被作者删掉
#: 或用久了失效，账号则长期存在 —— 验证目标不该自己烂掉。
VERIFY_SEC_UID = "MS4wLjABAAAA_XPOOXl2qR1TbXtRXRGDquVLEWU-729Gzii94rTTS57whbQ7HeeDxwt5G2ErFghV"

_COMPOSE_TEMPLATE = """\
# dtk 运行时依赖：Postgres 17 + TimescaleDB 与 Redis。
#
# 与仓库自带的 docker/compose.test.yml 的唯一区别，也是这个文件存在的理由：
# 那边的 Postgres 把数据放在 tmpfs 上，容器一停数据全部消失（身份池、归档、
# API Key、管理员账号一起没）。这里换成命名卷，数据留在磁盘上。
name: dtk-data

services:
  postgres:
    image: timescale/timescaledb-ha:pg17
    environment:
      POSTGRES_DB: {pg_db}
      POSTGRES_USER: {pg_user}
      POSTGRES_PASSWORD: {pg_password}
      POSTGRES_HOST_AUTH_METHOD: scram-sha-256
    ports:
      - "127.0.0.1:{pg_port}:5432"
    volumes:
      - dtk-pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U {pg_user} -d {pg_db}"]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: unless-stopped

  redis:
    image: redis:8-alpine
    # 队列与缓存。开 AOF 是为了让重启后任务状态还在，不再凭空消失。
    command: redis-server --appendonly yes --save ""
    ports:
      - "127.0.0.1:{redis_port}:6379"
    volumes:
      - dtk-redisdata:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 2s
      timeout: 3s
      retries: 30
    restart: unless-stopped

volumes:
  dtk-pgdata:
  dtk-redisdata:
"""

_DOCKERFILE = """\
# Local stand-in for `timescale/timescaledb-ha:pg17`.
#
# The compose files expect an image with PostgreSQL 17 and the TimescaleDB
# extension preinstalled. When that image is not reachable from this network,
# we build an equivalent one from the official `postgres:17` image plus the
# TimescaleDB packages published on packagecloud (architecture-matched), and
# tag it as the expected name so nothing else has to change.

FROM postgres:17

COPY pg-timescaledb.deb /tmp/
COPY pg-timescaledb-loader.deb /tmp/

# dpkg rather than apt: every dependency (postgresql-17, libc6, and the loader
# deb shipped alongside) is already present, so the build stays offline and
# does not have to reach a Debian mirror.
RUN set -eux; \\
    dpkg -i /tmp/pg-timescaledb-loader.deb; \\
    dpkg -i /tmp/pg-timescaledb.deb; \\
    rm -f /tmp/pg-timescaledb-loader.deb /tmp/pg-timescaledb.deb; \\
    conf="$(ls /usr/share/postgresql/*/postgresql.conf.sample | head -1)"; \\
    printf "\\n# Added by the local timescaledb build.\\nshared_preload_libraries = 'timescaledb'\\n" >> "$conf"; \\
    ls /usr/share/postgresql/*/extension/ | grep -i timescaledb | head -5
"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def sh(args, *, cwd=None, timeout=60, env=None):
    """跑一条命令，返回 (returncode, 合并后的 stdout+stderr)。"""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        p = subprocess.run(
            [str(a) for a in args], cwd=str(cwd) if cwd else None,
            timeout=timeout, env=full_env, capture_output=True, text=True,
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{args[0]}: 命令不存在"
    except subprocess.TimeoutExpired:
        return 124, f"{' '.join(str(a) for a in args)} 超时（{timeout}s）"


_LOOPBACK = frozenset({"127.0.0.1", "::1", "localhost"})
_OPENERS: dict[bool, urllib.request.OpenerDirector] = {}


def opener_for(url: str) -> urllib.request.OpenerDirector:
    """回环地址绕开环境代理（与 dtk.py 同一套处理）。"""
    host = urllib.parse.urlsplit(url).hostname or ""
    direct = host in _LOOPBACK
    if direct not in _OPENERS:
        handlers = [urllib.request.ProxyHandler({})] if direct else []
        _OPENERS[direct] = urllib.request.build_opener(*handlers)
    return _OPENERS[direct]


def http(method: str, url: str, *, body=None, headers=None, timeout=10,
         parse_json=True):
    """返回 (status, payload)。status=0 表示连接失败，payload 是错误说明。"""
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with opener_for(url).open(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
            status = resp.status
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        status = e.code
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}
    if not parse_json:
        return status, raw
    try:
        return status, json.loads(raw) if raw else None
    except json.JSONDecodeError:
        return status, raw


def load_creds() -> dict[str, str]:
    """与 dtk.py 相同的凭据解析：文件打底，环境变量覆盖。"""
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


def is_repo(path: Path) -> bool:
    return (path / "pyproject.toml").exists() and (path / "src" / "dtk").is_dir()


def find_repo() -> Path | None:
    """按 环境变量 > credentials.env > 常见位置 的顺序找 dtk 源码仓库。"""
    env = os.environ.get("DTK_REPO")
    if env:
        p = Path(env).expanduser()
        return p if is_repo(p) else None
    cred = load_creds().get("DTK_REPO")
    if cred:
        p = Path(cred).expanduser()
        if is_repo(p):
            return p
    candidates = [DATA_HOME / "repo", Path.home() / "douyin_api",
                  Path.home() / "src" / "douyin_api"]
    candidates += sorted(Path.home().glob("WorkBuddy/*/douyin_api"))
    for p in candidates:
        if is_repo(p):
            return p
    return None


def secret_key() -> str:
    """凭据加密主密钥（身份池的 cookie 用它加密）。"""
    return secrets.token_urlsafe(48)


# --------------------------------------------------------------------------- #
# 体检
# --------------------------------------------------------------------------- #
@dataclass
class Check:
    group: str
    name: str
    ok: bool | None          # True=通过 / False=缺失 / None=提示
    detail: str = ""
    fix: str = ""

    @property
    def mark(self) -> str:
        return "✓" if self.ok is True else ("·" if self.ok is None else "✗")


def _disp_width(text: str) -> int:
    """按终端显示宽度算长度：东亚宽字符占 2 列。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _docker_ok() -> tuple[bool, str]:
    if not shutil.which("docker"):
        return False, "未安装 docker 命令"
    rc, out = sh(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if rc != 0:
        return False, "docker daemon 未运行"
    return True, f"daemon 运行中（{out.strip().splitlines()[-1] if out.strip() else '?'}）"


def check_tools() -> list[Check]:
    out: list[Check] = []
    ok, detail = _docker_ok()
    out.append(Check("工具链", "容器运行时", ok, detail,
                     "装 OrbStack（推荐、轻）或 Docker Desktop；装好回来重跑本命令"))

    for cmd, name, fix in (
        ("git", "git", "xcode-select --install 或 brew install git"),
        ("uv", "uv", "curl -LsSf https://astral.sh/uv/install.sh | sh"),
    ):
        found = shutil.which(cmd)
        out.append(Check("工具链", name, bool(found),
                         "已安装" if found else "未安装", fix))

    lark = shutil.which("lark-cli")
    if not lark:
        out.append(Check("飞书", "lark-cli", None, "未安装 —— 只有要落飞书才需要",
                         "见技能 references/setup-notes.md；只采集可以先跳过"))
    else:
        rc, out_s = sh(["lark-cli", "auth", "status"], timeout=30)
        state, detail = None, "登录态未知"
        if rc == 0:
            try:
                data = json.loads(out_s)
                user = (data.get("identities") or {}).get("user") or {}
                if user.get("status") == "ready" and user.get("tokenStatus") == "valid":
                    state, detail = True, f"已登录（{user.get('userName', '?')}）"
                else:
                    detail = "未登录或 token 失效"
            except json.JSONDecodeError:
                detail = "auth status 输出无法解析"
        out.append(Check("飞书", "lark-cli", state, detail,
                         "lark-cli auth login（按提示扫码）"))

    chrome_cookies = Path.home() / "Library/Application Support/Google/Chrome/Default/Cookies"
    archived = DATA_HOME / "douyin_cookie.txt"
    if chrome_cookies.exists():
        out.append(Check("身份来源", "cookie 来源", True, "Chrome cookie 库可读（自动刷新用）"))
    elif archived.exists():
        out.append(Check("身份来源", "cookie 来源", None,
                         "没有 Chrome —— 用已存档的 cookie 也能跑",
                         "换 cookie 时才需要 Chrome 登录抖音"))
    else:
        out.append(Check("身份来源", "cookie 来源", False, "Chrome 与存档 cookie 都没有",
                         "见 setup-notes §5：手动导出 cookie 后跑 cookie.py"))
    return out


def check_docker_assets() -> list[Check]:
    out: list[Check] = []
    ok, detail = _docker_ok()
    if not ok:
        out.append(Check("容器", "镜像与容器", False, "docker 未就绪，无法检查",
                         "先解决上一组的问题"))
        return out

    for image, name in ((PG_IMAGE, "PG 镜像"), (REDIS_IMAGE, "Redis 镜像")):
        rc, _ = sh(["docker", "image", "inspect", image], timeout=30)
        out.append(Check("容器", name, rc == 0,
                         image if rc == 0 else f"{image} 不存在",
                         f"python3 {ME} install（第 1 步会准备它）"))

    for container, name in ((PG_CONTAINER, "PG 容器"), (REDIS_CONTAINER, "Redis 容器")):
        rc, out_s = sh(["docker", "inspect", "--format",
                        "{{.State.Health.Status}}", container], timeout=20)
        if rc == 0:
            status = out_s.strip().splitlines()[0] if out_s.strip() else "?"
            out.append(Check("容器", name, status == "healthy",
                             f"{container}: {status}",
                             f"cd {DATA_HOME} && docker compose up -d"))
        else:
            out.append(Check("容器", name, False, f"{container} 不存在",
                             f"cd {DATA_HOME} && docker compose up -d"))
    return out


def check_repo() -> list[Check]:
    out: list[Check] = []
    repo = find_repo()
    if repo is None:
        out.append(Check("dtk 仓库", "源码", False, "没找到（DTK_REPO 未设且常见位置没有）",
                         f"python3 {ME} install（会克隆到 {DATA_HOME}/repo）"))
        return out
    out.append(Check("dtk 仓库", "源码", True, str(repo)))

    if PATCH_FILE.exists():
        rc, _ = sh(["git", "apply", "--check", "--reverse", PATCH_FILE], cwd=repo, timeout=30)
        out.append(Check("dtk 仓库", "搜索补丁", rc == 0,
                         "已应用" if rc == 0 else "未应用（关键词搜索会 404）",
                         f"cd {repo} && git apply {PATCH_FILE}"))
    else:
        out.append(Check("dtk 仓库", "搜索补丁", False, f"补丁文件缺失：{PATCH_FILE}"))

    venv = repo / ".venv" / "bin" / "python"
    out.append(Check("dtk 仓库", "Python 环境", venv.exists(),
                     ".venv 就绪" if venv.exists() else ".venv 不存在",
                     f"cd {repo} && uv sync --all-extras"))

    env_file = repo / ".env"
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
        missing = [k for k in ("DTK_SECRET_KEY", "DTK_DATABASE_URL", "DTK_REDIS_URL")
                   if not re.search(rf"^{k}=\S", text, re.M)]
        out.append(Check("dtk 仓库", ".env", not missing,
                         "关键项齐全" if not missing else f"缺 {'、'.join(missing)}",
                         f"python3 {ME} install（第 6 步会补）"))
    else:
        out.append(Check("dtk 仓库", ".env", False, "不存在",
                         f"python3 {ME} install（第 6 步会生成）"))
    return out


def _service_probe(creds: dict[str, str]) -> dict:
    """探测 API 服务，返回一组事实供上层组装 Check。"""
    base = creds.get("DTK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    facts: dict = {"base": base}
    st, _ = http("GET", f"{base}/healthz", timeout=5)
    facts["alive"] = st == 200
    if not facts["alive"]:
        return facts
    st, raw = http("GET", f"{base}/openapi.json", timeout=15, parse_json=False)
    facts["patched"] = st == 200 and "search/posts" in str(raw)
    key = creds.get("DTK_API_KEY")
    if key:
        st, payload = http("GET", f"{base}/api/v1/system/status",
                           headers={"X-API-Key": key}, timeout=10)
        if st == 200 and isinstance(payload, dict) and payload.get("success"):
            facts["status"] = payload.get("data") or {}
        else:
            facts["key_bad"] = st
    return facts


def check_service(creds: dict[str, str]) -> list[Check]:
    out: list[Check] = []
    facts = _service_probe(creds)
    base = facts["base"]

    if not facts["alive"]:
        out.append(Check("服务", "API", False, f"{base} 连不上",
                         f"启动：{STACK_SH} up（没配好就先跑 install）"))
    else:
        out.append(Check("服务", "API", True, f"{base} 可达"))
        out.append(Check("服务", "搜索端点", bool(facts.get("patched")),
                         "已在（关键词搜索可用）" if facts.get("patched") else "缺失（HTTP 404）",
                         "上面『dtk 仓库 → 搜索补丁』修好后重启服务"))

        if "key_bad" in facts:
            out.append(Check("服务", "API Key", False,
                             f"服务拒绝了这个 Key（HTTP {facts['key_bad']}）",
                             f"检查 {CRED_FILE} 里的 DTK_API_KEY，或重跑 install 第 9 步"))
        elif "status" not in facts:
            # 没有 Key 就是不能采集 —— 所有数据请求都要带 X-API-Key，
            # 这跟 API 不可达是同一量级的缺失，不能只算"提示"。
            out.append(Check("服务", "API Key", False,
                             "凭据文件里没有 API Key（采集会 401）",
                             f"python3 {ME} install（第 9 步创建）"))
        else:
            data = facts["status"]
            comp = data.get("components") or {}
            out.append(Check("服务", "API Key", True, "有效"))
            for name, label in (("postgres", "Postgres"), ("redis", "Redis")):
                c = comp.get(name) or {}
                out.append(Check("服务", label, bool(c.get("ok")),
                                 f"ok（{c.get('latency_ms', '?')}ms）" if c.get("ok") else "异常",
                                 f"cd {DATA_HOME} && docker compose restart"))
            pool = ((data.get("pool") or {}).get("douyin")) or {}
            active = int(pool.get("active") or 0)
            out.append(Check("身份池", "可用身份", active > 0,
                             f"{active} 个活跃 / {pool.get('cooling', 0)} 个冷却中"
                             if active else "没有可用身份",
                             f"python3 {SCRIPTS / 'cookie.py'} refresh --test"))

    # worker 不依赖 API 是否可达，任何情况下都单独报它的实情。
    rc, out_s = sh(["pgrep", "-f", "dtk worker"], timeout=10)
    alive = rc == 0 and out_s.strip()
    out.append(Check("服务", "worker", bool(alive),
                     "运行中" if alive else "未运行（提交的任务会一直排队）",
                     f"{STACK_SH} up"))
    return out


def check_config(creds: dict[str, str]) -> list[Check]:
    folder = creds.get("FEISHU_FOLDER_TOKEN")
    return [Check("飞书", "目标文件夹", True if folder else None,
                  "已配置" if folder else "未配置 —— 不落飞书可以先不管",
                  f"python3 {SCRIPTS / 'feishu_bitable.py'} --set-folder <链接>")]


def run_check(as_json: bool) -> int:
    creds = load_creds()
    checks: list[Check] = []
    checks += check_tools()
    checks += check_docker_assets()
    checks += check_repo()
    checks += check_service(creds)
    checks += check_config(creds)

    passed = sum(1 for c in checks if c.ok is True)
    warned = sum(1 for c in checks if c.ok is None)
    missing = sum(1 for c in checks if c.ok is False)

    if as_json:
        print(json.dumps({
            "checks": [{"group": c.group, "name": c.name, "ok": c.ok,
                        "detail": c.detail, "fix": c.fix} for c in checks],
            "summary": {"passed": passed, "warned": warned, "missing": missing},
        }, ensure_ascii=False, indent=2))
        return 1 if missing else 0

    print(f"抖音链路体检 · content-collect/douyin　（数据目录 {DATA_HOME}）")
    print("─" * 58)
    groups: dict[str, list[Check]] = {}
    for c in checks:
        groups.setdefault(c.group, []).append(c)
    width = max((_disp_width(c.name) for c in checks), default=0) + 2
    for group, items in groups.items():
        print(f"\n{group}")
        for c in items:
            pad = " " * max(1, width - _disp_width(c.name))
            print(f"  {c.mark} {c.name}{pad}{c.detail}")
            if c.ok is not True and c.fix:
                print(f"    → {c.fix}")
    print()
    print(f"汇总：{passed} 项通过，{warned} 项提示，{missing} 项缺失")
    if missing:
        print(f"缺失项会让采集不可用。跑 python3 {ME} install 逐项补齐。")
    return 1 if missing else 0


# --------------------------------------------------------------------------- #
# 引导安装
# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    repo: Path | None = None
    yes: bool = False
    creds: dict[str, str] = field(default_factory=dict)


def confirm(ctx: Ctx, prompt: str) -> bool:
    if ctx.yes:
        return True
    if not sys.stdin.isatty():
        print("    （非交互环境：确认后请重跑并加 --yes）")
        return False
    try:
        ans = input(f"    {prompt} [Y/n] ").strip().lower()
    except EOFError:
        return False
    return ans in ("", "y", "yes")


def step_images(ctx: Ctx) -> bool:
    ok, detail = _docker_ok()
    if not ok:
        print(f"    ✗ 容器运行时不可用：{detail}")
        print("      装 OrbStack（推荐）或 Docker Desktop，装好重跑本命令。")
        return False

    rc, _ = sh(["docker", "image", "inspect", REDIS_IMAGE], timeout=30)
    if rc != 0:
        print(f"    拉取 {REDIS_IMAGE} …")
        rc, out = sh(["docker", "pull", REDIS_IMAGE], timeout=900)
        if rc != 0:
            rc2, _ = sh(["docker", "pull", f"docker.m.daocloud.io/library/{REDIS_IMAGE}"], timeout=900)
            if rc2 == 0:
                sh(["docker", "tag", f"docker.m.daocloud.io/library/{REDIS_IMAGE}", REDIS_IMAGE])
            else:
                print(f"    ✗ 拉取失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
                return False
        print("    ✓ Redis 镜像就绪")
    else:
        print("    ✓ Redis 镜像已就绪，跳过")

    rc, _ = sh(["docker", "image", "inspect", PG_IMAGE], timeout=30)
    if rc == 0:
        print("    ✓ Postgres+TimescaleDB 镜像已就绪，跳过")
        return True

    print("    Postgres+TimescaleDB 镜像不存在。先试官方源（约 3.9GB）…")
    if confirm(ctx, "尝试 docker pull 官方镜像？"):
        rc, out = sh(["docker", "pull", PG_IMAGE], timeout=1800)
        if rc == 0:
            print("    ✓ 官方镜像拉取成功")
            return True
        print("    官方源不可达，走本机自建（postgres:17 + TimescaleDB 官方 deb）。")

    return _build_timescale_image(ctx)


def _deb_version_key(version: str) -> tuple:
    """TimescaleDB 包的版本序：先比 `~` 前的数字段（2.30.2 > 2.22.0），
    并列时退化为整串字典序。够用即可 —— 索引里的排序不可信（实测第一个
    匹配是旧版本），必须自己挑最新的。"""
    nums = tuple(int(x) for x in re.findall(r"\d+", version.split("~", 1)[0]))
    return nums, version


def _deb_url(index: str, pkg: str) -> str | None:
    """在包索引里挑出指定包**版本最高**的那个 deb 下载地址。"""
    best: tuple[tuple, str] | None = None
    for block in index.split("\n\n"):
        if not re.search(rf"^Package: {re.escape(pkg)}$", block, re.M):
            continue
        m = re.search(r"^Filename: (.+)$", block, re.M)
        v = re.search(r"^Version: (.+)$", block, re.M)
        if not m:
            continue
        key = _deb_version_key(v.group(1).strip() if v else "")
        if best is None or key > best[0]:
            best = (key, "https://packagecloud.io/timescale/timescaledb/debian/"
                    + m.group(1).strip())
    return best[1] if best else None


def _build_timescale_image(ctx: Ctx) -> bool:
    """用 postgres:17 + TimescaleDB 官方 deb 自建等价镜像。"""
    print("    准备基础镜像 postgres:17 …")
    rc, _ = sh(["docker", "image", "inspect", "postgres:17"], timeout=30)
    if rc != 0:
        rc, _ = sh(["docker", "pull", "postgres:17"], timeout=900)
        if rc != 0:
            mirror = "docker.m.daocloud.io/library/postgres:17"
            print(f"    直连失败，改走镜像站 {mirror} …")
            rc, out = sh(["docker", "pull", mirror], timeout=900)
            if rc != 0:
                print(f"    ✗ 基础镜像拉取失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
                return False
            sh(["docker", "tag", mirror, "postgres:17"])

    rc, out = sh(["docker", "run", "--rm", "postgres:17", "sh", "-c",
                  '. /etc/os-release && echo "$VERSION_CODENAME"'], timeout=120)
    codename = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else "trixie"

    rc, out = sh(["docker", "version", "--format", "{{.Server.Arch}}"], timeout=30)
    arch = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else "arm64"

    print(f"    基础镜像：Debian {codename} / {arch}；查 TimescaleDB 包索引 …")
    index_url = (f"https://packagecloud.io/timescale/timescaledb/debian/dists/"
                 f"{codename}/main/binary-{arch}/Packages")
    st, index = http("GET", index_url, timeout=120, parse_json=False)
    if st != 200:
        print(f"    ✗ 拿不到包索引（HTTP {st}）：{index_url}")
        return False

    targets = (("timescaledb-2-loader-postgresql-17", "pg-timescaledb-loader.deb"),
               ("timescaledb-2-postgresql-17", "pg-timescaledb.deb"))
    build_dir = DATA_HOME / "build-pg-timescale"
    build_dir.mkdir(parents=True, exist_ok=True)
    for pkg, filename in targets:
        url = _deb_url(str(index), pkg)
        if not url:
            print(f"    ✗ 索引里找不到 {pkg}（{codename}/{arch}）")
            return False
        print(f"    下载 {pkg} …")
        rc, out = sh(["curl", "-sL", "-o", str(build_dir / filename), url], timeout=600)
        size = (build_dir / filename).stat().st_size if (build_dir / filename).exists() else 0
        if rc != 0 or size < 10000:
            print(f"    ✗ 下载失败（{size} 字节）")
            return False

    (build_dir / "Dockerfile").write_text(_DOCKERFILE, encoding="utf-8")
    print("    构建镜像（几分钟）…")
    rc, out = sh(["docker", "build", "--platform", f"linux/{arch}",
                  "-t", PG_IMAGE, "."], cwd=build_dir, timeout=1800)
    if rc != 0:
        print(f"    ✗ 构建失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
        return False
    print("    ✓ 自建镜像完成（标签与官方一致）")
    return True


def step_containers(ctx: Ctx) -> bool:
    ok, detail = _docker_ok()
    if not ok:
        print(f"    ✗ 容器运行时不可用：{detail}")
        return False
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    if not COMPOSE_FILE.exists():
        COMPOSE_FILE.write_text(_COMPOSE_TEMPLATE.format(
            pg_db=PG_DATABASE, pg_user=PG_USER, pg_password=PG_PASSWORD,
            pg_port=PG_PORT, redis_port=REDIS_PORT), encoding="utf-8")
        print(f"    已生成 {COMPOSE_FILE}（数据用命名卷，容器停了不丢）")

    healthy = True
    for c in (PG_CONTAINER, REDIS_CONTAINER):
        rc, out = sh(["docker", "inspect", "--format", "{{.State.Health.Status}}", c], timeout=20)
        if rc == 0 and "healthy" in out:
            print(f"    ✓ {c} 已健康，跳过")
            continue
        healthy = False
    if healthy:
        return True

    print("    启动 Postgres 与 Redis …")
    rc, out = sh(["docker", "compose", "up", "-d"], cwd=DATA_HOME, timeout=300)
    if rc != 0:
        print(f"    ✗ compose up 失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
        return False
    for _ in range(40):
        states = []
        for c in (PG_CONTAINER, REDIS_CONTAINER):
            rc, out = sh(["docker", "inspect", "--format", "{{.State.Health.Status}}", c], timeout=20)
            states.append(out.strip() if rc == 0 else "?")
        if all(s == "healthy" for s in states):
            print("    ✓ 两个容器都健康")
            return True
        time.sleep(2)
    print("    ✗ 容器未在 80 秒内变为健康，看日志：docker compose logs")
    return False


def step_repo(ctx: Ctx) -> bool:
    repo = ctx.repo or find_repo()
    if repo:
        print(f"    ✓ 已有仓库：{repo}")
    else:
        repo = DATA_HOME / "repo"
        if repo.exists():
            shutil.rmtree(repo, ignore_errors=True)
        print(f"    克隆 dtk 源码到 {repo} …")
        rc, out = sh(["git", "clone", REPO_URL, repo], timeout=900)
        if rc != 0:
            for mirror in REPO_MIRRORS:
                print(f"    直连失败，试镜像 {mirror.split('/')[2]} …")
                rc, out = sh(["git", "clone", mirror, repo], timeout=900)
                if rc == 0:
                    break
        if rc != 0:
            print(f"    ✗ 克隆失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
            return False
        print("    ✓ 克隆完成")

    rc, out = sh(["git", "rev-parse", "HEAD"], cwd=repo, timeout=30)
    head = out.strip().splitlines()[0] if out.strip() else ""
    if head.startswith(PINNED_COMMIT[:12]):
        print(f"    ✓ 已是补丁对应的版本（{PINNED_COMMIT[:12]}）")
    else:
        if not confirm(ctx, f"切换仓库到补丁对应的版本 {PINNED_COMMIT[:12]}？"):
            return False
        rc, out = sh(["git", "fetch", "origin", PINNED_COMMIT], cwd=repo, timeout=600)
        if rc != 0:
            rc, out = sh(["git", "fetch", "origin"], cwd=repo, timeout=900)
        rc, out = sh(["git", "checkout", "--force", PINNED_COMMIT], cwd=repo, timeout=120)
        if rc != 0:
            print(f"    ✗ 无法切到 {PINNED_COMMIT[:12]}：{out.strip()}")
            return False
        print(f"    ✓ 已切到 {PINNED_COMMIT[:12]}")
    ctx.repo = repo
    return True


def step_patch(ctx: Ctx) -> bool:
    if not ctx.repo or not PATCH_FILE.exists():
        print("    ✗ 仓库或补丁文件缺失")
        return False
    rc, _ = sh(["git", "apply", "--check", "--reverse", PATCH_FILE], cwd=ctx.repo, timeout=30)
    if rc == 0:
        print("    ✓ 补丁已应用，跳过")
        return True
    rc, out = sh(["git", "apply", PATCH_FILE], cwd=ctx.repo, timeout=60)
    if rc != 0:
        print(f"    ✗ 打补丁失败：{out.strip()}")
        print("      常见原因：仓库版本与补丁不匹配。确认已切到 "
              f"{PINNED_COMMIT[:12]}，或查看 references/patches/README.md。")
        return False
    print("    ✓ 补丁已应用（关键词搜索能力就位）")
    return True


def step_venv(ctx: Ctx) -> bool:
    if not ctx.repo:
        return False
    venv = ctx.repo / ".venv" / "bin" / "python"
    if venv.exists():
        print("    ✓ Python 环境已存在（.venv），跳过")
        return True
    if not shutil.which("uv"):
        print("    ✗ 缺 uv：curl -LsSf https://astral.sh/uv/install.sh | sh")
        return False
    print("    uv sync（首次约几分钟）…")
    rc, out = sh(["uv", "sync", "--all-extras"], cwd=ctx.repo, timeout=1800)
    if rc != 0:
        print("    直连 PyPI 失败，改走清华源重试 …")
        rc, out = sh(["uv", "sync", "--all-extras"], cwd=ctx.repo, timeout=1800,
                     env={"UV_DEFAULT_INDEX": "https://pypi.tuna.tsinghua.edu.cn/simple"})
    if rc != 0:
        print(f"    ✗ uv sync 失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
        return False
    print("    ✓ Python 环境就绪")
    return True


def step_env(ctx: Ctx) -> bool:
    if not ctx.repo:
        return False
    env_file = ctx.repo / ".env"
    if env_file.exists():
        text = env_file.read_text(encoding="utf-8")
        missing = [k for k in ("DTK_SECRET_KEY", "DTK_DATABASE_URL", "DTK_REDIS_URL")
                   if not re.search(rf"^{k}=\S", text, re.M)]
        if not missing:
            print("    ✓ .env 已就绪，跳过")
            return True
        print(f"    .env 存在但缺 {'、'.join(missing)} —— 补上（其余不动）")
        additions = []
        if "DTK_SECRET_KEY" in missing:
            additions.append(f"DTK_SECRET_KEY={secret_key()}")
        if "DTK_DATABASE_URL" in missing:
            additions.append("DTK_DATABASE_URL="
                             f"postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}@127.0.0.1:{PG_PORT}/{PG_DATABASE}")
        if "DTK_REDIS_URL" in missing:
            additions.append(f"DTK_REDIS_URL=redis://127.0.0.1:{REDIS_PORT}/0")
        with env_file.open("a", encoding="utf-8") as fh:
            fh.write("\n" + "\n".join(additions) + "\n")
        return True

    env_file.write_text(
        "# dtk 实例配置（doctor.py 生成）\n"
        f"DTK_SECRET_KEY={secret_key()}\n"
        f"DTK_DATABASE_URL=postgresql+asyncpg://{PG_USER}:{PG_PASSWORD}"
        f"@127.0.0.1:{PG_PORT}/{PG_DATABASE}\n"
        f"DTK_REDIS_URL=redis://127.0.0.1:{REDIS_PORT}/0\n"
        "DTK_BIND_HOST=127.0.0.1\n"
        "DTK_BIND_PORT=8000\n"
        "DTK_LOG_LEVEL=info\n"
        "DTK_LOG_JSON=false\n", encoding="utf-8")
    print("    ✓ 已生成 .env（SECRET_KEY 随机生成，与数据同生共死，别丢）")
    return True


def step_migrate(ctx: Ctx) -> bool:
    if not ctx.repo:
        return False
    rc, out = sh(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DATABASE,
                  "-tAc", "select count(*) from alembic_version"], timeout=30)
    if rc == 0 and out.strip().isdigit():
        print("    ✓ 数据库结构已就绪，跳过")
        return True
    print("    执行数据库迁移 …")
    rc, out = sh([".venv/bin/dtk", "migrate"], cwd=ctx.repo, timeout=300)
    if rc != 0:
        print(f"    ✗ 迁移失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
        return False
    print("    ✓ 迁移完成")
    return True


def step_serve(ctx: Ctx) -> bool:
    """启停统一交给技能自带的 stack.sh —— 不再生成第二套脚本。"""
    if not ctx.repo:
        return False
    if not STACK_SH.exists():
        print(f"    ✗ 找不到 {STACK_SH}")
        return False
    print("    启动全栈（容器 → 迁移 → API → worker，幂等）…")
    rc, out = sh([STACK_SH, "up"], cwd=DATA_HOME, timeout=900,
                 env={"DTK_REPO": str(ctx.repo)})
    for line in out.strip().splitlines():
        print(f"      {line}")
    if rc != 0:
        print("    ✗ stack.sh up 失败（见上方输出）")
        return False
    base = ctx.creds.get("DTK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    for _ in range(30):
        st, _ = http("GET", f"{base}/healthz", timeout=3)
        if st == 200:
            print(f"    ✓ 服务就绪（{base}）")
            return True
        time.sleep(2)
    print(f"    ✗ API 未在 60 秒内就绪，看 {RUN_DIR}/api.log")
    return False


def step_init(ctx: Ctx) -> bool:
    """建管理员与 API Key，写凭据文件。已初始化过则只补缺的部分。"""
    base = ctx.creds.get("DTK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")

    rc, out = sh(["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", PG_DATABASE,
                  "-tAc", "select count(*) from users"], timeout=30)
    users = int(out.strip()) if rc == 0 and out.strip().isdigit() else 0

    admin_user = ctx.creds.get("DTK_ADMIN_USER") or "admin"
    admin_pass = ctx.creds.get("DTK_ADMIN_PASSWORD")

    if users == 0:
        api_key_for_setup = None
        rc, out = sh(["docker", "exec", REDIS_CONTAINER, "redis-cli", "get", "setup:token"], timeout=30)
        token = out.strip().splitlines()[-1].strip() if rc == 0 and out.strip() else ""
        if not token or token in ("(nil)", ""):
            print("    ✗ 没拿到初始化令牌（Redis 里的 setup:token）")
            print("      手动方式见 references/setup-notes.md §4")
            return False
        if not admin_pass:
            admin_pass = secrets.token_urlsafe(12)
        st, payload = http("POST", f"{base}/api/setup/init", timeout=30,
                           body={"token": token, "username": admin_user, "password": admin_pass})
        if st != 200:
            print(f"    ✗ 建管理员失败（HTTP {st}）：{payload}")
            return False
        print(f"    ✓ 管理员已创建：{admin_user}（密码存到凭据文件）")
    else:
        print(f"    ✓ 已有管理员账号（{users} 个用户），跳过创建")

    existing_key = ctx.creds.get("DTK_API_KEY")
    if existing_key:
        st, payload = http("GET", f"{base}/api/v1/system/status",
                           headers={"X-API-Key": existing_key}, timeout=10)
        if st == 200 and isinstance(payload, dict) and payload.get("success"):
            print("    ✓ 现有 API Key 有效，跳过创建")
            return True  # 没有任何变更，不动凭据文件
        print("    · 现有 API Key 无效，重新创建一把")

    if not admin_pass:
        print("    · 无法自动登录（凭据文件里没有管理员密码）——手动创建 Key：")
        print("      curl -s -c /tmp/c.txt -X POST %s/api/v1/auth/login -H 'Content-Type: application/json'" % base)
        print("           -d '{\"username\":\"%s\",\"password\":\"<你的密码>\"}'" % admin_user)
        return False

    session_cookie = ""
    try:
        req = urllib.request.Request(
            f"{base}/api/v1/auth/login", method="POST",
            data=json.dumps({"username": admin_user, "password": admin_pass}).encode(),
            headers={"Content-Type": "application/json"})
        with opener_for(base).open(req, timeout=30) as resp:
            for header in resp.headers.get_all("Set-Cookie") or []:
                session_cookie += header.split(";", 1)[0] + "; "
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        print(f"    ✗ 登录失败（HTTP {exc.code}）：{body[:200]}")
        print("      如果密码不是凭据文件里这份，手动创建 Key 见 setup-notes §4")
        return False
    except (urllib.error.URLError, OSError) as exc:
        print(f"    ✗ 登录失败：{exc}")
        return False
    if not session_cookie:
        print("    ✗ 登录成功但没拿到会话 cookie（服务端行为变化？）")
        return False

    st, payload = http("POST", f"{base}/api/v1/admin/api-keys", timeout=30,
                       headers={"Cookie": session_cookie.strip("; ")},
                       body={"name": "douyin-data", "scopes": [
                           "douyin:read", "tiktok:read", "identity:manage",
                           "archive:read", "archive:export", "media:read",
                           "media:write", "admin"]})
    data = payload.get("data") if isinstance(payload, dict) else None
    new_key = None
    if isinstance(data, dict):
        new_key = data.get("key") or data.get("token") or data.get("secret")
    if st != 200 or not new_key:
        print(f"    ✗ 创建 API Key 失败（HTTP {st}）：{payload}")
        return False
    ctx.creds["DTK_API_KEY"] = new_key
    print("    ✓ API Key 已创建（只在创建时返回一次，已存进凭据文件）")
    _save_creds(ctx, admin_user, admin_pass)
    return True


def _save_creds(ctx: Ctx, admin_user: str, admin_pass: str | None) -> None:
    """把服务端凭据合并进 credentials.env（已有键保留，不覆盖用户改动）。"""
    DATA_HOME.mkdir(parents=True, exist_ok=True)
    existing: dict[str, str] = {}
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                existing[k.strip()] = v.strip()
    existing.update({
        "DTK_BASE_URL": ctx.creds.get("DTK_BASE_URL", "http://127.0.0.1:8000"),
        "DTK_ADMIN_USER": admin_user,
    })
    if admin_pass:
        existing["DTK_ADMIN_PASSWORD"] = admin_pass
    if ctx.creds.get("DTK_API_KEY"):
        existing["DTK_API_KEY"] = ctx.creds["DTK_API_KEY"]
    existing.setdefault("DTK_PG_HOST", "127.0.0.1")
    existing.setdefault("DTK_PG_PORT", str(PG_PORT))
    existing.setdefault("DTK_PG_USER", PG_USER)
    existing.setdefault("DTK_PG_PASSWORD", PG_PASSWORD)
    existing.setdefault("DTK_PG_DATABASE", PG_DATABASE)
    existing.setdefault("DTK_REDIS_URL", f"redis://127.0.0.1:{REDIS_PORT}/0")
    if ctx.repo:
        existing["DTK_REPO"] = str(ctx.repo)

    order = ["DTK_BASE_URL", "DTK_API_KEY", "DTK_ADMIN_USER", "DTK_ADMIN_PASSWORD",
             "DTK_REPO", "DTK_PG_HOST", "DTK_PG_PORT", "DTK_PG_USER",
             "DTK_PG_PASSWORD", "DTK_PG_DATABASE", "DTK_REDIS_URL",
             "FEISHU_FOLDER_TOKEN"]
    lines = ["# dtk 本地实例凭据（自建抖音采集服务）", "#",
             "# 本文件由 douyin-data skill 使用，请勿提交到 git，建议保持 chmod 600。",
             "", "# --- HTTP API ---"]
    for k in order:
        if k in existing:
            lines.append(f"{k}={existing[k]}")
    for k, v in existing.items():
        if k not in order:
            lines.append(f"{k}={v}")
    CRED_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    CRED_FILE.chmod(0o600)
    print(f"    ✓ 凭据已写入 {CRED_FILE}")


def step_identity(ctx: Ctx) -> bool:
    """确认身份池里有可用身份；没有就尝试从 Chrome 自动刷新。"""
    base = ctx.creds.get("DTK_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    key = ctx.creds.get("DTK_API_KEY")
    active = 0
    if key:
        st, payload = http("GET", f"{base}/api/v1/system/status",
                           headers={"X-API-Key": key}, timeout=10)
        if st == 200 and isinstance(payload, dict):
            pool = (((payload.get("data") or {}).get("pool") or {})
                    .get("douyin") or {})
            active = int(pool.get("active") or 0)
    if active > 0:
        print(f"    ✓ 身份池已有 {active} 个可用身份，跳过")
        return True

    cookie_py = SCRIPTS / "cookie.py"
    if not cookie_py.exists():
        print("    · 没有 cookie.py，跳过（手动导入见 setup-notes §5）")
        return True
    print("    从 Chrome 抓取 cookie 并导入身份池（会弹一次钥匙串授权）…")
    rc, out = sh([sys.executable, cookie_py, "refresh", "--test"], timeout=300)
    if rc != 0:
        print("    ✗ 自动刷新失败。手动方式：")
        print(f"      1) {cookie_py} status 查看现状")
        print(f"      2) {cookie_py} refresh --test 重试（确认 Chrome 已登录抖音）")
        print("      3) 或按 setup-notes §5 手动导出 cookie 后 identity-import")
        return True  # 不算致命：可以稍后补
    print("    ✓ 身份池已就绪")
    return True


def step_verify(ctx: Ctx) -> bool:
    """真的取一次数据 —— 「服务能跑」的唯一硬证据。"""
    dtk_py = SCRIPTS / "dtk.py"
    print("    读一个公开账号的主页验证链路 …")
    rc, out = sh([sys.executable, dtk_py, "--json", "user", VERIFY_SEC_UID], timeout=180)
    if rc != 0:
        print(f"    ✗ 取数失败：{out.strip().splitlines()[-1] if out.strip() else rc}")
        print(f"      排查：{STACK_SH} logs；或 python3 {dtk_py} health")
        return False
    payload: dict = {}
    try:
        start = out.index("{")
        payload = json.loads(out[start:out.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        pass
    nickname = payload.get("nickname") if isinstance(payload, dict) else None
    if not nickname:
        err = ((payload.get("error") or {}).get("message")
               if isinstance(payload, dict) else None)
        print(f"    ✗ 拿到了响应但没有账号数据：{err or out.strip()[:120]}")
        print("      若是风控（RISK/RATE），等几分钟再跑一次 install 即可。")
        return False
    count = (payload.get("stats") or {}).get("content_count")
    print(f"    ✓ 链路正常：读到账号「{nickname}」（{count} 个作品）")
    return True


INSTALL_STEPS = (
    ("准备镜像", "Postgres+TimescaleDB 与 Redis 镜像", "自动", step_images),
    ("启动容器", "Postgres 与 Redis（数据存命名卷）", "自动", step_containers),
    ("获取源码", f"克隆 dtk 并切到 {PINNED_COMMIT[:12]}", "自动", step_repo),
    ("应用补丁", "关键词搜索能力（15 个文件的补丁）", "自动", step_patch),
    ("Python 环境", "uv sync --all-extras", "自动", step_venv),
    ("生成配置", ".env（含随机 SECRET_KEY）", "自动", step_env),
    ("数据库迁移", "建表 + 时序扩展", "自动", step_migrate),
    ("启动服务", "API + worker（stack.sh，幂等）", "自动", step_serve),
    ("初始化账号", "管理员 + API Key + 凭据文件", "自动", step_init),
    ("导入登录态", "从 Chrome 抓 cookie 进身份池", "半自动", step_identity),
    ("端到端验证", "读一个公开账号的主页", "自动", step_verify),
)


def run_install(ctx: Ctx) -> int:
    print("抖音链路引导安装 · content-collect/douyin")
    print("=" * 58)
    ok, detail = _docker_ok()
    if not ok:
        print(f"前置检查未通过：{detail}")
        print("请先安装容器运行时（推荐 OrbStack，轻量；或 Docker Desktop），")
        print("启动它，然后重新运行本命令。其余环节本命令都会自动处理。")
        return 2

    ctx.creds = load_creds()
    ctx.repo = find_repo()
    total = len(INSTALL_STEPS)
    for index, (name, what, kind, fn) in enumerate(INSTALL_STEPS, 1):
        print(f"\n[{index}/{total}] {name}　（{what} · {kind}）")
        if not confirm(ctx, "继续这一步？"):
            print("    已跳过（后续步骤可能因此失败）")
            continue
        try:
            done = fn(ctx)
        except Exception as exc:  # noqa: BLE001 - 引导脚本，报错要可见而非堆栈
            print(f"    ✗ 这一步出错了：{type(exc).__name__}: {exc}")
            done = False
        if not done:
            print("    这一步没有完成 —— 修好问题后重跑本命令，已完成的步骤会自动跳过。")
            return 1
    print("\n" + "=" * 58)
    print("全部就绪。建议现在跑一次体检确认：")
    print(f"  python3 {ME} check")
    print(f"日常采集命令见 SKILL.md；服务启停：{STACK_SH} up|down|status")
    return 0


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="抖音链路环境体检与引导安装（换机器部署的第一入口）")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="只读体检：逐项检查前置条件")
    c.add_argument("--json", action="store_true", help="输出 JSON")
    i = sub.add_parser("install", help="引导安装：逐步把环境搭起来")
    i.add_argument("--yes", action="store_true", help="不问确认，直接执行")
    return p


def main() -> int:
    args = build_parser().parse_args()
    cmd = args.cmd or "check"
    if cmd == "check":
        return run_check(getattr(args, "json", False))
    return run_install(Ctx(yes=args.yes))


if __name__ == "__main__":
    raise SystemExit(main())
