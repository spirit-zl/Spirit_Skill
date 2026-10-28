# 飞书源采集

所有命令统一加 `--as user`（bot 常缺 drive/search scope）。采集器把内容落到
`<job_dir>/lark_raw/` 后，**复用本地解析器**转成 IR——不自己解析 DocxXML。

## 1. 解析入口

```
lark-cli drive +inspect --url "<链接>" --as user
```

返回 `type`（doc/docx/sheet/bitable/wiki/file/folder/mindnote/slides）、`title`、规范 `token`。
批处理：

```
python3 scripts/collect_lark.py collect --manifest <M> --url "<链接>" --expand auto
```

`--expand`：
- `auto`（默认）：wiki → 递归子节点；folder → 递归子文件与子文件夹；单文档 → 仅自身
- `none`：只要入口文档本身
- `links`：额外解析正文里的飞书链接并纳入（即"正文引用的子文档"）
- `depth N`：限制递归层数，默认 10

## 2. 展开

先取入口信息确认 space-id（wiki 节点必须有 space-id 才能列子节点）：

```
lark-cli wiki +node-get --node-token "<NODE_TOKEN>" --as user --format json
```

| 入口类型 | 命令 |
|---|---|
| wiki 节点 | `lark-cli wiki +node-list --space-id <space> --parent-node-token <node> --page-all --page-limit 0 --page-size 50 --as user --format json`，对 `has_child` 的节点继续向下递归 |
| 云盘文件夹 | `lark-cli drive files list --folder-token <token> --page-size 200 --as user`（循环 `--page-token` 翻页） |
| 纯文件/文档 | 直接进入第 3 步 |

- 递归时维护 visited 集合防环。
- **`--page-limit 0` 才是不限页**；写成 `--page-limit 20` 会把大知识库的节点截断，看起来"抓全了"其实漏了。
  确实只需要前 N 页时才显式给小值。
- 节点列表的返回体读 **`data.nodes`**（不是 `data.items`）。
- **`node_token` 与 `obj_token` 不是一回事**：列子节点、递归下钻用 `node_token`；
  导出/读取正文内容要用 `obj_token`。混用会导致"能列出节点但导不出内容"。
- `drive +inspect` 对 wiki 入口常常**不返回 `space_id`**，而 `--parent-node-token` 缺了 space-id
  就列不出子节点（整棵树会静默丢失）。入口是 wiki 时补一次
  `lark-cli wiki +node-get --node-token <token> --as user` 取 `space_id`。
- 每个节点要记录 `obj_type`（docx/doc/bitable/file）、`obj_token`、`node_token`、`title`、
  `_depth` 与 `_path`（祖先标题链，落进 IR 的 `breadcrumb`）。
- `drive +pull` **只能下载 type=file，会跳过所有在线文档**，不要用它做正文采集。

## 2.1 并发与限流

- 批量拉正文用 **`max_workers=2`**，这是实测最稳的值；再高会触发限流。
- 做成**可重入**：跳过已存在的本地文件，失败项多轮重跑（跑 2-3 轮直到无失败）。
- 限流告警不要当成失败终止；隔一段时间重跑即可，已拉到的不会重复拉。

## 3. 取正文（优先导出）

```
lark-cli drive +export --url "<链接>" --file-extension markdown --output-dir <job_dir>/lark_raw --as user
```

| 类型 | `--file-extension` |
|---|---|
| docx / doc / wiki | `markdown`（首选）或 `docx` |
| sheet | `xlsx` 或 `csv`（csv 需 `--sub-id`） |
| bitable | `base` |
| slides | `pptx` |

- 导出是**异步任务**，CLI 只做有限轮询；返回 `task_id` 未完成时补：
  `lark-cli drive +task_result --token <task_id>`
  **不要在任务未完成时就去解析产物**，会拿到空文件。
- 导出失败（权限、超大、类型不支持）→ 回退：
  `lark-cli docs +fetch --doc "<token|url>" --doc-format markdown --detail simple --as user`
- 两者都失败 → 登记 `parse_failed` + `no_permission` issue，跳过（partial 允许）。

## 3.1 特殊类型

| 类型 | 做法 |
|---|---|
| 多维表格 bitable | `lark-cli drive +export --token <bitable_token> --doc-type bitable --file-extension markdown --as user --output-dir <job_dir>/lark_raw`。若导出结果只是图片画册、没有文字，**记下"无文字信息"即可，不要编造内容** |
| PDF / 附件 | `lark-cli drive +download --token <file_token> --as user --output-dir <job_dir>/lark_raw`，落盘后走本地解析器（PDF 文本层为空时自动转 Vision OCR，见 `source-local.md`） |
| sheet | `--file-extension xlsx`（或 `csv`，csv 需 `--sub-id`），落盘后走本地表格解析 |
| slides | `--file-extension pptx` |
| mindnote | 导出 markdown；失败按 `parse_failed` 记录 |

导出物一律落到 `<job_dir>/lark_raw/`，**复用 `parsers.py`** 转 Markdown 写 IR，不直接解析 DocxXML。

## 4. 配图

```
lark-cli docs +media-download --token <file_token> --output <job_dir>/img/<id>.png --as user
```

图片 token 从 `lark-cli docs +fetch --doc <t> --detail full` 的 image block 里取。
取不到就降级：保留图注文字，`status=degraded`。

## 5. 已知坑

- 用户身份 token 会过期（约 24h），`auth status` 报 `tokenStatus != valid` 时先刷新再采集。
- wiki 节点与底层文档是两套 token：`+inspect` 已做 unwrap，直接用返回值。
- 超大知识库 `--page-all` 必须配 `--page-limit 0`（不限页）；给了小值会静默截断节点列表。
- 同一文档可能在 wiki 与云盘同时出现 → 交给 `normalize.py` 按内容指纹去重。
- 几百篇的抓取会让 `<job_dir>/lark_raw/` 涨到很大，收尾时提醒用户可清理（删除前先确认）。
