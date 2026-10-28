#!/usr/bin/env python3
"""从本机 Chrome 自动抓抖音 cookie，导入身份池 —— 「换 cookie」不用再手工找。

cookie 是采集服务唯一的真实凭据，``sid_guard`` 里有明确的有效期（当前这份
到 2026-12-14）。到期要换一份新的，手工换的老路是「打开浏览器 → 开发者工具
→ 抄 Cookie 头 → 存文件 → 导身份池」—— 四个环节每个都可能抄错，而且每次
都要重新找一遍。本脚本把它变成一条命令：

    python3 cookie.py status             # 存档 cookie 还剩几天 + 身份池现状
    python3 cookie.py ensure             # 能用就不动，不能用/临期才自动抓一份
    python3 cookie.py refresh --dry-run  # 抓取 + 服务端体检，不落盘、不导入
    python3 cookie.py refresh            # 抓取 → 备份旧档 → 导入身份池

``collect.py`` 在采集前会自动调 ``ensure``（``--no-auto-cookie`` 可关），
所以日常采集不必单独跑这条命令：浏览器里登录着抖音，cookie 就自己续上。

原理（macOS）：Chrome 的 cookie 库是 SQLite（``Chrome/Default/Cookies``），
cookie 值用钥匙串里 "Chrome Safe Storage" 的密钥做 AES-128-CBC 加密。脚本
读库、取钥匙、解密、拼回 Cookie 头；User-Agent 按本机 Chrome 的真实版本拼出
（身份必须带配套 UA，见 references/setup-notes.md）。

前提与坑：

- Chrome 里必须**处于登录状态**。没登录会抓到一个访客 cookie，脚本会拒绝。
- 首次读钥匙串会弹一次系统授权，点「始终允许」，之后就是静默的。
- **运行环境的沙箱会挡住钥匙串授权框**（表现为卡住不返回）。在 WorkBuddy
  会话里执行这条命令时需要放到沙箱外跑；普通终端不受影响。
- 只支持 macOS + Chrome。其他浏览器/系统请手工导出 cookie 后用
  ``dtk.py identity-import`` 导入。
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.parse
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import dtk  # noqa: E402  （同目录的 API 客户端，纯标准库）

DTK_HOME = Path(os.environ.get("DTK_HOME", "~/.workbuddy/data/douyin-data")).expanduser()
COOKIE_FILE = DTK_HOME / "douyin_cookie.txt"
COOKIE_BACKUP = DTK_HOME / "douyin_cookie.bak"

CHROME_ROOT = Path("~/Library/Application Support/Google/Chrome").expanduser()
CHROME_APP = Path("/Applications/Google Chrome.app")
KEYCHAIN_SERVICE = "Chrome Safe Storage"
#: 等用户点钥匙串授权框的耐心（秒）。
KEYCHAIN_TIMEOUT = 60

#: 服务端硬性要求的 cookie（dtk 的 identity/importing.py 里 REQUIRED）。
REQUIRED = ("ttwid",)
#: 有这些说明 cookie 里带着登录态 —— 刷新要的是登录态，不是游客的。
SESSION_MARKERS = ("sessionid", "sessionid_ss", "sid_tt", "sid_guard", "uid_tt")

UA_TEMPLATE = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
)


class CookieError(RuntimeError):
    """原因已经说人话了，不需要堆栈。"""


# --------------------------------------------------------------------------- #
# Chrome 的 cookie 库
# --------------------------------------------------------------------------- #
def count_douyin(profile: Path) -> int:
    """数一下这个 profile 里有多少条 douyin cookie；读不动就 -1。"""
    try:
        con = sqlite3.connect(f"file:{profile / 'Cookies'}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return -1
    try:
        row = con.execute(
            "select count(*) from cookies where host_key like '%.douyin.com'"
        ).fetchone()
        return int(row[0]) if row else -1
    except sqlite3.Error:
        return -1
    finally:
        con.close()


def find_profile(explicit: str | None) -> tuple[Path, int]:
    """定位 Chrome profile：显式指定的优先，否则找第一个有 douyin cookie 的。"""
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() and not path.exists():
            path = CHROME_ROOT / explicit  # 支持 --profile "Profile 1" 这种写法
        if not (path / "Cookies").exists():
            raise CookieError(f"{path} 里没有 Cookies 文件，不是一个 Chrome profile")
        count = count_douyin(path)
        if count <= 0:
            raise CookieError(f"{path} 里没有 douyin 的 cookie —— 这个 profile 没登录过抖音？")
        return path, count

    candidates = [CHROME_ROOT / "Default", *sorted(CHROME_ROOT.glob("Profile *"))]
    existed = False
    for cand in candidates:
        if not (cand / "Cookies").exists():
            continue
        existed = True
        count = count_douyin(cand)
        if count > 0:
            return cand, count
    if existed:
        raise CookieError("Chrome 的各个 profile 里都没有 douyin 的 cookie —— 浏览器里没登录过抖音？")
    raise CookieError(f"{CHROME_ROOT} 下没有 Chrome 的 cookie 库；没装 Chrome？用 --profile 指定别的位置")


def read_douyin_rows(profile: Path) -> list[tuple[str, str, bytes]]:
    """把 douyin 的 (host_key, name, encrypted_value) 读出来。

    用 SQLite 的 backup API 读**活的**数据库（Chrome 正在运行时也能读，
    而且不丢 WAL 里刚写的数据）；直接拷贝文件可能在写入中途拿到半截。
    """
    scratch = Path(tempfile.mkdtemp(prefix="dtk-chrome-", dir="/tmp"))
    try:
        copy = scratch / "Cookies"
        src = sqlite3.connect(f"file:{profile / 'Cookies'}?mode=ro", uri=True, timeout=10)
        dst = sqlite3.connect(str(copy))
        src.backup(dst)
        dst.close()
        src.close()
        con = sqlite3.connect(str(copy))
        try:
            return con.execute(
                "select host_key, name, encrypted_value from cookies "
                "where host_key like '%.douyin.com' and length(encrypted_value) > 3 "
                "order by host_key, name"
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 钥匙串与解密
# --------------------------------------------------------------------------- #
def keychain_password(timeout: int = KEYCHAIN_TIMEOUT) -> bytes:
    """Chrome 的加密主密钥，存在登录钥匙串里。

    ``timeout`` 可调：交互式 refresh 愿意等用户点 60 秒授权框；采集前的自动
    检查不愿意 —— 谁也说不准那 60 秒里用户在不在，卡住比直接说「读不到」更糟。
    """
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CookieError(
            "读取钥匙串超时。多半是授权弹窗被沙箱挡住了 —— 在普通终端里跑，"
            "或让助手以非沙箱方式执行；弹窗出现时点「始终允许」。"
        )
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CookieError(
            f"读不到钥匙串里的“{KEYCHAIN_SERVICE}”（退出码 {proc.returncode}）。"
            "确认这台机器上 Chrome 正常用过；之前拒绝过授权的话，"
            "去「钥匙串访问」里把该项的访问控制改回允许。"
        )
    return proc.stdout.strip()


def derive_key(password: bytes) -> bytes:
    """Chrome 的密钥派生：PBKDF2-HMAC-SHA1，盐 saltysalt，1003 轮，16 字节。"""
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def _aes_cbc_decrypt(blob: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        # 没装 cryptography 就走 openssl（macOS 自带）。--nopad 让填充在
        # Python 里自己剥，免得坏数据时 openssl 直接报错、看不到原因。
        proc = subprocess.run(
            ["openssl", "enc", "-aes-128-cbc", "-d", "-K", key.hex(),
             "-iv", (b" " * 16).hex(), "-nopad"],
            input=blob, capture_output=True,
        )
        if proc.returncode != 0:
            raise CookieError(f"openssl 解密失败（退出码 {proc.returncode}）")
        return proc.stdout
    decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
    return decryptor.update(blob) + decryptor.finalize()


def decrypt_value(enc: bytes, key: bytes, host: str) -> str:
    prefix = enc[:3]
    if prefix != b"v10":
        raise CookieError(f"cookie 用了未知的加密格式 {prefix!r}（Chrome 更新换代了？）")
    plain = _aes_cbc_decrypt(enc[3:], key)
    pad = plain[-1] if plain else 0
    if 1 <= pad <= 16 and plain.endswith(bytes([pad]) * pad):
        plain = plain[:-pad]
    # 较新的 Chrome 会把 SHA256(域名) 也加密进去，解出来要先剥掉。
    if plain[:32] == hashlib.sha256(host.encode()).digest():
        plain = plain[32:]
    return plain.decode("utf-8", errors="replace")


def build_jar(rows: list[tuple[str, str, bytes]], key: bytes) -> dict[str, str]:
    """解密全部 cookie 并去重（同名时以先出现的为准，排序后即 .douyin.com）。"""
    jar: dict[str, str] = {}
    for host, name, enc in rows:
        value = decrypt_value(enc, key, host)
        if value and name not in jar:
            jar[name] = value
    return jar


# --------------------------------------------------------------------------- #
# 周边信息
# --------------------------------------------------------------------------- #
def chrome_version() -> str | None:
    plist = CHROME_APP / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    proc = subprocess.run(
        ["defaults", "read", str(plist), "CFBundleShortVersionString"],
        capture_output=True, text=True,
    )
    value = proc.stdout.strip()
    return value if re.fullmatch(r"\d+(\.\d+)+", value) else None


def chrome_ua() -> str:
    version = chrome_version()
    if not version:
        raise CookieError("读不出 Chrome 版本号；用 --ua 显式给一个 User-Agent")
    return UA_TEMPLATE.format(version=version)


def sid_guard_expiry(jar: dict[str, str]) -> dt.datetime | None:
    """从 sid_guard 里算登录态的真实到期时间（服务端也是这么算的）。"""
    raw = jar.get("sid_guard")
    if not raw:
        return None
    parts = urllib.parse.unquote(raw).split("|")
    if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
        return dt.datetime.fromtimestamp(int(parts[1]) + int(parts[2]))
    return None


def expiry_note(expiry: dt.datetime | None) -> str:
    if expiry is None:
        return "（没带 sid_guard，算不出到期时间）"
    days = (expiry - dt.datetime.now()).days
    tail = f"（剩 {days} 天）" if days >= 0 else "（已过期）"
    return f"到 {expiry:%Y-%m-%d %H:%M}{tail}"


# --------------------------------------------------------------------------- #
# 存档与导入
# --------------------------------------------------------------------------- #
def save_jar(text: str) -> bool:
    """写入存档；内容有变化时先把旧份备份到 ``douyin_cookie.bak``。返回是否备份。"""
    COOKIE_FILE.parent.mkdir(parents=True, exist_ok=True)
    backed_up = False
    if COOKIE_FILE.exists() and COOKIE_FILE.read_text(encoding="utf-8").strip() != text.strip():
        shutil.copyfile(COOKIE_FILE, COOKIE_BACKUP)
        backed_up = True
    COOKIE_FILE.write_text(text + "\n", encoding="utf-8")
    os.chmod(COOKIE_FILE, 0o600)
    return backed_up


def import_identity(jar_text: str, ua: str, dry_run: bool) -> object:
    body = {
        "platform": "douyin",
        "cookies": jar_text,
        "user_agent": ua,
        "language": "zh-CN",
        "timezone": "Asia/Shanghai",
        "dry_run": dry_run,
    }
    creds = dtk.load_creds()
    return dtk.request("POST", "/api/v1/admin/identities/import", body=body, creds=creds)


def summarize_import(result: object) -> str:
    """把导入接口的返回压成一行人话；结构没见过时退回截断 JSON。"""
    if not isinstance(result, dict):
        return str(result)[:300]
    report = result.get("report") if isinstance(result.get("report"), dict) else result
    bits: list[str] = []
    identity_id = result.get("id") or result.get("identity_id")
    if not identity_id and isinstance(result.get("identity"), dict):
        identity_id = result["identity"].get("id")
    if identity_id:
        bits.append(f"身份 {str(identity_id)[:8]}…")
    if report.get("usable") is not None:
        bits.append(f"usable={report.get('usable')}")
    if report.get("authenticated") is not None:
        bits.append(f"authenticated={report.get('authenticated')}")
    if report.get("missing_required"):
        bits.append(f"缺必需项：{'、'.join(map(str, report['missing_required']))}")
    if report.get("expires_at"):
        bits.append(f"到期 {report['expires_at']}")
    return "；".join(bits) or json.dumps(result, ensure_ascii=False)[:300]


def identity_rows(data: object) -> list[dict]:
    if isinstance(data, dict):
        for key in ("items", "identities", "data"):
            if isinstance(data.get(key), list):
                return [row for row in data[key] if isinstance(row, dict)]
    if isinstance(data, list):
        return [row for row in data if isinstance(row, dict)]
    return []


# --------------------------------------------------------------------------- #
# 子命令
# --------------------------------------------------------------------------- #
def cmd_status(_args: argparse.Namespace) -> int:
    # 1) 存档 cookie（不需要钥匙串）
    if COOKIE_FILE.exists():
        text = COOKIE_FILE.read_text(encoding="utf-8").strip()
        jar = dict(kv.split("=", 1) for kv in text.split("; ") if "=" in kv)
        expiry = sid_guard_expiry(jar)
        print(f"存档 cookie：{COOKIE_FILE.name}（{len(jar)} 条）")
        print(f"  登录态{expiry_note(expiry)}")
        if COOKIE_BACKUP.exists():
            print(f"  旧份备份：{COOKIE_BACKUP.name}")
    else:
        print(f"还没有存档 cookie（{COOKIE_FILE}）——跑一次 refresh 生成")

    # 2) Chrome 侧（不需要钥匙串）
    try:
        profile, count = find_profile(None)
        version = chrome_version() or "未知版本"
        print(f"Chrome：{profile}（douyin cookie {count} 条，Chrome {version}）")
    except CookieError as exc:
        print(f"Chrome：{exc}")

    # 3) 身份池
    try:
        creds = dtk.load_creds()
        data = dtk.request("GET", "/api/v1/admin/identities", creds=creds)
    except Exception as exc:  # noqa: BLE001  （服务没起也要能看存档信息）
        print(f"身份池：读不到（{exc}）")
        return 0
    rows = identity_rows(data)
    print(f"身份池：{len(rows)} 个身份")
    for row in rows:
        identity_id = str(row.get("id", "?"))
        bits = [
            f"  {identity_id[:8]}…",
            f"state={row.get('state', '?')}",
        ]
        if row.get("cooldown_until"):
            bits.append(f"冷却到 {row['cooldown_until']}")
        if row.get("last_used_at"):
            bits.append(f"用至 {row['last_used_at']}")
        fp = row.get("fingerprint")
        if isinstance(fp, dict) and fp.get("browser_family"):
            bits.append(f"{fp['browser_family']} {fp.get('browser_major', '?')}")
        print("　".join(bits))
    return 0


def refresh(*, profile: str | None = None, ua: str | None = None, dry_run: bool = False,
            no_import: bool = False, test: bool = False,
            keychain_timeout: int = KEYCHAIN_TIMEOUT, log=print) -> dict:
    """抓取 → 存档 → 导入，返回一份摘要。

    这是「换 cookie」的**唯一实现**：``refresh`` 与 ``ensure`` 两个子命令、
    以及采集前的自动检查，走的都是它。以前是「命令行入口里写一遍流程」，
    结果自动检查只能反过来开子进程、靠解析 stdout 判断成败 —— 一旦失败，
    调用方拿到的只有一行中文，没有原因也没有身份 id。
    """
    if sys.platform != "darwin":
        raise CookieError("自动抓取目前只支持 macOS；其他系统请手工导出 cookie 后走 identity-import")

    found, count = find_profile(profile)
    log(f"Chrome profile：{found}（douyin cookie {count} 条）")

    log("读取钥匙串（第一次会弹一次授权框，点「始终允许」）…")
    key = derive_key(keychain_password(keychain_timeout))

    jar = build_jar(read_douyin_rows(found), key)
    missing = [name for name in REQUIRED if name not in jar]
    if missing:
        raise CookieError(f"抓到的 cookie 里缺 {'、'.join(missing)} —— 浏览器里登录抖音了吗？")
    if not any(name in jar for name in SESSION_MARKERS):
        raise CookieError(
            "抓到的 cookie 里没有登录态（sessionid 系都没有）—— Chrome 里只是游客状态。"
            "先在浏览器里登录抖音，再跑一次。"
        )

    expiry = sid_guard_expiry(jar)
    log(f"解密完成：{len(jar)} 条 cookie，登录态{expiry_note(expiry)}")

    jar_text = "; ".join(f"{k}={v}" for k, v in jar.items())
    agent = ua or chrome_ua()
    log(f"User-Agent：{agent}")

    if dry_run:
        probe = import_identity(jar_text, agent, dry_run=True)
        log(f"[dry-run] 服务端体检：{summarize_import(probe)}")
        return {"dry_run": True, "cookies": len(jar), "expires_at": expiry,
                "profile": str(found), "probe": probe}

    backed_up = save_jar(jar_text)
    log(f"已存档：{COOKIE_FILE}" + (f"（旧份在 {COOKIE_BACKUP.name}）" if backed_up else ""))
    if no_import:
        log("按要求跳过导入（--no-import）。")
        return {"saved": True, "imported": False, "cookies": len(jar),
                "expires_at": expiry, "profile": str(found), "backed_up": backed_up}

    result = import_identity(jar_text, agent, dry_run=False)
    log(f"已导入身份池：{summarize_import(result)}")

    identity_id = None
    if isinstance(result, dict):
        identity_id = result.get("id") or result.get("identity_id")
        if not identity_id and isinstance(result.get("identity"), dict):
            identity_id = result["identity"].get("id")

    if test:
        if not identity_id:
            log("（没从返回里解析出身份 id，跳过 --test；用 identity-list 找）")
        else:
            probe = dtk.request(
                "POST", f"/api/v1/admin/identities/{identity_id}/test",
                body={}, creds=dtk.load_creds(), poll=120,
            )
            log(f"真实探测：{json.dumps(probe, ensure_ascii=False)[:300]}")

    return {"saved": True, "imported": True, "cookies": len(jar), "expires_at": expiry,
            "profile": str(found), "identity_id": identity_id,
            "backed_up": backed_up, "import_report": result}


def cmd_refresh(args: argparse.Namespace) -> int:
    refresh(profile=args.profile, ua=args.ua, dry_run=args.dry_run,
            no_import=args.no_import, test=args.test)
    if not args.dry_run and not args.no_import:
        print("旧身份不用管：cookie 失效后会自动冷却，不会影响调度。")
    return 0


# --------------------------------------------------------------------------- #
# 自动获取：采集前先确认身份池能用，不能用就去 Chrome 抓一份
# --------------------------------------------------------------------------- #
#: 存档 cookie 距到期不足这么多天，就顺手换一份新的。
#:
#: 换迟了的代价比换早了高得多：cookie 一失效，整批采集会在第一页就
#: ``IDENTITY_POOL_EXHAUSTED``；而提前换只是多一次导入，旧身份自然冷却。
EXPIRY_MARGIN_DAYS = 3

#: 自动检查愿意等钥匙串多久。交互式 refresh 是 60 秒（在等人点授权框），
#: 自动检查是在采集前顺手做的 —— 卡住比读不到更糟，读不到还能给出原因。
AUTO_KEYCHAIN_TIMEOUT = 8


def pool_identities(creds: dict[str, str] | None = None) -> list[dict]:
    """身份池现状。服务没起时抛 dtk.ApiError，由调用方决定怎么办。"""
    data = dtk.request("GET", "/api/v1/admin/identities",
                       creds=creds or dtk.load_creds())
    return identity_rows(data)


def ready_identity(rows: list[dict]) -> dict | None:
    """挑一个**现在就能用**的身份；没有则 None。

    三个条件缺一不可：还在 active（没被退休）、有登录态（authenticated）、
    不在风控冷却里。只看 state 会把「冷却到 6 小时后」误判成可用 —— 那正是
    ``IDENTITY_POOL_EXHAUSTED`` 的来源。
    """
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        if row.get("state") != "active" or row.get("retired_at"):
            continue
        if not row.get("authenticated"):
            continue
        if (row.get("health") or 0) <= 0:
            continue
        until = row.get("cooldown_until")
        if isinstance(until, str) and until:
            try:
                if dt.datetime.fromisoformat(until.replace("Z", "+00:00")) > now:
                    continue
            except ValueError:
                pass
        return row
    return None


def archived_expiry() -> dt.datetime | None:
    """存档 cookie 的登录态到期时间（读 ``sid_guard``，不碰钥匙串）。"""
    if not COOKIE_FILE.exists():
        return None
    text = COOKIE_FILE.read_text(encoding="utf-8").strip()
    jar = dict(kv.split("=", 1) for kv in text.split("; ") if "=" in kv)
    return sid_guard_expiry(jar)


def ensure(*, auto: bool = True, margin_days: int = EXPIRY_MARGIN_DAYS,
           profile: str | None = None, ua: str | None = None,
           quiet: bool = False) -> dict:
    """确认「现在能采」；不能且 ``auto`` 为真时，就地刷一份 cookie。

    返回 ``state``：

    * ``ok``        —— 身份池里有可用身份，且存档 cookie 还没临期；
    * ``refreshed`` —— 原本不能用（或已临期），刷新成功，现在能采；
    * ``failed``    —— 刷新失败，但池里还有别的可用身份，可以冒险继续；
    * ``blocked``   —— 池里没有任何可用身份，刷新也失败 —— 采不了。

    这个函数**不抛异常**（服务连不上除外）：采集前的检查不该因为它自己出问题
    就把整件事拦下来，它只负责报告状态，由调用方决定硬停还是带风险继续。
    """
    def note(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    facts: dict = {"auto": auto, "margin_days": margin_days}
    try:
        rows = pool_identities()
    except dtk.ApiError as exc:
        facts.update(state="unknown", reason=f"读不到身份池（{exc.code or exc.status}）")
        note(f"  · {facts['reason']}，跳过登录态检查")
        return facts

    facts["identities"] = len(rows)
    expiry = archived_expiry()
    facts["expires_at"] = expiry.isoformat() if expiry else None
    soon = expiry is not None and (
        expiry - dt.datetime.now() <= dt.timedelta(days=margin_days))
    if soon:
        facts["reason"] = f"存档 cookie {expiry_note(expiry)}，已进入 {margin_days} 天预警"

    good = ready_identity(rows)
    if good and not soon:
        facts.update(state="ok", identity_id=good.get("id"))
        return facts
    if good and soon and not auto:
        facts.update(state="ok", identity_id=good.get("id"), warn=facts["reason"])
        return facts

    if not auto:
        facts.update(state="blocked" if not good else "ok",
                     reason=facts.get("reason") or "身份池里没有可用身份（全部冷却或退休）")
        return facts

    note(f"  · {facts.get('reason') or '身份池里没有可用身份'}，从本机 Chrome 自动抓一份…")
    try:
        result = refresh(profile=profile, ua=ua, keychain_timeout=AUTO_KEYCHAIN_TIMEOUT,
                         log=lambda m: note(f"    {m}"))
    except CookieError as exc:
        facts.update(state="failed" if good else "blocked", reason=str(exc))
        note(f"  ! 自动获取 cookie 失败：{exc}")
        if not good:
            note("  ! 身份池里也没有可用身份 —— 这会采不到东西。")
        return facts
    except dtk.ApiError as exc:
        facts.update(state="failed" if good else "blocked",
                     reason=f"服务端拒绝导入（{exc.code or exc.status}）：{exc.message}")
        note(f"  ! {facts['reason']}")
        return facts

    after = archived_expiry() or result.get("expires_at")
    facts.update(state="refreshed", identity_id=result.get("identity_id"),
                 expires_at=after.isoformat() if after else None)
    note(f"  · 已刷新登录态{expiry_note(after if isinstance(after, dt.datetime) else None)}")
    return facts


def cmd_ensure(args: argparse.Namespace) -> int:
    facts = ensure(auto=not args.no_auto, margin_days=args.margin_days,
                   profile=args.profile, quiet=args.json)
    if args.json:
        print(json.dumps(facts, ensure_ascii=False, indent=2, default=str))
        return 0 if facts["state"] in ("ok", "refreshed", "unknown") else 2
    print({"ok": "登录态可用，无需刷新",
           "refreshed": "登录态已自动刷新",
           "failed": "自动刷新失败，但池里还有别的可用身份",
           "blocked": "没有可用身份，采集会被拦",
           "unknown": "读不到身份池"}.get(facts["state"], facts["state"]))
    if facts.get("reason"):
        print(f"原因：{facts['reason']}")
    return 0 if facts["state"] in ("ok", "refreshed", "unknown") else 2


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cookie.py",
        description="从本机 Chrome 抓抖音 cookie 并导入身份池（macOS）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            "  python3 cookie.py status\n"
            "  python3 cookie.py refresh --dry-run\n"
            "  python3 cookie.py refresh --test\n"
        ),
    )
    sub = p.add_subparsers(dest="command")
    sub.add_parser("status", help="看存档 cookie 的到期时间与身份池现状")

    r = sub.add_parser("refresh", help="抓取 Chrome 里的 cookie → 存档 → 导入身份池")
    r.add_argument("--dry-run", action="store_true",
                   help="只抓取并在服务端做体检，不落盘、不导入")
    r.add_argument("--no-import", action="store_true", help="抓取并存档，但不导入")
    r.add_argument("--profile", help="Chrome profile 目录或名字（默认自动找）")
    r.add_argument("--ua", help="覆盖 User-Agent（默认按本机 Chrome 版本生成）")
    r.add_argument("--test", action="store_true", help="导入后对新身份发一次真实探测")

    # 采集前的自动检查：能用就不动，不能用/临期才去 Chrome 抓。
    # 采集脚本默认走这条（collect.py 的 --no-auto-cookie 可关掉）。
    e = sub.add_parser("ensure", help="确认登录态可用；不可用就自动从 Chrome 抓一份")
    e.add_argument("--no-auto", action="store_true", help="只检查、不自动抓取")
    e.add_argument("--margin-days", type=int, default=EXPIRY_MARGIN_DAYS,
                   help=f"距到期不足这么多天就提前换（默认 {EXPIRY_MARGIN_DAYS}）")
    e.add_argument("--profile", help="Chrome profile 目录或名字（默认自动找）")
    e.add_argument("--json", action="store_true", help="输出 JSON 结果")
    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "refresh":
        try:
            return cmd_refresh(args)
        except CookieError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 2
        except dtk.ApiError as exc:
            print(f"服务端拒绝：{exc.code or exc.status} —— {exc.message}", file=sys.stderr)
            return 2
    if args.command == "ensure":
        return cmd_ensure(args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
