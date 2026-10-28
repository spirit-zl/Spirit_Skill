# 从 knowledge-synthesis 生成 note plan

上游 `knowledge-synthesis` 只负责产出**成文 md**，不碰 Obsidian。本 skill 负责把它的作业
转成 note plan 并写入 vault。两者唯一的交接面是 KS 的 job 目录。

## 1. 交接面：KS job 目录里的三样东西

```
<job_dir>/manifest.json     sources[]（source_id / title / origin / completeness）
                            outputs[] （emit 到本地的成文 md 路径）
                            status    （ready | partial | failed）
<job_dir>/ir.jsonl          每条来源的 path_or_url（即 locator）、content_sha256、origin
<job_dir>/final/*.md        成文（emit 到本地时的源；也是 outputs 缺失时的兜底来源）
```

`<job_dir>` 固定为 `~/.workbuddy/data/knowledge-synthesis/<job_id>/`。

## 2. 生成 plan

```
python3 scripts/plan_from_synthesis.py build \
  --manifest /Users/spirit/.workbuddy/data/knowledge-synthesis/<job_id>/manifest.json
```

产出 `~/.workbuddy/data/obsidian-bridge/plans/<YYYY>/<MM>/<plan_id>/`：

```
plan.json
drafts/<note_id[:8]>-<标题>.md
```

转换规则（都在脚本里，不必手算）：

| 项 | 规则 |
|---|---|
| 一篇笔记 ↔ 一份成文 | 每个 `outputs[]`（或 `final/*.md`）→ 一条 note |
| `proposed_title` | 取成文里的一级标题；没有则用文件名 |
| 引用的来源 | 解析正文里的 `[S<n>]` 锚点；一个都没有时退化为「全部纳入来源」 |
| `source_key` | `sha256(origin + "\0" + locator)`，locator 取 `ir.jsonl` 的 `path_or_url` |
| `group_key` | `ks:<sha256(规范化标题)[:16]>` → **标题即笔记身份** |
| `## 溯源信息` | 草稿里没有该章节时，脚本按来源元数据自动补一节 |

`build` 的返回值里要看两个字段：

- `plan_status`：`partial` 表示上游有解析失败/截断/被去重的来源，需走 §4 的批准流程
- `unresolved_anchors`：正文里有 `[Sn]` 在上游 job 里找不到（多半是指向了已去重或解析失败的来源）。
  非空时应回到上游核对，别让笔记的溯源信息与实际来源对不上

## 3. 标题即身份（重要）

`group_key` 由标题决定，直接影响修订语义：

- **标题不变、来源内容变了** → 同一 group、`input_fingerprint` 变 → 判为**新修订**
  （新文件 + `supersedes` + `revision` 递增，旧文件保留）
- **改了标题** → 另一篇笔记，**不**构成修订链

所以：想让后续更新挂在同一篇笔记上，就**不要改标题**。

## 4. partial 的处置

KS 的 job 若有解析失败、疑似截断或被去重的来源，`status` 会是 `partial`，
转出来的 plan 也是 `partial`。此时 `import_notes.py` 会**直接拒绝导入**，必须：

1. 向用户说明缺了什么（跑 `plan_from_synthesis.py show --plan <plan.json>` 看 sources）
2. 拿到用户**明确同意**后，加 `--approve-partial` 重新 build
3. 导入后的笔记会带 `partial: true`，正文必须显著标注不完整

不得跳过这一步静默导入不完整内容。

## 5. 草稿被人工编辑后

`draft_sha256` 参与 `input_fingerprint`，所以改草稿**必须**重算，否则导入会报
「draft_sha256 与文件不符」：

```
python3 scripts/plan_from_synthesis.py refresh --plan <plan.json>
```

重算后再 `--mode check`：由于指纹变了，同一篇笔记会被判为**新修订**
（新文件 + `supersedes`，旧文件不动），**不会**覆盖原笔记、也**不会**被当成已导入跳过。
这就是"补完草稿再导入"能真正生效的原因。

## 6. 完整链路

```
knowledge-synthesis（采集→综合→emit 到本地）
        │  交出 manifest.json
        ▼
plan_from_synthesis.py build          # 生成 plan.json + drafts
        │  看输出的 unresolved_anchors / plan_status
        │  需要用 --approve-partial 时先问用户
        ▼
   （需要时人工按 obsidian-note-template 补全草稿结构）
        │
        ▼
plan_from_synthesis.py refresh         # 改过草稿才需要
        ▼
import_notes.py --mode check           # dry-run，看 create/skip/claimed/blocked
        │  有 blocked 或未批准的 partial → 停下找用户
        ▼
import_notes.py --mode apply           # 原子写入 vault + 更新索引 + 写回执
```

需要重建索引时：`rebuild_index.py --mode check` 然后 `--mode apply`。
