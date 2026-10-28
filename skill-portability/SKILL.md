---
name: skill-portability
description: 把一个已经能跑但绑定了本机环境的技能（或脚本项目）改造成跨平台、可发布到 git 的仓库。当用户要求「做成跨平台的 / 跨系统的 / 换台机器也能用 / 发布到 git / 开源出去 / 给别人用」，或现有代码里出现绝对路径、平台专有 API、宿主专有工具名、写死时区与临时目录时使用。覆盖路径配置层、跨平台文件锁、OCR 等平台专有能力的后端抽象、文件名安全、编码、依赖声明、安装脚本、README，以及把可移植性约束写成可执行回归测试。
agent_created: true
---

# skill-portability — 技能/脚本项目的跨平台化改造

把一个绑定了本机环境的项目，改造成任意 Windows / macOS / Linux 上都能跑、
可发布到 git 的形态。

## 何时使用

- 用户要求「跨平台」「跨系统」「换机器也能用」「发布到 git」「开源」
- 代码里出现 `/Users/<名字>`、`~/.某平台目录`、平台专有的 import、写死的时区/临时目录
- 准备把内部工具交给别人用

## 核心原则

**默认值可以方便，但绝不能假设环境。** 所有路径、解释器、外部命令都必须可配置，
且默认值落在各操作系统自己的标准位置。

## 改造清单（按顺序做，每步都有验证方式）

### 1. 路径配置层（最重要）

新建 `config.py`，把散落在各处的路径收敛到一处，暴露为模块级常量。

优先级链：**技能专属环境变量 > 共享数据根环境变量 > 系统标准目录**。

```python
def default_data_base():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Local")
    elif sys.platform == "darwin":
        base = os.path.join(os.path.expanduser("~"), "Library", "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or os.path.join(
            os.path.expanduser("~"), ".local", "share")
    return os.path.abspath(os.path.expanduser(base))
```

**不能用默认值推断的路径，就别给默认值。** 例如目标 vault / 目标仓库这类路径，
猜错会造成真实破坏 —— 缺了就报错，让使用者显式指定。

数据目录必须落在仓库之外，否则会被 git 跟踪。

### 2. 平台专有能力的后端抽象

**不要静默降级成空结果**——这是最危险的模式。以 OCR 为例，正确做法是：

```python
def detect_backend():
    """返回 (后端名, 说明)。都不可用时后端名为 None。"""
    # macOS Vision → Tesseract → None
```

调用方在 `backend is None` 时**抛异常并说明原因**，让记录带上 `parse_failed` 与原因，
而不是返回空字符串。空文档比报错危险得多：前者没人发现，后者会被处理。

同一原则适用于：图像处理、通知、系统集成、剪贴板、开机自启等一切平台专有能力。

### 3. 跨平台文件锁

三级降级，覆盖所有主流系统：

| 层级 | 实现 | 说明 |
| --- | --- | --- |
| 1 | POSIX `fcntl.flock` | Linux / macOS，进程退出内核自动释放 |
| 2 | Windows `msvcrt.locking` | 需先 `seek(0)`，文件至少 1 字节 |
| 3 | 原子 `O_CREAT\|O_EXCL` lockfile | 兜底，靠 PID + mtime 判陈旧 |

**lockfile 兜底路径有个必踩的坑**：清理掉陈旧锁之后必须在同一轮里立刻重试，
否则调用方默认 `timeout=0` 会直接放弃，陈旧锁永远抢不回来（锁永久卡死）。
详见 `references/portability-checklist.md`。

### 4. 文件名安全（Windows）

除常见非法字符 `\ / : * ? " < > |` 与控制字符外，还要处理：

- **保留设备名**：`CON`、`PRN`、`AUX`、`NUL`、`COM1-9`、`LPT1-9`（主名命中时加前缀）
- **结尾的点与空格**：Windows 会静默丢弃，导致文件名与预期不符
- **先截断再清理**：截断本身可能切出一个结尾的点

### 5. 编码与临时目录

- 所有 `subprocess` 调用显式 `encoding="utf-8", errors="replace"`
  —— Windows 上 `text=True` 会用区域编码（cp936/cp1252）解码，把 JSON 打乱
- 所有文件读写显式 `encoding="utf-8"`
- 临时目录用 `tempfile.gettempdir()`，不要写 `/tmp`（Windows 没有）

### 6. 时间戳

用本地时区，**不要写死偏移**：

```python
datetime.now().astimezone().isoformat(timespec="seconds")   # 对
datetime.now(timezone(timedelta(hours=8)))                  # 错：换时区就错时间
```

### 7. 宿主专有工具名

调用外部能力（MCP server、平台 API）时，不要在正文里写死某宿主的工具名。
做法：正文用**逻辑操作名**，另附一张映射表（逻辑操作 → 各宿主实际工具名），
并加一句「先列出当前环境可用的工具再调用」。

### 8. 依赖声明与自检

- `requirements.txt`（核心）+ `requirements-ocr.txt` 之类的可选依赖分开
- 提供 `doctor.py`：检查解释器版本、路径可写、依赖齐否、可选能力可用性、
  外部命令是否存在。**换机器后第一件事就是跑它**
- 纯标准库的项目要明确声明「无需安装任何东西」，这是很大的优势

## 可发布性

| 项 | 要求 |
| --- | --- |
| README | 是什么、流水线图、环境要求、安装、配置表、快速开始、跨平台差异、已知限制 |
| `.gitignore` | `__pycache__`、虚拟环境、编辑器、系统文件、运行期数据目录兜底 |
| 安装脚本 | 支持多宿主的目标目录（如 `.github/skills`、`.claude/skills`、`~/.copilot/skills`），复制/软链两种模式，默认不覆盖 |
| 许可证 | **必须问用户**，不能替用户决定；未指定时要显式说明「默认保留所有权利」 |
| 敏感信息 | 仓库里不得有个人路径、用户名、内网地址、真实 vault 路径、API key |

**宿主专有字段只在安装时注入**：源仓库保持中立，`install.py` 装到某宿主时才补上该宿主
需要的字段（如 WorkBuddy 的 `agent_created`）。这样同一份源能装到所有宿主。

## 把约束写成测试（关键一步）

可移植性要求很容易在后续改动中被破坏。**把它们变成可执行的回归测试**，
模板见 `assets/portability_test_template.py`，用纯标准库 `unittest`，无需 pytest。

必测的六类：

1. 仓库里不得出现作者机器路径 / 平台数据目录硬编码
2. 不得使用 `/tmp` 字面量
3. `SKILL.md` 的 frontmatter 合规（`name` 与目录名一致、`description` 单行且 ≤1024 字符、
   正文 ≤500 行）
4. 配置层环境变量优先级正确；无默认值的路径缺失时报错
5. Windows 保留设备名、结尾点/空格、超长标题的截断
6. 每个锁后端的互斥性与陈旧锁回收；无后端时必须显式失败

写测试时注意两个常见自伤：

- 测试自己的样例字符串会命中自己的禁用模式 → 扫描时跳过 `tests/` 目录
- 模块级常量在导入时求值 → **必须先设环境变量，再 import 被测模块**，
  并清 `sys.modules` 缓存（两个技能都有同名 `config.py` 时尤其要注意）

## 验证步骤

```
1. 编译全部脚本                      → 无语法错误
2. 残余耦合扫描                      → 绝对路径/平台专有 API 清零
3. 两个技能各自跑完整链路            → 功能没被改坏
4. 跑可移植性测试                    → 全绿
5. install.py 装到临时目录           → 从新位置还能跑（可重定位性）
6. 用临时数据目录与临时输出目标验证  → 不碰用户的真实数据
```

**第 5 步容易被忽略但很重要**：验证脚本用相对自身位置定位模块，
装到别处后仍能正常工作。写文件时用 `os.path.dirname(os.path.abspath(__file__))`
插入 `sys.path`，不要依赖当前工作目录。

## 常见错误

| 错误 | 正确做法 |
| --- | --- |
| 只改目录名/文件名就算「跨平台」 | 路径、锁、编码、时区、文件名、工具名都要过一遍 |
| 平台能力不可用时静默返回空 | 显式抛错并说明原因 |
| 给目标路径编一个「合理」的默认值 | 缺了直接报错，避免写错位置 |
| 把运行期数据目录放进仓库 | 放系统标准数据目录，或确保 gitignore 兜底 |
| 用 `\|` 做 grep 交替匹配 | 部分环境不支持，用 `grep -E` 或专用搜索工具 |
| 替用户决定许可证 | 问；未指定时明确说明默认保留所有权利 |
