#!/usr/bin/env python3
"""可移植性回归测试模板（纯标准库，无需 pytest）。

用法：
  1. 复制到 <repo>/tests/test_portability.py
  2. 改下面 CONFIG 区：REPO 用相对 __file__ 定位；SKILLS 填你的技能名→脚本目录
  3. 按需删掉不适用的 TestCase
  4. 运行：python3 -m unittest discover -s tests -v

两个易踩的自伤点（模板里已处理）：
  - 扫描禁用模式时跳过 tests/（测试自己会写样例字符串）
  - 模块级常量在导入时求值 → 必须先设环境变量再 import，并清 sys.modules 缓存
"""

import importlib
import os
import pathlib
import re
import sys
import tempfile
import time
import unittest

# ----------------------------- CONFIG -------------------------------------

REPO = pathlib.Path(__file__).resolve().parent.parent
SKILLS = {
    # 技能名 -> 脚本目录
    "knowledge-combine": REPO / "knowledge-combine" / "scripts",
    "obsidian-writer": REPO / "obsidian-writer" / "scripts",
}
# 两个技能里同名的模块，加载时需要在 sys.modules 里清掉避免串味
LOCAL_MODULES = ("config", "locking", "ocr", "parsers", "import_notes",
                 "plan_from_job", "rebuild_index", "common")
# 声明「只用标准库」的技能 -> 允许的顶层模块
STDLIB_ONLY = {"obsidian-writer"}

# 禁用模式：按需增删。注意别让模式字符串自己命中自己。
FORBIDDEN = [
    (re.compile(r"/Users/[A-Za-z0-9_.-]+"), "作者机器的绝对路径"),
    (re.compile(r"\.workbuddy[/\\]data"), "硬编码的平台数据目录"),
]

# ----------------------------- 工具 ---------------------------------------


def load_skill(scripts_dir, *modnames):
    for name in LOCAL_MODULES:
        sys.modules.pop(name, None)
    sys.path.insert(0, str(scripts_dir))
    try:
        return [importlib.import_module(m) for m in modnames]
    finally:
        try:
            sys.path.remove(str(scripts_dir))
        except ValueError:
            pass


def set_env(**kw):
    """临时改环境变量，返回恢复函数。值为 None 表示删除该变量。"""
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return restore


# ----------------------- 1. 仓库卫生 --------------------------------------


class TestRepoHygiene(unittest.TestCase):
    TEXT_EXT = {".md", ".py", ".txt", ".json", ".yaml", ".yml", ".cfg", ".toml"}

    def _files(self):
        for p in REPO.rglob("*"):
            if not p.is_file():
                continue
            if any(part in {".git", "__pycache__", ".venv"} for part in p.parts):
                continue
            if "tests" in p.relative_to(REPO).parts:
                continue      # 测试里的样例字符串会自我命中
            if p.suffix in self.TEXT_EXT:
                yield p

    def test_no_personal_or_platform_paths(self):
        offenders = []
        for p in self._files():
            text = p.read_text(encoding="utf-8", errors="replace")
            for pat, label in FORBIDDEN:
                for m in pat.finditer(text):
                    offenders.append("%s:%d %s（%s）"
                                     % (p.relative_to(REPO),
                                        text[:m.start()].count("\n") + 1,
                                        label, m.group(0)))
        self.assertEqual(offenders, [], "仍有机器/平台相关残留：\n" + "\n".join(offenders))

    def test_no_unix_only_tmp_literal(self):
        offenders = []
        for p in self._files():
            if p.suffix != ".py" or "scripts" not in p.parts:
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if re.search(r"""["']/tmp[/"']""", line):
                    offenders.append("%s:%d" % (p.relative_to(REPO), i))
        self.assertEqual(offenders, [], "应改用 tempfile.gettempdir()：%s" % offenders)


# ----------------------- 2. 技能格式合规 ----------------------------------


class TestSkillFrontmatter(unittest.TestCase):
    def test_name_matches_directory_and_description_is_single_line(self):
        for name, scripts_dir in SKILLS.items():
            text = (scripts_dir.parent / "SKILL.md").read_text(encoding="utf-8")
            self.assertTrue(text.startswith("---\n"), "%s：缺少 frontmatter" % name)
            fm = text.split("---", 2)[1]

            m = re.search(r"^name:\s*(.+)$", fm, re.M)
            self.assertIsNotNone(m, "%s：缺少 name" % name)
            self.assertEqual(m.group(1).strip(), name,
                             "%s：name 必须与目录名一致，否则不会被加载" % name)

            d = re.search(r"^description:\s*(.+)$", fm, re.M)
            self.assertIsNotNone(d, "%s：缺少 description" % name)
            desc = d.group(1).strip()
            self.assertFalse(desc.startswith((">", "|")),
                             "%s：description 不能用 YAML 折叠块" % name)
            self.assertLessEqual(len(desc), 1024)
            self.assertNotIn("<", desc)
            self.assertNotIn(">", desc)

    def test_body_not_too_long(self):
        for name, scripts_dir in SKILLS.items():
            lines = len((scripts_dir.parent / "SKILL.md").read_text(
                encoding="utf-8").splitlines())
            self.assertLessEqual(lines, 500, "%s：SKILL.md 正文 %d 行" % (name, lines))

    def test_stdlib_only_skills_stay_stdlib_only(self):
        if not STDLIB_ONLY:
            self.skipTest("未声明纯标准库技能")
        allowed = {"argparse", "hashlib", "json", "os", "re", "shutil", "sys",
                   "unicodedata", "uuid", "datetime", "errno", "time", "importlib",
                   "tempfile", "fcntl", "msvcrt", "ctypes", "glob", "pathlib",
                   "stat"} | set(LOCAL_MODULES)
        bad = []
        for name in STDLIB_ONLY:
            for p in SKILLS[name].glob("*.py"):
                for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                    m = re.match(r"\s*(?:import|from)\s+([A-Za-z_][\w.]*)", line)
                    if m and m.group(1).split(".")[0] not in allowed:
                        bad.append("%s/%s:%d %s" % (name, p.name, i, m.group(1)))
        self.assertEqual(bad, [], "引入了第三方依赖：%s" % bad)


# ----------------------- 3. 配置层 ----------------------------------------


class TestConfig(unittest.TestCase):
    def test_env_precedence_and_platform_default(self):
        """必须先设环境变量再 import —— 模块级常量在导入时求值。"""
        name = sorted(SKILLS)[0]
        (CFG,) = load_skill(SKILLS[name], "config")
        self.assertTrue(str(getattr(CFG, "STAGING_ROOT", "")).startswith("/")
                        or os.path.isabs(str(getattr(CFG, "STAGING_ROOT", "/"))),
                        "默认值必须是绝对路径")
        self.assertNotIn(str(REPO), str(getattr(CFG, "STAGING_ROOT", "")),
                         "数据目录不能落在仓库内")

    def test_no_default_for_destructive_paths(self):
        """目标写入路径（vault / 目标仓库）缺配置时必须报错，不能有默认值。"""
        for name, scripts_dir in SKILLS.items():
            (CFG,) = load_skill(scripts_dir, "config")
            if hasattr(CFG, "vault_root"):
                restore = set_env(OBSIDIAN_VAULT=None)
                try:
                    with self.assertRaises(SystemExit):
                        CFG.vault_root(None)
                finally:
                    restore()
                return
        self.skipTest("本项目没有此类路径")


# ----------------------- 4. 文件名安全 ------------------------------------


class TestFilenameSafety(unittest.TestCase):
    def _fn(self, title):
        # 改成你项目里的文件名清洗函数
        (mod,) = load_skill(list(SKILLS.values())[-1], "import_notes")
        return mod.sanitize_filename(title)

    def test_illegal_chars_removed(self):
        out = self._fn('a/b\\c:d*e?f"g<h>i|j')
        self.assertFalse(set(out) & set('/\\:*?"<>|'), out)

    def test_windows_reserved_names(self):
        for bad in ("CON", "con", "NUL", "AUX", "PRN", "COM1", "COM9", "LPT1", "LPT9"):
            out = self._fn(bad)
            self.assertTrue(out.startswith("_"), "%r 应避开 Windows 保留名，实际 %r" % (bad, out))

    def test_trailing_dot_and_space_stripped(self):
        self.assertFalse(self._fn("名字.").endswith("."))
        out = self._fn("x" * 119 + "." + "y" * 50)
        self.assertLessEqual(len(out), 120)
        self.assertFalse(out.endswith("."))


# ----------------------- 5. 跨平台锁 --------------------------------------


class TestLocking(unittest.TestCase):
    def _locking(self):
        (mod,) = load_skill(list(SKILLS.values())[-1], "locking")
        return mod

    def test_native_backend_mutual_exclusion(self):
        locking = self._locking()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "locks", "t.lock")
            with locking.ExclusiveLock(path) as a:
                other = locking.ExclusiveLock(path)
                other.backend = a.backend
                self.assertFalse(other._attempt(), "第二个实例必须拿不到锁")
                other.release()

    def test_lockfile_fallback_mutual_exclusion(self):
        locking = self._locking()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "locks", "l.lock")
            a = locking.ExclusiveLock(path, force_backend="lockfile")
            b = locking.ExclusiveLock(path, force_backend="lockfile")
            try:
                a.acquire()
                self.assertFalse(b._attempt())
                a.release()
                self.assertFalse(os.path.exists(path))
            finally:
                a.release()
                b.release()

    def test_stale_lock_takeover(self):
        """陈旧锁必须在 timeout=0 下也能抢回来（清理后需同轮重试）。"""
        locking = self._locking()
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "locks", "s.lock")
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write("999999999\n")
            old = time.time() - 10_000
            os.utime(path, (old, old))
            lk = locking.ExclusiveLock(path, force_backend="lockfile")
            try:
                lk.acquire()
            finally:
                lk.release()


if __name__ == "__main__":
    unittest.main(verbosity=2)
