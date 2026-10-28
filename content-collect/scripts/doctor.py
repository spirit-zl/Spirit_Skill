#!/usr/bin/env python3
"""content-collect 的环境体检与安装引导 —— 换机器第一件事就跑它。

    python3 doctor.py check                 # 只读体检（退出码 0=全绿，1=有缺项）
    python3 doctor.py check --json
    python3 doctor.py plan                  # 只列「哪些工具没装/要升级 + 装它的命令」
    python3 doctor.py plan --json           # 给模型读的结构化清单
    python3 doctor.py install --dry-run     # 打印将要执行的命令，不动手
    python3 doctor.py install --yes         # 真的装（用户同意后再用这条）
    python3 doctor.py install --skip-douyin # 只用小红书，不装数据库那一套

`check` 只看；`plan` / `install` 管装。装完会自动再跑一次 `check`，
把「还差什么（多半是要人工的登录态）」列出来。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CDP_PORT = int(os.environ.get("XHS_CDP_PORT", "9333"))
DATA_HOME = Path(os.environ.get("XHS_HOME", "~/.workbuddy/data/xiaohongshu-cdp")).expanduser()

OK, BAD, TIP = "✓", "✗", "·"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []  # (组, 标记, 名称, 说明)

    def add(self, group: str, mark: str, name: str, detail: str = "") -> None:
        self.rows.append((group, mark, name, detail))

    def render(self, as_json: bool) -> int:
        bad = sum(1 for _, m, _, _ in self.rows if m == BAD)
        tip = sum(1 for _, m, _, _ in self.rows if m == TIP)
        if as_json:
            print(json.dumps({
                "ok": bad == 0,
                "failed": bad, "tips": tip,
                "items": [{"group": g, "mark": m, "name": n, "detail": d}
                          for g, m, n, d in self.rows],
            }, ensure_ascii=False, indent=2))
            return 0 if bad == 0 else 1

        print(f"环境体检 · content-collect　（数据目录 {DATA_HOME}）")
        group = None
        for g, m, n, d in self.rows:
            if g != group:
                print(f"\n{g}")
                group = g
            print(f"  {m} {n:<12}{('  ' + d) if d else ''}")
        print(f"\n汇总：{len(self.rows) - bad - tip} 项通过，{tip} 项提示，{bad} 项缺失")
        return 0 if bad == 0 else 1


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)


def check_toolchain(r: Report) -> None:
    node = shutil.which("node")
    if not node:
        r.add("工具链", BAD, "Node", "没装 Node 22+；brew install node")
        return
    _, out = run([node, "-v"])
    version = out.strip()
    major = 0
    try:
        major = int(version.lstrip("v").split(".")[0])
    except ValueError:
        pass
    if major >= 22:
        r.add("工具链", OK, "Node", f"{version}（{node}）")
    else:
        r.add("工具链", BAD, "Node", f"{version} 太旧，采集脚本用了全局 fetch/WebSocket，要 22+")

    if CHROME.exists():
        r.add("工具链", OK, "Chrome", str(CHROME))
    else:
        r.add("工具链", BAD, "Chrome", f"找不到 {CHROME}；这条路必须用本机 Chrome")


def check_login(r: Report) -> None:
    code, out = run([sys.executable, str(HERE / "xhs_cookie.py"), "status"], timeout=90)
    if code == 0:
        lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
        detail = lines[-1] if lines else ""
        r.add("登录态", OK, "小红书 cookie", detail)
    else:
        detail = [ln.strip() for ln in out.splitlines() if ln.strip()]
        r.add("登录态", BAD, "小红书 cookie",
              (detail[-1] if detail else "读不到") + "　→ Chrome 里登录小红书后重跑")


def cdp_alive() -> dict | None:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            f"http://127.0.0.1:{CDP_PORT}/json/version", timeout=2) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


def check_browser(r: Report) -> None:
    info = cdp_alive()
    if info:
        r.add("受控浏览器", OK, f"端口 {CDP_PORT}", f"{info.get('Browser', '?')}（已在跑，直接复用）")
    else:
        r.add("受控浏览器", TIP, f"端口 {CDP_PORT}", "现在没起，首次采集会自动拉起")


def check_feishu(r: Report) -> None:
    lark = shutil.which("lark-cli") or os.environ.get("LARK_CLI")
    if not lark:
        r.add("飞书（可选）", TIP, "lark-cli", "没装；只采集不落表就不需要")
        return
    code, out = run([lark, "auth", "status"], timeout=30)
    ready = '"status": "ready"' in out or '"status":"ready"' in out
    if code == 0 and ready:
        r.add("飞书（可选）", OK, "lark-cli", "已授权")
    else:
        r.add("飞书（可选）", TIP, "lark-cli", "未授权；要落表先跑 lark-cli auth login")

    envs = [DATA_HOME / "credentials.env",
            Path("~/.workbuddy/data/douyin-data/credentials.env").expanduser()]
    hit = next((p for p in envs if p.exists() and "FEISHU_FOLDER_TOKEN=" in p.read_text(encoding="utf-8")), None)
    if hit:
        token = ""
        for line in hit.read_text(encoding="utf-8").splitlines():
            if line.startswith("FEISHU_FOLDER_TOKEN="):
                token = line.split("=", 1)[1].strip()
        r.add("飞书（可选）", OK, "默认文件夹", f"https://my.feishu.cn/drive/folder/{token}（来自 {hit.parent.name}）")
    else:
        r.add("飞书（可选）", TIP, "默认文件夹", "还没记过；落表时用 --base 指定，或 --set-folder 记一个")


def probe_http(url: str, timeout: float = 3.0) -> tuple[bool, str]:
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=timeout) as resp:
            return True, resp.read(200).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as exc:
        return False, str(getattr(exc, "reason", exc))


# --------------------------------------------------------------------------- #
# 安装引导：列出缺什么、装它要跑什么命令
# --------------------------------------------------------------------------- #
@dataclass
class Tool:
    key: str          # 机器名
    name: str         # 给人看的名字
    need: str         # 「需要什么」一句话
    platforms: str    # all / douyin
    check: object     # () -> (bool, 说明)
    install: list     # 安装命令（list[list[str]]）；空 = 本机没有可用通道
    manual: str = ""  # 没有命令行通道时给的人工入口


def _ver(cmd: list[str]) -> str:
    code, out = run(cmd, timeout=15)
    return out.strip().splitlines()[0] if code == 0 and out.strip() else ""


def _check_python() -> tuple[bool, str]:
    v = sys.version_info
    return (v >= (3, 9), f"{v.major}.{v.minor}.{v.micro}")


def _check_node() -> tuple[bool, str]:
    path = shutil.which("node")
    if not path:
        return False, "未安装"
    raw = _ver([path, "-v"]).lstrip("v")
    try:
        major = int(raw.split(".")[0])
    except (ValueError, IndexError):
        return False, f"版本读不出来（{raw}）"
    managed = "workbuddy/binaries" in path
    return (major >= 22, f"{raw}（{'WorkBuddy 受管运行时' if managed else '系统'}；需要 ≥22）")


def _check_chrome() -> tuple[bool, str]:
    return (CHROME.exists(), str(CHROME) if CHROME.exists() else "未安装")


def _check_lark() -> tuple[bool, str]:
    path = shutil.which("lark-cli") or os.environ.get("LARK_CLI", "")
    if not path:
        return False, "未安装（要用飞书才需要）"
    code, out = run([path, "auth", "status"], timeout=30)
    ready = '"status": "ready"' in out or '"status":"ready"' in out
    return (True, ("已授权" if ready else "已安装，但还没 auth login"))


def _check_uv() -> tuple[bool, str]:
    path = shutil.which("uv")
    return (bool(path), _ver([path, "--version"]) if path else "未安装（抖音链路要它）")


def _env_urls() -> dict[str, str]:
    """从 douyin_api/.env 里取连接串（找不到就空）。"""
    repo = NEXT_REPO[0] if NEXT_REPO else None
    if not repo:
        return {}
    env = repo / ".env"
    if not env.exists():
        return {}
    out = {}
    for line in env.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _tcp(host: str, port: int) -> bool:
    import socket
    try:
        with socket.create_connection((host, port), timeout=3):
            return True
    except OSError:
        return False


def _svc_check(key: str, default_port: int):
    """数据库/缓存：直连能通就算过；通不了但有 Docker 也算「有来源」。"""
    def check() -> tuple[bool, str]:
        url = _env_urls().get(key, "")
        m = re.search(r"@([^/:]+):(\d+)", url) or re.search(r"//([^/:]+):(\d+)", url)
        if m and _tcp(m.group(1), int(m.group(2))):
            return True, f"{m.group(1)}:{m.group(2)} 可达"
        if shutil.which("docker"):
            return True, "本机有 Docker —— 用 stack.sh up 起容器即可"
        return False, (f"{key} 未配置或连不上" if m else f".env 里没有 {key}")
    return check


NEXT_REPO: list[Path] = []   # main() 里填；_env_urls 用它


def _find_repo() -> Path | None:
    repo = os.environ.get("DTK_REPO")
    cands = [Path(repo).expanduser()] if repo else []
    cands += [Path("~/.workbuddy/data/douyin-data/repo").expanduser(),
              Path("~/douyin_api").expanduser(),
              Path("~/src/douyin_api").expanduser(),
              Path("~/WorkBuddy/spirit_WorkBuddy/douyin_api").expanduser()]
    return next((p for p in cands if (p / "src" / "dtk").is_dir()), None)


def tools() -> list[Tool]:
    """每台机器的可用通道不一样：有 brew 用 brew，有 npm 用 npm，都没有就给人官网。"""
    has_brew = bool(shutil.which("brew"))
    has_npm = bool(shutil.which("npm"))
    return [
        Tool("python", "Python 3", "≥3.9，纯标准库，不用 pip 装东西", "all", _check_python,
             [["brew", "install", "python@3.12"]] if has_brew else [],
             "WorkBuddy 自带受管 Python；独立安装见 https://www.python.org/downloads/"),
        Tool("node", "Node.js", "≥22（脚本用了全局 fetch/WebSocket）", "all", _check_node,
             [["brew", "install", "node"]] if has_brew else [],
             "WorkBuddy 自带受管 Node；独立安装见 https://nodejs.org/"),
        Tool("chrome", "Google Chrome", "取 cookie + 当受控浏览器，必须", "all", _check_chrome,
             [["brew", "install", "--cask", "google-chrome"]] if has_brew else [],
             "https://www.google.com/chrome/"),
        Tool("lark", "lark-cli", "写飞书表格用（可选）", "all", _check_lark,
             [["npm", "install", "-g", "@larksuite/cli"]] if has_npm else [],
             "https://www.npmjs.com/package/@larksuite/cli"),
        Tool("uv", "uv", "跑 dtk 用", "douyin", _check_uv,
             [["brew", "install", "uv"]] if has_brew else [],
             "https://docs.astral.sh/uv/getting-started/installation/"),
        Tool("postgres", "Postgres", "dtk 的库（原生或 Docker 都行）", "douyin",
             _svc_check("DTK_DATABASE_URL", 5432),
             [["brew", "install", "postgresql@17"], ["brew", "services", "start", "postgresql@17"]]
             if has_brew else [],
             "或用 OrbStack/Docker Desktop 跑容器（stack.sh up）"),
        Tool("redis", "Redis", "dtk 的队列", "douyin",
             _svc_check("DTK_REDIS_URL", 6379),
             [["brew", "install", "redis"], ["brew", "services", "start", "redis"]]
             if has_brew else [],
             "或用 OrbStack/Docker Desktop 跑容器（stack.sh up）"),
    ]


def cmd_plan(as_json: bool, skip_douyin: bool) -> int:
    NEXT_REPO.append(_find_repo()) if _find_repo() else None
    rows = []
    for t in tools():
        if skip_douyin and t.platforms == "douyin":
            continue
        ok, detail = t.check()
        rows.append({"key": t.key, "name": t.name, "need": t.need, "ok": ok,
                     "detail": detail, "platforms": t.platforms,
                     "install": [" ".join(c) for c in t.install],
                     "manual": t.manual})
    missing = [r for r in rows if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not missing, "missing": [r["key"] for r in missing],
                          "tools": rows}, ensure_ascii=False, indent=2))
        return 0 if not missing else 1
    print("换机器要看的东西（✓ 已就绪　✗ 需要处理）\n")
    for r in rows:
        mark = OK if r["ok"] else BAD
        tag = "两平台" if r["platforms"] == "all" else "仅抖音"
        print(f"  {mark} [{tag}] {r['name']}")
        print(f"        {r['detail']}")
        if not r["ok"] and r["install"]:
            print(f"        → {' && '.join(r['install'])}")
        elif not r["ok"]:
            print(f"        → 本机没有可自动安装的通道，人工装：{r['manual']}")
    print()
    if missing:
        print(f"共 {len(missing)} 项待处理。确认安装请执行：")
        print(f"  python3 {Path(__file__).name} install --yes"
              + (" --skip-douyin" if skip_douyin else ""))
    else:
        print("全部就绪，可以直接用。")
    return 0 if not missing else 1


def cmd_install(as_json: bool, yes: bool, dry_run: bool, skip_douyin: bool) -> int:
    repo = _find_repo()
    if repo:
        NEXT_REPO.append(repo)
    todo: list[tuple[Tool, list[list[str]]]] = []
    for t in tools():
        if skip_douyin and t.platforms == "douyin":
            continue
        ok, detail = t.check()
        if ok or not t.install:
            continue
        todo.append((t, t.install))

    if not todo:
        print("没有需要安装的东西（或缺少 brew/npm 无法自动装）。")
        return cmd_plan(as_json, skip_douyin)

    if dry_run or not yes:
        print("将要执行：")
        for t, cmds in todo:
            print(f"  · {t.name}")
            for c in cmds:
                print(f"      {' '.join(c)}")
        if dry_run:
            print("\n（--dry-run：没有执行任何命令）")
        else:
            print("\n确认后加 --yes 执行。")
        return 0

    failed: list[str] = []
    for t, cmds in todo:
        print(f"\n== 安装 {t.name} ==")
        for c in cmds:
            print(f"  $ {' '.join(c)}")
            proc = subprocess.run(c, text=True)
            if proc.returncode != 0:
                print(f"  ✗ 失败（退出码 {proc.returncode}）")
                failed.append(t.key)
                break
        else:
            print(f"  ✓ {t.name} 完成")

    print("\n== 装完复检 ==")
    rc = cmd_plan(False, skip_douyin)
    print("\n== 完整体检 ==")
    r = Report()
    check_toolchain(r)
    check_login(r)
    check_browser(r)
    check_douyin(r)
    check_feishu(r)
    r.render(False)
    if failed:
        print(f"\n有 {len(failed)} 项装失败：{'、'.join(failed)}（看上面的报错）")
    return 0 if not failed and rc == 0 else 1


def check_douyin(r: Report) -> None:
    """抖音链路：仓库 + 服务。**不看 Docker** —— 只要 .env 指得到库，原生服务也算过。"""
    group = "抖音链路（需要自建 dtk）"
    engine = HERE / "douyin" / "collect.py"
    if engine.exists():
        r.add(group, OK, "引擎", str(engine.relative_to(SKILL)))
    else:
        r.add(group, BAD, "引擎", f"缺 {engine}")

    # 仓库位置：环境变量 > 凭据文件 > 常见路径
    repo = os.environ.get("DTK_REPO")
    if not repo:
        cred = Path("~/.workbuddy/data/douyin-data/credentials.env").expanduser()
        if cred.exists():
            for line in cred.read_text(encoding="utf-8").splitlines():
                if line.startswith("DTK_REPO="):
                    repo = line.split("=", 1)[1].strip()
    candidates = [Path(repo).expanduser()] if repo else []
    candidates += [Path("~/.workbuddy/data/douyin-data/repo").expanduser(),
                   Path("~/douyin_api").expanduser(),
                   Path("~/src/douyin_api").expanduser(),
                   Path("~/WorkBuddy/spirit_WorkBuddy/douyin_api").expanduser()]
    found = next((p for p in candidates if (p / "src" / "dtk").is_dir()), None)
    if found:
        has_env = (found / ".env").exists()
        r.add(group, OK if has_env else BAD, "源码仓库", f"{found}" + ("" if has_env else "（缺 .env）"))
    else:
        r.add(group, BAD, "源码仓库", "找不到 dtk 源码；换机器见 references/setup-notes.md")
        return

    ok, body = probe_http("http://127.0.0.1:8000/healthz")
    if ok:
        r.add(group, OK, "API", "http://127.0.0.1:8000 可达")
    else:
        r.add(group, BAD, "API", "没起；stack.sh up（有 Docker）或 stack.sh up --no-docker（用本机库）")

    ok_api, body2 = probe_http("http://127.0.0.1:8000/readyz")
    if ok_api:
        try:
            ready = json.loads(body2 + ("}" if not body2.rstrip().endswith("}") else ""))
            comps = ready.get("components", {})
            deps = "、".join(f"{k}={'ok' if v.get('ok') else 'x'}" for k, v in comps.items())
            r.add(group, OK if all(v.get("ok") for v in comps.values()) else BAD, "PG / Redis", deps or "无组件信息")
        except (json.JSONDecodeError, AttributeError):
            r.add(group, TIP, "PG / Redis", "readyz 返回看不懂，跳过")
    else:
        r.add(group, TIP, "PG / Redis", "API 没起，查不了")

    code, out = run(["pgrep", "-f", "dtk worker"], timeout=5)
    r.add(group, OK if code == 0 else TIP, "worker", "在跑" if code == 0 else "没跑（采集会一直卡在 running）")


def main() -> int:
    p = argparse.ArgumentParser(
        prog="doctor.py",
        description="content-collect 环境体检 / 安装引导（换机器第一件事）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "典型流程（换机器）：\n"
            "  1) doctor.py plan            # 列出缺什么\n"
            "  2) 征得用户同意后：doctor.py install --yes\n"
            "  3) doctor.py check           # 复检，确认可以用了\n"
        ),
    )
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("check", help="完整体检（只读）")
    c.add_argument("--json", action="store_true")

    pl = sub.add_parser("plan", help="列出各工具的安装情况与安装命令（只读）")
    pl.add_argument("--json", action="store_true")
    pl.add_argument("--skip-douyin", action="store_true", help="只用小红书，忽略抖音那套依赖")

    ins = sub.add_parser("install", help="安装缺失的工具（用户同意后用）")
    ins.add_argument("--yes", action="store_true", help="真的执行（不加只打印计划）")
    ins.add_argument("--dry-run", action="store_true", help="只打印将执行的命令")
    ins.add_argument("--skip-douyin", action="store_true", help="不装数据库那一套")
    ins.add_argument("--json", action="store_true")
    args = p.parse_args()

    if args.cmd == "plan":
        return cmd_plan(args.json, args.skip_douyin)
    if args.cmd == "install":
        return cmd_install(args.json, args.yes, args.dry_run, args.skip_douyin)
    if args.cmd != "check":
        p.print_help()
        return 2

    r = Report()
    check_toolchain(r)
    check_login(r)
    check_browser(r)
    check_douyin(r)
    check_feishu(r)
    return r.render(args.json)


if __name__ == "__main__":
    raise SystemExit(main())
