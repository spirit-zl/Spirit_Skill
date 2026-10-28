# Spirit_Skill

平台无关的技能中央仓库。**所有技能的唯一真身都在这里**，任何 AI 平台（WorkBuddy、Claude Code、CodeBuddy、DeepSeek Harness、Cherry Studio……）都指向同一份文件，不产生副本、不产生分叉。

## 核心约定

| 规则 | 说明 |
|---|---|
| 一个技能 = 一个目录 | 目录名即技能名，内含 `SKILL.md` |
| 元数据用最小公约数 | frontmatter 只要有 `name` + `description`，所有平台都认 |
| 平台目录只放链接 | 平台侧不存副本，永远读到最新版 |
| 平台目录本身必须是真实目录 | **绝不能**把平台技能根目录整体换成软链接（见下方事故记录） |
| `_` 前缀是系统目录 | 如 `_hub/`，不参与技能分发 |

## 目录结构

```
Spirit_Skill/
├── INDEX.md              # 技能总览（自动生成，任何 agent 读它就能用）
├── README.md             # 本文件
├── _hub/                 # 工具区，不对外分发
│   ├── platforms.json    # 平台接入点配置（要接新平台改这里）
│   ├── sync.py           # 正向分发：仓库 → 各平台
│   ├── collect.py        # 反向收纳：各平台 → 仓库
│   ├── doctor.py         # 体检
│   └── backups/          # 备份与归档
├── content-collect/
│   ├── SKILL.md
│   ├── scripts/          # 可选
│   ├── references/       # 可选
│   └── assets/           # 可选
└── ...
```

## 三个命令

```bash
# 1) 分发到各平台（每次新增/删除技能后跑一次）
python3 _hub/sync.py

# 2) 把平台上新建/改过的技能收回来（在别的平台创建了技能之后跑）
python3 _hub/collect.py            # 先预览
python3 _hub/collect.py --apply    # 确认后执行

# 3) 体检
python3 _hub/doctor.py
```

`sync.py` 常用参数：

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只显示计划，不动文件 |
| `--all` | 连 `enabled=false` 的平台也接入 |
| `--platform workbuddy` | 只处理指定平台 |
| `--prune` | 清理仓库里已删技能的残留链接 |

## 日常工作流

**场景 A：在任意平台创建了新技能**
1. 让 AI 把技能直接写到 `/Users/spirit/Spirit_Skill/<技能名>/`（首选），或让它在平台目录里创建
2. 如果在平台目录里创建的 → `python3 _hub/collect.py --apply` 收回仓库
3. `python3 _hub/sync.py` 分发到所有平台
4. 重启对应平台，技能即生效

**场景 B：修改已有技能**
直接在 `/Users/spirit/Spirit_Skill/<技能名>/` 里改。因为平台侧是软链接，**所有平台立刻读到新版**，无需同步。

**场景 C：新接一个平台**
1. 在 `_hub/platforms.json` 里加一条记录，填 `skillsDir`
2. `python3 _hub/sync.py --platform <id>`

## 各平台接入方式

| 平台 | 接入点 | 方式 |
|---|---|---|
| WorkBuddy | `~/.workbuddy/skills` | 逐技能软链接（受管目录，见下） |
| Agents 标准 | `~/.agents/skills` | 逐技能软链接 |
| Claude Code | `~/.claude/skills` | 逐技能软链接 |
| CodeBuddy CLI | `~/.codebuddy/skills` | 逐技能软链接 |
| DeepSeek Harness | 无原生技能目录 | 读 `INDEX.md` + 绝对路径，或封装 cordis 插件 |

没在列表里的平台：只要它支持 `SKILL.md` 约定，加一条 `platforms.json` 就能接；不支持的话，让它读 `INDEX.md`——那里面列了每个技能的绝对路径和用途。

## 事故记录（重要，别重犯）

**2026-10-28**：把 `~/.workbuddy/skills` 整个目录换成了指向本仓库的软链接。WorkBuddy 作为受管目录会往技能目录里写 `_user_meta.json` 并做清理，结果**穿透软链接删掉了本仓库里的 9 个技能目录**（好在有备份，已完整恢复）。

**根因**：平台技能根目录是它自己的地盘，它会清理、会写元数据。把真实数据放在它的软链接目标后面，等于把真身交给它管。

**现在的做法**：平台根目录保持真实目录，里面每个技能一条软链接。平台清理最多删掉链接，删不到真身。

**推论**：`sync.py` 会检测平台目录是否为软链接，一旦发现直接报错退出，就是为了防这个。

## 元数据规范

```yaml
---
name: my-skill              # 必填，建议与目录名完全一致
description: 一句话说明用途 + 触发词   # 必填，建议 ≤200 字符
---
```

**description 要短**：平台注入技能清单时有全局字符预算（如 WorkBuddy 是 8000 字符分给所有技能），描述过长会被截断到几十字符，触发词全部丢失，技能就"在列表里但叫不出来"。长触发词列表放到正文里，别塞进 frontmatter。

其他字段（`version`、`author`、`agent_created`、`display_name` 等）是平台私有扩展，写上不影响别的平台解析，但别指望别的平台认。

## 备份

- 快照：`_hub/backups/`
- 建议本仓库纳入 git，任何误删都能 `git checkout` 回来
