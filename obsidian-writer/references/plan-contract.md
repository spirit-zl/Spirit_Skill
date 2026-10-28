# Note Plan 契约 v1.0

`plan.json` 是 **note plan**：一份「要往 vault 写哪些笔记」的稳定输入。
生产者是 `plan_from_synthesis.py`（从 knowledge-synthesis 作业转换），也可由调用方手工编写。
消费者是 `import_notes.py`。**本契约只有一份副本**，位于本 skill 内。

---

## 1. 暂存区布局

```
BRIDGE_ROOT = /Users/spirit/.workbuddy/data/obsidian-bridge

BRIDGE_ROOT/
├── plans/<YYYY>/<MM>/<plan_id>/plan.json
│                               /drafts/<note_id[:8]>-<slug>.md
│                               /receipts/<attempt_id>.json
├── index/import-index.json
└── locks/import.lock
```

**plan 目录必须落在 vault 之外**：vault 位于 git 仓库（`/Users/spirit/Spirit_Obsidian`）、
`.gitignore` 为空、且装了 `remotely-save`。草稿一旦进入 vault 会被 git 跟踪并可能同步到第三方存储。

---

## 2. 标识规范

所有 hash 均为 **SHA-256，十六进制小写**。

| 标识 | 计算方式 | 用途 |
|---|---|---|
| `source_key` | `sha256(origin + "\0" + locator)` | 定位**来源文档**（locator 稳定，内容更新不影响） |
| `content_sha256` | 来源正文规范化后的 sha256 | 判断来源内容是否变化 |
| `group_key` | `ks:<sha256(规范化标题)[:16]>` | 定位**一篇目标笔记**（身份 = 标题） |
| `input_fingerprint` | 见 §5 | 判断整篇笔记是否需要重新生成 |
| `note_id` | UUIDv4 | 笔记唯一标识，用于认领与冲突判定 |

`locator` 按来源类型取值：ima → `media_id`；飞书 → 文档 token 或原链接；本地 → 绝对路径。

---

## 3. `plan.json` 结构

```json
{
  "schema_version": "1.0",
  "plan": {
    "plan_id": "20260928-144500-local",
    "status": "ready",
    "created_at": "2026-09-28T14:45:00+08:00",
    "producer": "knowledge-synthesis",
    "producer_job_id": "20260928-142030-local",
    "producer_manifest": "/abs/path/manifest.json"
  },
  "job_id": "20260928-142030-local",
  "source_system": "ima|lark|local",
  "source_scope": {"id": "kb-id 或目录绝对路径", "name": "知识库名或目录名"},
  "policy": {
    "summary_profile": "knowledge-note-zh",
    "summary_profile_version": "1.0",
    "allow_partial": false,
    "partial_approved_at": null
  },
  "sources": [
    {
      "source_key": "sha256-hex",
      "source_id": "S1",
      "title": "原文标题",
      "origin": "local",
      "locator": "/abs/path/report.md",
      "breadcrumb": ["父目录"],
      "content_sha256": "sha256-hex",
      "chars": 12345,
      "completeness": "no_truncation_detected"
    }
  ],
  "notes": [
    {
      "note_id": "uuid-v4",
      "group_key": "ks:<16 位 hex>",
      "aggregation_mode": "single|merged",
      "source_keys": ["sha256-hex"],
      "input_fingerprint": "sha256-hex",
      "proposed_title": "笔记标题",
      "draft_uri": "job://drafts/abcd1234-笔记标题.md",
      "draft_sha256": "sha256-hex",
      "status": "ready",
      "generated_at": "2026-09-28T14:45:00+08:00"
    }
  ]
}
```

### 必填字段（消费者强制校验）

- 顶层：`schema_version`、`plan`、`source_system`、`sources`、`notes`
- `plan.plan_id`、`plan.status`、`plan.created_at` 非空
- 每个 source：`source_key`、`origin`、`locator`、`content_sha256`
- 每个 note：`note_id`、`group_key`、`source_keys`、`input_fingerprint`、
  `proposed_title`、`draft_uri`、`status`
- 每个 note 的 `source_keys` 必须全部能在 `sources` 中找到

> 来源**不要求**把正文快照复制进 plan 目录：正文快照留在上游 job 里，
> 这里只保留定位信息与指纹。真正需要完整性校验的是**草稿**（`draft_sha256`）。

---

## 4. `job://` 受限 URI

草稿引用一律使用 `job://` 前缀，禁止绝对路径与 `../`。

```
job://drafts/abcd1234-笔记标题.md
```

解析规则：

1. 必须以 `job://` 开头
2. 去掉前缀后的相对路径不得包含 `..`
3. 不得为绝对路径
4. 解析后必须 `realpath` 校验仍在 **plan 所在目录**内
5. 目标不得为符号链接逃逸
6. 目标必须存在且为普通文件

任一不满足即视为**恶意或错误输入**，拒绝导入。

---

## 5. `input_fingerprint` 生成规则

```
payload = "\n".join([
    group_key,
    summary_profile_version,
    draft_sha256,
    *[ f"{s.source_key}:{s.content_sha256}:{s.completeness}"
       for s in sorted(sources_of_this_note, key=lambda x: x.source_key) ]
])
input_fingerprint = sha256(payload)
```

含义：group 相同、模板版本相同、**草稿内容相同**、来源集合与各自内容指纹相同 →
重复运行**完全幂等**。

`draft_sha256` 必须计入，否则「人工补完草稿后重新导入」会被判为已导入而跳过，
改动永远进不了 vault。计入之后，任何草稿改动都会自然生成**修订版**，
与「永不覆盖 + 修订」的语义一致。

注意：`plan_from_synthesis.py` 生成的草稿是**确定性**的（正文取自上游成文 +
按来源元数据拼接的溯源信息），所以同一个上游 job 反复 build 得到同一个指纹，
不会因为重跑而凭空产生修订。

---

## 6. 状态机

### plan 状态

| 状态 | 含义 | 可否导入 |
|---|---|---|
| `ready` | 全部就绪 | **是** |
| `ready_noop` | 全部已导入，无需生成文件 | **是**（应报告 0 创建） |
| `partial` | 存在缺失/截断/被跳过的来源 | **仅在 `policy.allow_partial=true` 且 `policy.partial_approved_at` 非空时** |
| `failed` | 不可恢复 | 否 |

### note.status

`ready` | `skipped` | `blocked` | `summary_error`

### source.completeness

- `no_truncation_detected` — **不宣称完整**，仅表示未检测到截断迹象
- `suspected_truncated` — 启发式命中截断迹象
- `empty` — 内容过短，信息量有限
- `parse_failed` — 解析失败（不应出现在 `sources` 里，上游已剔除）

---

## 7. 幂等与修订规则

消费者在 `locks/import.lock` 持锁下读 `index/import-index.json` 做最终判定：

| 情况 | 行为 |
|---|---|
| 同 `group_key` + 同 `input_fingerprint` 已成功导入，且笔记文件仍在 | **跳过**（`skipped_already_imported`） |
| 同 `group_key`，`input_fingerprint` 不同 | 生成**新修订**：新文件 + `supersedes` 指向旧笔记 + `revision` 递增，**不动旧文件** |
| 文件已写但索引未更新（崩溃恢复） | 用 frontmatter 的 `note_id` + `input_fingerprint` + 文件 hash **认领**，不重复写 |
| 同 `group_key` 但标题变了 | 标题即身份 → 变成**另一篇笔记**，不是修订 |
| 同名文件但不同 `note_id` | 加 `--<note_id[:8]>` 后缀，**永不覆盖** |
| 同名且文件 hash 不符 | 加后缀或中止，**绝不覆盖** |

用户显式要求强制重建时加 `--force`（仍生成修订，不覆盖）。

---

## 8. 回执 `receipts/<attempt_id>.json`

消费者**不得修改** plan（plan 是稳定输入），结果另写回执：

```json
{
  "attempt_id": "uuid",
  "job_id": "上游 job id",
  "plan_path": "/abs/path/plan.json",
  "plan_digest": "sha256",
  "mode": "apply",
  "status": "success|partial_success|noop|failed",
  "notes": [
    {"note_id": "...", "status": "created|skipped_already_imported|claimed|conflict|failed",
     "vault_uri": "vault://000inbox/note/xxx.md", "note_sha256": "...",
     "revision": 1, "supersedes": null}
  ]
}
```

---

## 9. 禁止事项

- 生产者不得写入 Obsidian vault
- 消费者不得调用 ima / 飞书 / 本地数据源、不得重新总结正文
- 双方均**不得**执行 `git add` / `git commit` / `git push`
- 不得生成 Dataview / Templater / Excalidraw / `<script>` 语法（vault 未装这些插件）
- 不得虚构来源 URL、页码、段落号

---

## 10. 版本兼容

消费者遇到**主版本**不同的 `schema_version` 直接拒绝，不做兼容猜测。
次版本差异（如 `1.1` 对 `1.0`）允许：忽略未知字段，但仍校验 §3 的必填字段。
