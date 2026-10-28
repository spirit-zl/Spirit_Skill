# IR（中间表示）契约

所有采集器落盘为同一份 JSONL：`<job_dir>/ir.jsonl`，一行一篇资料。
`ir.jsonl` 是唯一进入综合阶段的输入；正文不进对话上下文，只在需要时由脚本分块喂给模型。

## 字段

```json
{
  "source_id": "S1",
  "source_key": "local:<sha256前16>",
  "origin": "local | ima | lark",
  "title": "原文标题",
  "path_or_url": "/abs/path/file.pdf | https://... | doc token",
  "breadcrumb": ["父目录", "子目录"],
  "media_type": "PDF | DOCX | MD | TXT | HTML | PPTX | XLSX | DOCX_FEISHU | SHEET | IMG | OTHER",
  "modified_at": "2026-09-01T10:00:00+08:00",
  "author": "",
  "content_file": "<job_dir>/raw/S1.md",
  "content_chars": 12345,
  "content_sha256": "<去空白后正文 sha256>",
  "anchors": ["p12", "sec-3.2", "block_xyz"],
  "completeness": "no_truncation_detected | suspected_truncated | parse_failed | empty",
  "parse_note": "",
  "images": [{"id":"I1","file":"<job_dir>/img/S1_p12.png","page":12,"caption":"","ocr":"","status":"ok|degraded|failed"}],
  "duplicate_of": null,
  "priority": 3,
  "extra": {}
}
```

## 约束

- `source_id` 在 job 内唯一，按采集顺序 `S1…Sn`；**全局成文阶段的引用锚点就用它**（`[S12]`）。
- `source_key` 跨 job 稳定：`origin:<content_sha256前16>`，用于摘要缓存命中。
- `content_file` 必须是 Markdown 文本（PDF/PPTX/XLSX/DOCX 均已转换），UTF-8。
- `completeness` 只能由采集器按判据填写，**禁止事后改写成 `no_truncation_detected`**。
- `parse_failed` 的条目仍然登记（便于报告），但综合阶段跳过。
- `duplicate_of` 填被保留条的 `source_id`；重复条不进综合。
- `priority` 用于去重仲裁：local=3、lark=2、ima=1，数值大者保留。

## 完整性判据

| 类型 | 判据 |
|---|---|
| ima PDF | 正文末章/附录是否出现；`fetch_media_content` 上限约 10.5 万字符，接近即 `suspected_truncated` |
| 本地 PDF | 抽取文本页数 < 总页数且文本层为空 → 扫描件，走 OCR，成功仍标 `no_truncation_detected`，OCR 失败标 `parse_failed` |
| 飞书 | `docs +fetch` 返回为空但文档存在 → `parse_failed`；返回被分页截断则翻页取全 |
| 任何 | 正文长度 < 50 字符且有实质内容预期 → `parse_failed` |

## 图片条目

`images[]` 只在 `image_policy=recover` 时填充。每个图必须有 `page` 或 `anchor`，
`ocr` 字段存该图的文字（用于图注与页图对齐）。取不到图的：`status=failed` + `caption` 文字描述。
