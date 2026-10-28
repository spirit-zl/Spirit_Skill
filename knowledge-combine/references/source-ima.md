# ima 源采集

ima 的读写走 MCP，**脚本无法代替 agent 调用 MCP**。分工：
agent 负责枚举与取正文 → 结果写进 `items.json` → 用 `scripts/collect_ima.py record` 登记成 IR。

## 1. 定位知识库

```
mcp__ima-mcp__search_knowledge_base(query="<库名>", limit=20, cursor="")
```

`knowledge_base_id` **只取工具返回值**（不得编造）；匹配范围含库名、描述、创建者昵称；
多个近似匹配必须让用户选。无结果时退回分页列举：

```
mcp__ima-mcp__get_knowledge_base_list(params=[
  {"type":"KBT_MINE_KB","limit":50,"cursor":""},
  {"type":"KBT_SHARED_KB","limit":50,"cursor":""},
  {"type":"KBT_SUBSCRIBED_JOIN_KB","limit":50,"cursor":""}
])
```

一次运行**只处理一个知识库**。

## 2. 三种定位方式

| 用户给的是 | 做法 |
|---|---|
| 目录 | 逐层 `get_knowledge_list(knowledge_base_id, limit, cursor, folder_id=<fid>, sort_type="UPDATE_TS_DESC_SORT_TYPE")` 递归 |
| 文件 | 直接 `fetch_media_content(media_id=...)` |
| 关键词 | `search_knowledge` 取候选 → **必须** `fetch_media_content` 回源取全文 |

分页一律靠 `cursor`：首页传 `""`，持续跟随返回的下一页 cursor 直到为空。
**若下一页 cursor 与历史重复 → 立即中止并记为分页异常**，不要死循环。

### 递归铁律

- **不要用 `MEDIA_TYPE_FILTER_OUT_TYPE` 过滤 FOLDER**，否则子文件夹里的文档全丢。
- 每层都不排除 FOLDER，从返回里分辨文件夹条目取 `folder_id`；维护
  `visited: set[folder_id]` 防环；自行构建 `folder_path` 父子链写进 IR 的 `breadcrumb`。
- 枚举完成后在**客户端**按 `parse_state` 分类：

| parse_state | 处置 |
|---|---|
| `MEDIA_PARSE_SUCCESS` | 可抓取 |
| `MEDIA_PARSING` | 稍后重试一轮；仍不成功按 `parse_failed` 登记 |
| `MEDIA_PARSE_ERROR` / `MEDIA_PARSE_TIMEOUT` | 不可读，列入报告 |

- **用户显式点名的文档**若不是 PARSE_SUCCESS → **阻塞任务**并告知用户，不得静默跳过。
- 文件夹/主题批量任务里存在解析中或失败的 → 列出清单，让用户选「等待重试」还是「接受 partial」。
  绝不静默丢弃。

主题检索用 `search_knowledge` 时加状态过滤，只取已解析成功的：

```
mcp__ima-mcp__search_knowledge(knowledge_base_id="<ID>", query="<主题>", cursor="",
  filters=[{"filter_type":"MEDIA_STATE_FILTER_TYPE",
            "media_state_filter":{"media_states":["MEDIA_PARSE_SUCCESS"]}}])
```

返回的是**命中片段**，**不得当正文**；只用于选候选 `media_id`，并与结构枚举结果交叉校验。

## 3. 取正文

```
mcp__ima-mcp__fetch_media_content(media_id="<MID>")
```

- **串行调用**，不要并发；按返回的分片顺序拼接；网络/工具错误重试 **1 次**，
  仍失败记入 failures（可重放），不无限重试。
- **长 PDF 上限约 10.5 万字符，超出静默丢弃**。接口没有 offset / 续读参数，
  因此**不得把「工具调用成功」等同于「全文完整」**。
- 返回体过大时工具会把结果落盘到 `tool-results/*.txt`，用 Read 分段读完再处理。
- 补全办法：本机找同名 PDF 原件（文件名 + 字节数一致）→ `scripts/images.py` 用 pymupdf 取文本层，
  无文本层则 Vision OCR。

### 3.0 截断检测信号（命中任一即标 `suspected_truncated`）

1. 工具返回中显式出现截断提示
2. 正文中止在句子、列表项、表格行或代码块中间
3. Markdown code fence 未闭合（``` 计数为奇数）
4. 响应体积接近宿主输出上限
5. 单独重试后仍在相同位置异常终止

只写 `no_truncation_detected` / `suspected_truncated`，**禁止写 `complete`**——
那是在宣称我们无法保证的事。

### 3.1 采集规模与确认

- 选中条目 **> 20 篇**时，先报数量与估算体量，再问一次是否继续。
- 疑似截断、解析失败的条目**默认阻塞**「写成完整文档」的结论：它们仍纳入，但
  其贡献的结论必须加脚注「来源可能不完整」。用户如果想直接以不完整内容推进，
  必须拿到明确同意（对应 `allow_partial`）。
- 疑似截断的处置选项（交给用户选）：拆分 ima 原文后重跑 / 手工导出全文 / 明确接受 partial。

### 3.2 增量与摘要复用

- 同一份正文（同 `content_sha256` + 同 profile 版本）再次遇到时，直接用缓存摘要，
  不重复生成：`scripts/normalize.py cache-get/cache-put`。
- 缓存 key 含 `summary_profile_version`，模板改了要升版本号，否则会拿到旧口径的摘要。

## 4. 图片（ima 侧取不到）

正文里的图片是 `5/<hash>/pdf_xxx/tmp_zip_img/<uuid>.png` 这类 **zip 内相对路径**，
拼 `https://ima-media-prod.image.myqcloud.com/<path>` 直连 **403**（私有桶需签名）。

可行路径只有一条：本机找同名原件 → pymupdf 抽图 → 与 OCR 文本按 4-gram 重合度 + **单调 DP**
（图出现页号必须非递减）对齐页号。**不要逐图 argmax**，会错配。
通常直接落整页截图更省事（同页多图合并）。

## 5. 登记

agent 把取回的内容整理成 `items.json`：

```json
[{"media_id":"...","title":"...","media_type":"PDF","folder_path":["父","子"],
  "modified_at":"","content_file":"<job_dir>/raw/tmp1.md","completeness":"no_truncation_detected"}]
```

然后：

```
python3 scripts/collect_ima.py record --manifest <M> --items items.json
```

## 6. 输出侧见 `output-targets.md`（ima 写入要走 create_media → COS → add_knowledge）

## 7. 常见错误

| 错误 | 正确做法 |
|---|---|
| 用 `search_knowledge` 的片段当正文 | 只用于选候选，正文必须 `fetch_media_content` |
| 直接 filter out FOLDER | 逐层递归，否则漏子文件夹文档 |
| 并发调 `fetch_media_content` | 串行调用，失败重试 1 次 |
| 静默跳过解析失败的文档 | 列出并让用户决定等待还是 partial |
| 把疑似截断的内容标为完整 | 只写 `no_truncation_detected` / `suspected_truncated` |
| 跳过 `validate` 直接 finalize | 必须先 validate 且 0 error |
| 本技能里直接写 Obsidian vault | 交接 `obsidian-writer`，本技能不碰 vault |

