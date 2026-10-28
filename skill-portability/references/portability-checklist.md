# 跨平台化实现参考

配合 `SKILL.md` 使用，给出可直接套用的实现片段。

---

## 1. 路径配置层模板

```python
"""路径与平台配置。所有可配置项都支持环境变量覆盖。"""
import os
import sys

APP_NAME = "<技能名>"
IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = (not IS_WINDOWS) and (not IS_MACOS)


def default_data_base():
    """各操作系统标准的「用户数据」根目录。"""
    if IS_WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    elif IS_MACOS:
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.abspath(os.path.expanduser(base))


def _resolve(specific_env, shared_env=None, app_name=APP_NAME):
    """优先级：专属变量 > 共享变量 > 系统标准目录。"""
    v = os.environ.get(specific_env)
    if v:
        return os.path.abspath(os.path.expanduser(v))
    if shared_env:
        shared = os.environ.get(shared_env)
        if shared:
            return os.path.join(os.path.abspath(os.path.expanduser(shared)), app_name)
    return os.path.join(default_data_base(), app_name)


STAGING_ROOT = _resolve("MY_SKILL_HOME", "MY_PIPELINE_DATA")
CACHE = os.path.join(STAGING_ROOT, "_cache")


def destructive_target(cli_value=None):
    """写入类路径（目标仓库、目标知识库）：**不给默认值**。

    猜错会造成真实破坏，所以缺配置时必须报错。
    """
    if cli_value:
        return os.path.abspath(os.path.expanduser(cli_value))
    env = os.environ.get("MY_SKILL_TARGET")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    raise SystemExit(
        "未指定目标路径。请用 --target <路径>，或设置环境变量 MY_SKILL_TARGET。\n"
        "（这里刻意不给默认值：猜错会写到别的地方。）")
```

**注意**：模块级常量在 `import` 时求值。测试里必须先设环境变量再导入，
并清 `sys.modules` 缓存（同名 `config.py` 会在多个技能间互相覆盖）。

---

## 2. 跨平台文件锁

```python
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None


class ExclusiveLock:
    def __init__(self, path, timeout=0.0, stale_after=3600, force_backend=None):
        self.path = os.path.abspath(path)
        self.timeout = max(0.0, float(timeout))
        self.stale_after = stale_after
        self.force_backend = force_backend      # 便于测试各后端
        self.fh = None
        self.backend = None
        self._lockfile_owned = False

    def pick_backend(self):
        if self.force_backend:
            return self.force_backend
        if fcntl is not None:
            return "fcntl"
        if msvcrt is not None:
            return "msvcrt"
        return "lockfile"

    def acquire(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.backend = self.pick_backend()
        deadline = time.time() + self.timeout
        while True:
            if self._attempt():
                return self
            if time.time() >= deadline:
                raise LockError("另一个进程正在运行（锁：%s）" % self.path)
            time.sleep(0.2)

    def _attempt(self):
        return {"fcntl": self._attempt_fcntl,
                "msvcrt": self._attempt_msvcrt,
                "lockfile": self._attempt_lockfile}[self.backend]()

    def _attempt_lockfile(self):
        """原子创建；已存在则判陈旧。

        **关键**：清理掉陈旧锁后必须在同一轮里立刻重试。
        调用方默认 timeout=0，不重试的话陈旧锁永远抢不回来，锁会永久卡死。
        """
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._is_stale():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                    continue          # 清完马上重抢
                return False
            with os.fdopen(fd, "w") as f:
                f.write("%d\n" % os.getpid())
            self._lockfile_owned = True
            return True
        return False
```

### Windows `msvcrt` 的两个坑

1. 文件必须有内容：先写 1 字节再 `seek(0)`
2. 锁以「字节范围」为单位：`msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)` 锁 1 字节即可

```python
def _attempt_msvcrt(self):
    fh = open(self.path, "a+")
    try:
        if os.path.getsize(self.path) == 0:
            fh.write("0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        fh.close()
        return False
    self.fh = fh
    return True
```

### 陈旧判定

```python
def _is_stale(self):
    try:
        age = time.time() - os.path.getmtime(self.path)
        pid = int((open(self.path).read().strip() or "0").splitlines()[0])
    except Exception:
        return True          # 读不出来就当垃圾锁
    if age > self.stale_after:
        return True
    return not _pid_alive(pid)
```

判定存活的 PID 时，**判不准就保守认为还活着**（宁可失败也不并发写）。
Windows 没有可靠的 `os.kill(pid, 0)`，用 `OpenProcess` 探测。

---

## 3. 平台专有能力的后端抽象

```python
BACKEND_REASON = {}
try:
    if IS_MACOS:
        import Vision  # noqa
        VISION_OK = True
    else:
        BACKEND_REASON["vision"] = "非 macOS 平台"
except Exception as exc:
    BACKEND_REASON["vision"] = "缺少依赖：%s" % exc

try:
    import pytesseract  # noqa
    TESSERACT_OK = True
except Exception as exc:
    BACKEND_REASON["tesseract"] = "缺少依赖：%s" % exc


def detect_backend():
    """返回 (后端名, 说明)；都不可用时后端名为 None。"""
    if VISION_OK:
        return "macos-vision", "macOS Vision"
    if TESSERACT_OK:
        return "tesseract", "Tesseract"
    return None, "无可用后端（%s / %s）" % (
        BACKEND_REASON.get("vision", "-"), BACKEND_REASON.get("tesseract", "-"))
```

调用方：

```python
backend, reason = detect_backend()
if not backend:
    # 显式失败，让记录带上 parse_failed 与原因
    raise RuntimeError("需要 OCR，但当前环境没有可用后端。%s" % reason)
```

**不要写成 `if not backend: return ""`** —— 静默的空结果会一路传播，
最后变成「这篇文档是空的」而没人发现。

---

## 4. 宿主专有字段的注入

源仓库保持中立，只在装到特定宿主时补字段：

```python
def inject_host_field(installed_skill_md, field="agent_created"):
    text = installed_skill_md.read_text(encoding="utf-8")
    if field in text or not text.startswith("---\n"):
        return False
    end = text.find("\n---", 3)
    if end < 0:
        return False
    installed_skill_md.write_text(
        text[:end] + "\n%s: true" % field + text[end:], encoding="utf-8")
    return True
```

安装脚本里只在目标宿主匹配时调用，例如 `if target == "workbuddy" and not link: ...`。

---

## 5. 常见宿主技能目录

| 宿主 | 项目级 | 个人级 |
| --- | --- | --- |
| VS Code / Copilot | `.github/skills/`、`.claude/skills/`、`.agents/skills/` | `~/.copilot/skills/`、`~/.claude/skills/`、`~/.agents/skills/` |
| Claude Code | `.claude/skills/` | `~/.claude/skills/` |
| 通用 Agent Skills | `.agents/skills/` | `~/.agents/skills/` |
| WorkBuddy | — | `~/.workbuddy/skills/` |

目录名即技能名，**必须与 frontmatter 的 `name` 一致**，否则不会被加载。
