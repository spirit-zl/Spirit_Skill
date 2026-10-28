# Manifest 契约

路径：`<暂存根>/<job_id>/manifest.json`，暂存根 `~/.workbuddy/data/knowledge-combine`。

## 结构

```json
{
  "job_id": "20260928-114000-local",
  "created_at": "2026-09-28T11:40:00+08:00",
  "source": {
    "type": "local",
    "selection": {"mode": "folder", "path": "/abs/dir", "recursive": true},
    "filters": {"max_docs": 200, "include": [], "exclude": [], "updated_after": null}
  },
  "output": {"target": "local", "location": "/abs/out", "format": "md", "mode": "create"},
  "policy": {
    "max_chars_per_chunk": 12000, "chunk_overlap": 300,
    "max_docs": 200, "confirm_threshold": 50,
    "allow_partial": true, "image_policy": "recover"
  },
  "authorized_scopes": ["local:/abs/out/"],
  "intent": {"topic": "", "audience": "", "genre": "", "target_words": 4000},
  "sources": [],
  "images": [],
  "chunks": [],
  "summaries": {"source": {}, "group": {}, "global": null},
  "outline": null,
  "outputs": [],
  "issues": [],
  "status": "draft"
}
```

## 状态机

```
draft → collected → normalized → synthesized → emitted → ready | partial | failed
```

- `validate` 在 `emitted` 之后运行，检查：至少 1 篇 source、至少 1 个 output、
  所有 output 都有落盘证明、`issues` 中无 `severity=error`。
- `finalize` 自动判：`issues` 中有 skip/failed 源 → `partial`；否则 `ready`。
- 输出完全没落盘 → `failed`（`allow_partial=true` 也不豁免）。

## issues 条目

```json
{"level": "source|image|output", "ref": "S12", "kind": "parse_failed|suspected_truncated|no_permission|duplicate|upload_failed",
 "severity": "warning|error", "message": "...", "at": "ISO8601"}
```

- `severity=error` 会阻塞 `validate`；只有输出落盘失败才置 error。
- 其余一律 `warning`，允许 partial 推进。

## 授权 scope

- local：`local:<abs 目录>/`
- lark：`lark:folder:<token>` 或 `lark:wiki:<node_token>`
- ima：`ima:kb:<kb_id>/folder:<folder_id>`（无 folder 时为 `ima:kb:<kb_id>/root`）

`authorize` 只在 scope 字符串不在 `authorized_scopes` 时需要用户确认；已存在则直接返回 ok。

## 交接面（交给 obsidian-writer 时）

本技能不写 Obsidian，但 job 目录是下游唯一的数据来源。下游会读：

| 内容 | 用途 |
|---|---|
| `manifest.json` 的 `sources[]` | 每篇来源的 `source_id` / `title` / `origin` / `completeness` |
| `manifest.json` 的 `outputs[]` | 已落盘成文的路径（下游据此找正文） |
| `manifest.json` 的 `status` | `partial` 时下游会要求用户批准 |
| `ir.jsonl` 的 `path_or_url` | 作为来源 `locator`（media_id / 文档 token / 绝对路径） |
| `ir.jsonl` 的 `content_sha256` | 作为来源正文指纹 |
| 成文正文里的 `[S<n>]` | 决定该篇笔记引用了哪些来源 |

因此：**成文里的 `[S<n>]` 锚点不要删**（见 `synthesis-protocol.md` 的「引用锚点」），
`ir.jsonl` 不要手工改动。这两条断了，下游就无法正确建立溯源信息与幂等指纹。
