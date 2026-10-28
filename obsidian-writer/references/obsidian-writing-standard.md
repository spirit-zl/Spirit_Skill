# Obsidian 写作规范

本机 vault：`/Users/spirit/Spirit_Obsidian/Spirit知识库`
写入落点：`000inbox/note`（与 `.obsidian/app.json` 的 `newFileFolderPath` 一致）

---

## 1. Vault 现状（决定了规范可以自由定义）

- 近乎空白：仅 1 篇历史 md，**没有既有 frontmatter 约定**，无历史包袱
- 链接语法：wikilink（`app.json` 的 `newLinkFormat: relative` + `alwaysUpdateLinks: true`）
- 核心插件启用：`daily-notes` / `templates` / `properties` / `tag-pane` / `bases`
- **`zk-prefixer` 关闭** → 文件名不加 Zettelkasten ID 前缀
- 社区插件：`realclaudian` / `drawio-obsidian` / `editing-toolbar` / `obsidian-enhancing-mindmap` / `remotely-save` / `wechat-format`
- **未安装 Dataview / Templater / Excalidraw**

---

## 2. Frontmatter（固定字段，由脚本生成）

```yaml
---
title: "笔记标题"
created: "2026-09-28T15:00:00+08:00"
updated: "2026-09-28T15:00:00+08:00"
source: "ima"            # ima | lark | local
tags:
  - "ai-summary"
  - "local"
source_scope: "/abs/root 或 kb-id"       # 范围标识，可空
source_scope_name: "范围名"               # 人读范围名，可空
source_ids:              # 来源定位：media_id / 文档 token / 绝对路径
  - "..."
source_titles:
  - "来源一"
source_hashes:
  - "sha256:..."
job_id: "上游 job id"
note_id: "note-uuid"
note_key: "ks:<16 位 hex>"
input_fingerprint: "sha256"
aggregation: "single|merged"
revision: 1
partial: false
supersedes: null
---
```

规则：

- 结构**扁平**，不使用嵌套对象（`properties` 插件才能正常显示）
- 所有 ID **一律字符串并加引号**，防止科学计数法或类型漂移
- `source_ids` **恒为数组**，单篇时也是单元素数组（避免单/多篇类型不一致）
- `tags` 内**不加 `#`**；用扁平单级 tag（无 Dataview，多级 tag 无法查询渲染）
- `created` 是笔记生成时间，**不冒充原文创建时间**
- `partial: true` 时正文必须含醒目不完整说明
- 修订版：`supersedes: "[[旧笔记文件名|旧版笔记]]"` + `revision` 递增
- **不可手工改名 `note_id` / `note_key` / `input_fingerprint`**：它们是幂等判定的依据，
  改了会导致重复导入

> 旧版字段（`ima_note_id` / `ima_group_key` / `ima_media_ids` 等）只在**读取历史笔记**时兼容，
> 新写入一律用上表的通用名。

---

## 3. 正文结构

按 `assets/obsidian-note-template.md`。必要章节：

| 章节 | 必需 |
|---|---|
| `# 一级标题`（有且仅有一个） | ✅ |
| `## 摘要` | ✅ |
| `## 关键结论` | ✅ |
| `## 原文要点` | 建议 |
| `## 综合与差异`（多篇） | 建议 |
| `## 待核验与局限` | 建议 |
| `## 溯源信息` | ✅ |

来源引用统一用 `[S1]` `[S2]` 编号，与溯源信息一一对应。

**写 vault 之前必须先让草稿符合本结构**：`import_notes.py --mode check` 会把
缺一级标题、多一级标题、含禁用语法判为 `blocked`（拒绝导入）；缺建议章节只给 warning。
缺章节时先补进草稿，再用 `plan_from_synthesis.py refresh` 重算草案指纹。

---

## 4. 文件命名

`scripts/import_notes.py` 自动处理：

1. Unicode NFC 归一化
2. 替换 `\ / : * ? " < > |` 与控制字符为 `-`
3. 空白字符转 `-`，折叠连续 `--`
4. 去除首尾空格、点号、连字符
5. 截断至 120 字符
6. 空标题回退 `note-YYYYMMDD-HHmmss.md`

冲突策略：
- 目标已存在 → `标题--<note_id前8位>.md`
- 仍存在 → `标题--updated-YYYYMMDD-HHmmss.md`
- **永不覆盖已存在文件**

---

## 5. 链接

- 同目录笔记：`[[文件名|显示标题]]`
- 附件：`![[../attachment/文件名.png]]`
- 不写 vault 绝对路径
- 只为**确认存在**或本次生成的笔记建链接，**不虚构** wikilink
- **不虚构**来源网页 URL——上游只保证 `locator`（media_id / token / 路径），不保证可打开链接

---

## 6. 明令禁止

以下语法在 vault 未装相应插件时会退化成纯文本或报错：

- ` ```dataview ` / ` ```dataviewjs ` 代码块
- Dataview inline query
- Templater `<% ... %>`
- Excalidraw 专用块
- HTML `<script>` 及可执行内容
- 伪造的页码、段落号、来源链接

脚本会在导入前拦截，命中即 `blocked` 并中止。

---

## 7. Git 与同步注意事项

- 仓库根 `/Users/spirit/Spirit_Obsidian`，`.gitignore` 为空
- 本 skill **永不执行** `git add` / `git commit` / `git push`
- 结束后可只读执行 `git -C "/Users/spirit/Spirit_Obsidian" status --short` 汇报
- 已装 `remotely-save`：写入 vault 的笔记（含 frontmatter 里的 locator）可能被同步到第三方存储，
  首次导入前应提示用户
