# 输出目标实现细节

三种目标共用一份 Markdown 成文（`<job_dir>/final/*.md`）。**Markdown 是唯一真源**，
其他格式都从它转换，避免多份内容不一致。

## 通用：授权 scope（复用授权）

- 首次写入某个 scope 时向用户确认一次，通过后 `scripts/job.py authorize` 记进 `authorized_scopes`
- 之后再写同一 scope（含同目录下多篇）**不再询问**
- 换 scope 或 `mode=overwrite` 首次出现 → 重新确认
- `overwrite` 是高风险动作，必须明确列出将被覆盖的文件/文档再执行

## A. 本地

```
python3 scripts/emit.py local --manifest <M> --file <md> [--name "标题"] [--as-docx]
```

- 目标目录不存在则创建；同名文件默认**不覆盖**，改为 `标题-20260928.md`（避免静默毁档）
- 需要 `.docx` 时：把 md 交给 `tencent-docx` 的 doc-formatter / doc-converter 链路，
  不要自己用 python-docx 拼排版
- 图片：md 用相对路径引用 `<job_dir>/img/`，docx 转换时由下游处理
- 落盘后回写 `manifest.outputs[]`（路径 + 字节数 + sha256）

## B. 飞书

用户给的是文件夹**名字**而不是 token 时，先搜出来（`--only-title` 避免搜到正文里的同名词）：

```
lark-cli drive +search --query "<文件夹名>" --doc-types folder --only-title --as user --format json
```

新建到云盘文件夹：

```
lark-cli docs +create --doc-format markdown --content "@./final.md" \
  --parent-token <folder_token> --title "<标题>" --as user
```

新建到 wiki：

```
lark-cli wiki +node-create --space-id <space|my_library> --parent-node-token <node> \
  --obj-type docx --title "<标题>" --as user        # 再往该节点写内容
```

覆写：

```
lark-cli markdown +overwrite --token <token> --content "@./final.md" --as user   # Drive 原生 md
lark-cli docs +update ... --as user                                              # docx 块级更新
```

关键点：
- `--content` 多行一律用 `@file`（绝对路径），不要拼字符串
- **写完必须回读校验**，确认没丢内容：

```
lark-cli docs +fetch --doc <doc_id> --as user --doc-format markdown --format json \
  | python3 -c "import json,sys;print(len(json.load(sys.stdin)['data']['document']['content']))"
```

  回读字符数略少于源文件是正常的（空白归一化）；差得多说明写入被截断，要排查。
- 覆写前必须用户确认（high-risk-write，CLI 会要求 `--yes`）
- 图片：`docs +media-insert` **只能插到文末**；要放到指定章节，先
  `docs +fetch --detail with-ids` 取章节末尾 block id，再
  `docs +update --command block_move_after --block-id <锚点> --src-block-ids <图片block>`
- 超长文档（> 3 万字）建议分批 append，单次写入过大会超时
- 用户 token 约 24h 过期，写之前 `lark-cli auth status`

## C. ima

**三步走，缺一不可：**

1. 本地生成文件（md / docx / pdf，扩展名必须在下表内）
2. `mcp__ima-mcp__create_media(content_type, file_name, file_size, file_ext, knowledge_base_id)`
   → 拿到 COS 上传凭证与 `media_id`
3. 上传 → `mcp__ima-mcp__add_knowledge(knowledge_base_id, media_id, folder_id, duplicate_name_strategy)`

脚本负责第 1、3 步之间的部分：

```
python3 scripts/emit.py ima --manifest <M> --file <md> --kb-id <id> \
  [--folder-id <fid>] [--format md] [--creds creds.json]
```

`--creds` 传入 `create_media` 返回的凭证 JSON，脚本做实际 PUT 上传；
`create_media` 与 `add_knowledge` 本身由 agent 调 MCP。

### create_media 硬约束

- `content_type` 必须按扩展名**精确**填写，禁止 `application/octet-stream` 兜底：
  pdf→`application/pdf`；doc→`application/msword`；
  docx→`application/vnd.openxmlformats-officedocument.wordprocessingml.document`；
  ppt→`application/vnd.ms-powerpoint`；
  pptx→`application/vnd.openxmlformats-officedocument.presentationml.presentation`；
  xls→`application/vnd.ms-excel`；
  xlsx→`application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`；
  csv→`text/csv`；md/markdown→`text/markdown`；txt→`text/plain`；xmind→`application/x-xmind`；
  png→`image/png`；jpg/jpeg→`image/jpeg`；webp→`image/webp`；mp3→`audio/mpeg`；m4a→`audio/x-m4a`；
  wav→`audio/wav`；html→`text/html`；epub→`application/epub+zip`；aac→`audio/aac`
- `file_size` 必须与真实字节数一致，服务端据此校验，超限直接拒绝
- 扩展名不在上表 → **拒绝上传**，不要猜
- `duplicate_name_strategy`：默认 `DUPLICATE_NAME_STRATEGY_SAVE`（保留）；
  用户明确要覆盖同名才用 `REPLACE`

### 图片

ima 解析入库只取文本，**图片基本会丢**。要保留图 → 输出选 `pdf` 或 `docx`；
输出 `md` 时把图片降级为「图注 + 文字描述」，并在报告注明。

## D. Obsidian —— 不在本技能内，做交接

本技能**不写 Obsidian vault**，也不生成 note plan。用户要求写进 Obsidian 时：

1. 照常走完流程，`emit.py local` 把成文落到本地（`output.target: local`）
2. `job.py validate` 通过、`finalize` 得到 `ready`（或用户已批准的 `partial`）
3. 交接给独立技能 `obsidian-writer`：

```
HANDOFF
skill: obsidian-writer
manifest_path: <job_dir>/manifest.json
job_id: <job_id>
status: ready | partial
```

交接前检查：

- `manifest.outputs[]` 里有已落盘的成文，否则下游找不到正文
- 成文正文里的来源引用保持 `[S<n>]` 锚点形式，下游据此还原「引用了哪些来源」
- `status=partial` 且用户尚未明确批准 → **先问，别移交**

## 失败处理

任一目标写入失败：记 `issues[severity=error]` + `upload_failed`，该 output 不进 `outputs[]`。
若所有 output 都失败 → job 状态 `failed`（`allow_partial=true` 也不豁免）；
部分成功 → `partial`，报告里明确「N 篇已写入，M 篇失败 + 原因」。
