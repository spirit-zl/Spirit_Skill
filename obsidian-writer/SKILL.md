---
name: obsidian-writer
description: 把已经成文的汇总稿按 Obsidian 规范写入本机 vault。当用户要求「写入 Obsidian / 导入 Obsidian / 存进知识库 / 放进 000inbox / 把这份总结写进 Obsidian / 迁进 Obsidian / import into Obsidian / write note to Obsidian」，或 knowledge-synthesis 完成汇总后要求把结果落到 Obsidian 时使用。它消费 note plan（或由 knowledge-synthesis 的作业产出转换而来），做契约校验、幂等去重、修订判定、原子写文件、维护 vault 外的导入索引。它不采集资料、不重新总结正文、不调用 ima/飞书、永不执行 git commit。
agent_created: true
---

# obsidian-writer

消费 note plan，把成文草稿按 Obsidian 规范原子写入 vault 的 `000inbox/note`，
并维护 vault 之外的去重索引与回执。

## 何时使用

- 用户拿着一个 note plan 路径或 knowledge-synthesis 的 job 要求写入 / 导入 Obsidian
- 上游 `knowledge-synthesis` 产出成文后，用户要求落进 Obsidian
- 导入中断后要求重放，或索引损坏需要重建

## 核心边界

- ✅ 读 plan、校验契约、写 vault、维护索引与回执
- ❌ **不采集**资料、**不重新总结**正文（内容由上游成文决定）
- ❌ **不调用** ima MCP / 飞书 CLI / 任何数据源
- ❌ **永不覆盖**已存在的笔记文件
- ❌ **不执行** `git add` / `git commit` / `git push`
- ❌ 校验不通过一律不写 vault；`partial` 未经用户批准一律不写

## 路径常量

```
vault        /Users/spirit/Spirit_Obsidian/Spirit知识库
落点         000inbox/note
暂存根       /Users/spirit/.workbuddy/data/obsidian-bridge
上游 job 根  /Users/spirit/.workbuddy/data/knowledge-synthesis
脚本         scripts/plan_from_synthesis.py  import_notes.py  rebuild_index.py
契约         references/plan-contract.md
规范         references/obsidian-writing-standard.md
上游交接     references/from-knowledge-synthesis.md
模板         assets/obsidian-note-template.md
Python       /Users/spirit/.workbuddy/binaries/python/envs/default/bin/python
```

下文所有 `python3` 一律替换成上面这个解释器路径。

## 输入有两种

### A. 从 knowledge-synthesis 的作业生成 plan（常规路径）

```
python3 scripts/plan_from_synthesis.py build \
  --manifest /Users/spirit/.workbuddy/data/knowledge-synthesis/<job_id>/manifest.json
```

细节（含 `[S<n>]` 锚点如何决定引用来源、`group_key` 如何由标题决定）见
`references/from-knowledge-synthesis.md`。

### B. 直接用已有 plan

调用方手工编写的 plan 也行，但必须满足 `references/plan-contract.md`。
不得靠"猜最新的 plan"——**存在多个 plan 时必须让用户指定**。

## 工作流

### 1. 确认输入与 vault

拿到**明确的绝对路径**（plan.json，或上游 manifest.json）。确认落点是
`000inbox/note`。首次为某用户运行时提示：vault 装了 `remotely-save`，
写进去的内容可能被同步到第三方存储。

### 2. 先 dry-run

```
python3 scripts/import_notes.py --plan <P> \
  --vault-root "/Users/spirit/Spirit_Obsidian/Spirit知识库" --mode check
```

输出每条 note 的 `create` / `skipped_already_imported` / `claimed` / `blocked`。

**以下情况必须停下向用户确认，不得直接 apply：**

- `is_partial: true`
- `blocked > 0`（缺一级标题、多一级标题、含 dataview 等禁用语法）
- 出现 `conflict`
- 批量异常地大

`blocked` 的处理：补草稿让其符合 `obsidian-writing-standard.md` 的结构，
再跑 `plan_from_synthesis.py refresh --plan <P>` 重算指纹，然后重新 check。

### 3. 执行导入

```
python3 scripts/import_notes.py --plan <P> \
  --vault-root "/Users/spirit/Spirit_Obsidian/Spirit知识库" --mode apply
```

脚本完成：校验契约 → 解析 `job://` 草稿（拒绝 `..` 与符号链接逃逸）→ 去重判定 →
规划文件名 → 渲染 frontmatter → 规则检查 → 原子写文件 → 原子更新索引 → 写回执。

### 4. 汇报

说明：创建 / 跳过 / 认领 / 冲突数量、回执与索引路径、**声明未执行任何 git 写操作**。
可只读执行：

```
git -C "/Users/spirit/Spirit_Obsidian" status --short
```

## 去重语义（勿自行重写）

| 情况 | 行为 |
|---|---|
| 同 `group_key` + 同 `input_fingerprint`，文件仍在 | 整篇跳过 |
| 同 group，fingerprint 变了（来源更新） | 生成**新修订**：新文件 + `supersedes` + `revision` 递增，**不动旧文件** |
| 文件已写但索引未更新（崩溃恢复） | 用 frontmatter 的 `note_id` + `input_fingerprint` **认领**，不重复写 |
| 改了标题 | 标题即身份 → 是**另一篇笔记**，不是修订 |
| 同名但不同 note | 加 `--<note_id前8位>` 后缀，永不覆盖 |

用户显式要求强制重建时加 `--force`（仍生成修订，不覆盖）。

## 索引损坏时

```
python3 scripts/rebuild_index.py --vault-root "/Users/spirit/Spirit_Obsidian/Spirit知识库" --mode check
python3 scripts/rebuild_index.py --vault-root "/Users/spirit/Spirit_Obsidian/Spirit知识库" --mode apply
```

从笔记 frontmatter 重建（apply 前自动 `.bak`）。兼容读取旧版 `ima_*` 字段。

## 常见错误

| 错误 | 正确做法 |
|---|---|
| 没有 plan 就去重新采集/总结 | 退回 `knowledge-synthesis`，本 skill 不生产内容 |
| partial 直接 apply | 先 `check`，并向用户说明缺什么、取得明确批准 |
| 改了草稿却直接导入 | 先 `plan_from_synthesis.py refresh` 重算 `draft_sha256`（指纹含草稿，不改会被判为不符；改了会生成修订版） |
| build 输出里 `unresolved_anchors` 非空却不管 | 正文的 `[Sn]` 在上游找不到，溯源信息会对不上，回上游核对 |
| 覆盖同名文件 | 永不覆盖，加后缀或中止 |
| 为了让笔记"更好看"而改标题 | 改标题会断开修订链；确需改就先说明后果 |
| 导入后顺手 git commit | 禁止，只做只读 `git status` |
| 手工编辑 plan 里的 `note_id`/`input_fingerprint` | 这两个字段是幂等依据，改了会重复导入 |
