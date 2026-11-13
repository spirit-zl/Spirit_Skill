#!/usr/bin/env python3
"""doctor.py —— 环境自检（换机器先跑这个）

用法：
    python3 doctor.py          # 只读体检，退出码 0=无缺失，1=有缺失
    python3 doctor.py --json   # 机器可读
    python3 doctor.py --deep   # 额外验证策牛登录态（会启一次无头浏览器，约 15 秒）

检查项：
    node / Chrome / ffmpeg / lark-cli 身份 / 浏览器登录态目录 / 目标文档配置
"""

import json
import os
import shutil
import subprocess
import sys

DATA_DIR = os.path.expanduser("~/.workbuddy/data/sztg-replay")
PROFILE = os.path.join(DATA_DIR, "profile")
CONFIG = os.path.join(DATA_DIR, "config.json")
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def check_node():
    node = shutil.which("node")
    if not node:
        return False, "找不到 node（需 18+，用到内置 WebSocket）"
    try:
        v = subprocess.run([node, "-e", "console.log(process.versions.node)"],
                           capture_output=True, text=True).stdout.strip()
        major = int(v.split(".")[0])
        return (major >= 18), f"node {v}" + ("" if major >= 18 else "（需 18+）")
    except Exception as e:
        return False, f"node 检测失败: {e}"


def check_chrome():
    cfg = load_config()
    cands = ([cfg["chrome"]] if cfg.get("chrome") else []) + CHROME_CANDIDATES
    for p in cands:
        if os.path.exists(p):
            return True, p
    return False, "找不到 Chrome/Edge/Chromium"


def check_ffmpeg():
    cfg = load_config()
    if cfg.get("ffmpeg") and os.path.exists(cfg["ffmpeg"]):
        return True, cfg["ffmpeg"]
    p = shutil.which("ffmpeg")
    if p:
        return True, p
    try:
        r = subprocess.run(["python3", "-c", "import imageio_ffmpeg;print(imageio_ffmpeg.get_ffmpeg_exe())"],
                           capture_output=True, text=True)
        if r.returncode == 0 and os.path.exists(r.stdout.strip()):
            return True, r.stdout.strip()
    except Exception:
        pass
    return False, "缺 ffmpeg（python3 -m pip install imageio-ffmpeg）"


def check_lark():
    p = shutil.which("lark-cli")
    if not p:
        return False, "找不到 lark-cli"
    try:
        r = subprocess.run([p, "auth", "status"], capture_output=True, text=True)
        st = json.loads(r.stdout)
        user = st.get("identities", {}).get("user", {})
        if user.get("status") == "ready":
            return True, f"user {user.get('userName')} ready"
        return False, f"lark-cli 用户身份未就绪：{user.get('message')}"
    except Exception as e:
        return False, f"lark-cli 状态读取失败: {e}"


def check_profile():
    if os.path.isdir(PROFILE) and os.listdir(PROFILE):
        size = sum(
            os.path.getsize(os.path.join(dp, f))
            for dp, _, fs in os.walk(PROFILE) for f in fs
            if os.path.exists(os.path.join(dp, f))
        )
        return True, f"{PROFILE}（{size // 1024 // 1024} MB）"
    return False, "没有保存的登录态，首次需扫码：capture.cjs qr"


def check_config():
    cfg = load_config()
    if cfg.get("doc"):
        return True, f"目标文档 {cfg['doc']}"
    return False, f'未配置目标文档，请在 {CONFIG} 写入 {{"doc": "..."}}'


def check_deep():
    script = os.path.join(SKILL_DIR, "scripts", "capture.cjs")
    r = subprocess.run(["node", script, "status"], capture_output=True, text=True)
    if r.returncode == 0:
        try:
            d = json.loads(r.stdout)
            return True, f"已登录 {d.get('account')}"
        except Exception:
            return True, "已登录"
    if r.returncode == 4:
        return False, "登录态失效，需要重新扫码"
    return False, f"状态检查异常：{(r.stderr or r.stdout)[-200:]}"


def main():
    deep = "--deep" in sys.argv
    as_json = "--json" in sys.argv

    checks = [
        ("node", check_node),
        ("chrome", check_chrome),
        ("ffmpeg", check_ffmpeg),
        ("lark-cli", check_lark),
        ("profile", check_profile),
        ("config", check_config),
    ]
    if deep:
        checks.append(("login", check_deep))

    results = []
    for name, fn in checks:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"异常: {e}"
        results.append({"item": name, "ok": ok, "detail": detail})

    missing = [r for r in results if not r["ok"]]
    if as_json:
        print(json.dumps({"ok": not missing, "results": results}, ensure_ascii=False, indent=2))
    else:
        for r in results:
            print(f"{'[OK]  ' if r['ok'] else '[MISS]'} {r['item']}: {r['detail']}")
        print()
        if missing:
            print(f"有 {len(missing)} 项需要处理。")
        else:
            print("环境完整，可以使用。")
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main())
