#!/usr/bin/env python3
"""从小红书网页版的登录态里取 cookie —— 和抖音那一套同一个原理，换了个域名。

小红书和抖音的差别只有一个：**它不是「cookie 一给就能调接口」的平台**。
抖音的接口收 cookie + 一个算得出来的签名就行；小红书的接口签名（``x-s`` /
``x-t``）是页面里的 JS 现算的，而且请求必须由页面本身发出才不会被打回。
所以这里抓出来的 cookie 不是拿去 ``requests.get()``，而是**注入到一个受控
Chrome 里**，让页面自己去发明细请求（见 ``xhs.mjs``）。

用法：
    python3 xhs_cookie.py status           # 看 Chrome 里有没有登录态
    python3 xhs_cookie.py export --out /tmp/xhs_cookies.json
    python3 xhs_cookie.py export --json    # 打到 stdout

原理（macOS）：Chrome 的 cookie 库是 SQLite，值用钥匙串里 "Chrome Safe
Storage" 的密钥做 AES-128-CBC 加密。读库 → 取钥匙 → 解密。首次读钥匙串会弹
一次系统授权（点「始终允许」），之后静默。

只支持 macOS + Chrome。其他浏览器/系统请手工导出后用 ``--file`` 导入。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path

DOMAIN = "xiaohongshu.com"
CHROME_ROOT = Path("~/Library/Application Support/Google/Chrome").expanduser()
CHROME_APP = Path("/Applications/Google Chrome.app")
KEYCHAIN_SERVICE = "Chrome Safe Storage"
KEYCHAIN_TIMEOUT = 60

UA_TEMPLATE = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{version} Safari/537.36"
)

#: 有这些才说明「登录着」。``web_session`` 是小红书登录态的正主；
#: ``a1`` / ``webId`` 是设备指纹，缺了接口会把请求当成新设备。
LOGIN_MARKERS = ("web_session",)
DEVICE_MARKERS = ("a1", "webId")


class CookieError(RuntimeError):
    """原因已经说人话了，不需要堆栈。"""


# --------------------------------------------------------------------------- #
# Chrome 的 cookie 库
# --------------------------------------------------------------------------- #
def count_domain(profile: Path) -> int:
    try:
        con = sqlite3.connect(f"file:{profile / 'Cookies'}?mode=ro", uri=True, timeout=5)
    except sqlite3.Error:
        return -1
    try:
        row = con.execute(
            "select count(*) from cookies where host_key like ?", (f"%{DOMAIN}",)
        ).fetchone()
        return int(row[0]) if row else -1
    except sqlite3.Error:
        return -1
    finally:
        con.close()


def find_profile(explicit: str | None) -> tuple[Path, int]:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_absolute() and not path.exists():
            path = CHROME_ROOT / explicit
        if not (path / "Cookies").exists():
            raise CookieError(f"{path} 里没有 Cookies 文件，不是一个 Chrome profile")
        return path, count_domain(path)

    candidates = [CHROME_ROOT / "Default", *sorted(CHROME_ROOT.glob("Profile *"))]
    existed = False
    for cand in candidates:
        if not (cand / "Cookies").exists():
            continue
        existed = True
        count = count_domain(cand)
        if count > 0:
            return cand, count
    if existed:
        raise CookieError(f"Chrome 的各个 profile 里都没有 {DOMAIN} 的 cookie —— 浏览器里没登录过小红书？")
    raise CookieError(f"{CHROME_ROOT} 下没有 Chrome 的 cookie 库；没装 Chrome？用 --profile 指定")


def read_rows(profile: Path) -> list[tuple[str, str, str, bytes]]:
    """(host_key, name, path, encrypted_value)，用 backup API 读活的库。"""
    scratch = Path(tempfile.mkdtemp(prefix="xhs-chrome-", dir="/tmp"))
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
                "select host_key, name, path, encrypted_value from cookies "
                "where host_key like ? and length(encrypted_value) > 3 "
                "order by host_key, name",
                (f"%{DOMAIN}",),
            ).fetchall()
        finally:
            con.close()
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 钥匙串与解密
# --------------------------------------------------------------------------- #
def keychain_password(timeout: int = KEYCHAIN_TIMEOUT) -> bytes:
    try:
        proc = subprocess.run(
            ["security", "find-generic-password", "-w", "-s", KEYCHAIN_SERVICE],
            capture_output=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CookieError(
            "读取钥匙串超时。多半是授权弹窗被沙箱挡住了 —— 在普通终端里跑，"
            "或让助手以非沙箱方式执行；弹窗出现时点「始终允许」。"
        ) from exc
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CookieError(
            f"读不到钥匙串里的“{KEYCHAIN_SERVICE}”（退出码 {proc.returncode}）。"
            "确认这台机器上 Chrome 正常用过；之前拒绝过授权的话，"
            "去「钥匙串访问」里把该项的访问控制改回允许。"
        )
    return proc.stdout.strip()


def derive_key(password: bytes) -> bytes:
    return hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)


def _aes_cbc_decrypt(blob: bytes, key: bytes) -> bytes:
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
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
    # 较新的 Chrome 把 SHA256(域名) 也加密进去了，解出来要先剥掉。
    if plain[:32] == hashlib.sha256(host.encode()).digest():
        plain = plain[32:]
    return plain.decode("utf-8", errors="replace")


def chrome_version() -> str | None:
    plist = CHROME_APP / "Contents" / "Info.plist"
    if not plist.exists():
        return None
    proc = subprocess.run(
        ["defaults", "read", str(plist), "CFBundleShortVersionString"],
        capture_output=True, text=True,
    )
    value = proc.stdout.strip()
    return value if value and value[0].isdigit() else None


def chrome_ua() -> str:
    version = chrome_version()
    if not version:
        raise CookieError("读不出 Chrome 版本号；用 --ua 显式给一个 User-Agent")
    return UA_TEMPLATE.format(version=version)


# --------------------------------------------------------------------------- #
# 导出
# --------------------------------------------------------------------------- #
def export(profile_arg: str | None, ua: str | None) -> dict:
    profile, count = find_profile(profile_arg)
    key = derive_key(keychain_password())
    rows = read_rows(profile)
    if not rows:
        raise CookieError(f"{profile} 里没有 {DOMAIN} 的 cookie —— 浏览器里没登过小红书？")

    jar: dict[str, str] = {}
    for host, name, path, enc in rows:
        value = decrypt_value(enc, key, host)
        if not value:
            continue
        # 同名 cookie 在不同 host 上值可能不同（acw_tc 就有三个）；
        # 以最宽的 .domain 版本为准，因为它对所有子域都生效。
        if name in jar and not host.startswith("."):
            continue
        jar[name] = value

    missing = [m for m in LOGIN_MARKERS if m not in jar]
    if missing:
        raise CookieError(
            f"抓到的 cookie 里没有 {'、'.join(missing)} —— Chrome 里只是游客状态。"
            "先在浏览器里登录小红书，再跑一次。"
        )
    no_device = [m for m in DEVICE_MARKERS if m not in jar]
    return {
        "profile": str(profile),
        "user_agent": ua or chrome_ua(),
        "cookies": [{"name": k, "value": v, "domain": f".{DOMAIN}", "path": "/"}
                    for k, v in jar.items()],
        "count": len(jar),
        "warnings": [f"缺少设备指纹 {m}，接口可能被当成新设备" for m in no_device],
    }


def cmd_status(_args) -> int:
    try:
        profile, count = find_profile(None)
        print(f"Chrome：{profile}（{DOMAIN} cookie {count} 条）")
    except CookieError as exc:
        print(f"Chrome：{exc}")
        return 1
    try:
        data = export(None, None)
    except CookieError as exc:
        print(f"登录态：{exc}")
        return 1
    names = [c["name"] for c in data["cookies"]]
    print(f"登录态：有（{data['count']} 条，含 web_session）")
    print(f"  关键项：{'、'.join(n for n in ('a1','webId','web_session','gid','webId') if n in names)}")
    for warn in data["warnings"]:
        print(f"  ! {warn}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="xhs_cookie.py",
        description="从小红书网页版登录态取 cookie（macOS + Chrome），供 xhs.mjs 注入受控浏览器。",
    )
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("status", help="看 Chrome 里有没有小红书登录态")
    e = sub.add_parser("export", help="导出 cookie（JSON）")
    e.add_argument("--out", help="写到这个文件（默认打 stdout）")
    e.add_argument("--json", action="store_true", help="打 JSON（默认也是）")
    e.add_argument("--profile", help="Chrome profile 目录或名字（默认自动找）")
    e.add_argument("--ua", help="覆盖 User-Agent")

    args = p.parse_args(argv)
    if args.cmd == "status":
        return cmd_status(args)
    if args.cmd == "export":
        try:
            data = export(args.profile, args.ua)
        except CookieError as exc:
            print(f"失败：{exc}", file=sys.stderr)
            return 2
        payload = json.dumps(data, ensure_ascii=False, indent=1)
        if args.out:
            Path(args.out).write_text(payload + "\n", encoding="utf-8")
            print(f"已写入 {args.out}（{data['count']} 条 cookie）", file=sys.stderr)
        else:
            print(payload)
        return 0
    p.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
