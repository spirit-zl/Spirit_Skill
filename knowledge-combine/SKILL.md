---
name: knowledge-combine
description: 把「一批资料」汇总整理成一篇或多篇成文文档。用于用户要求从 ima 知识库（指定目录/文件/关键词）、飞书云文档或飞书知识库 wiki 节点（含其下整棵子文档树）、或本地文件夹里的一批文件中，做「汇总 / 整理 / 综述 / 归纳 / 合并成一篇 / 做成一份完整文档 / 按主题分类成多篇 / 产出白皮书或周报」，并指定把结果写入 ima 知识库、飞书云文档或本地路径时使用。典型触发：「把这些资料整理成一份完整文档」「汇总这个文件夹/这个飞书目录/这个 ima 目录的内容」「把飞书 wiki 这个节点下所有文档汇总成几篇」「按主题把资料分类成多篇文档」「输出一份总结到飞书/ima/本地」。单次运行只处理一种输入源，输出到一个目标位置。本技能不写 Obsidian：目标为 Obsidian 时，先用本技能产出成文，再交接 obsidian-writer 写入 vault。
agent_created: true
---

# knowledge-combine — 资料汇总成文

从单一来源批量采集资料，归一化后分层综合，产出一或多篇成文文档，写入指定位置。

## 已确认的默认策略（无需每次询问）

| 决策      | 取值                                                    |
| ------- | ----------------------------------------------------- |
| 混合输入    | ❌ 不支持，一次运行只处理一种来源                                     |
| partial | ✅ 允许，失败项跳过并在报告中列出，文档相应章节标「未覆盖」                        |
| 大纲      | 模型直接产出，不向用户确认                                         |
| 配图      | 尝试还原（能取到就带入，取不到降级为图注文字）                               |
| 输出授权    | 同一 scope（同一知识库 / 同一飞书目录 / 同一本地目录）首次确认后复用；跨 scope 重新确认 |
| 覆盖范围    | 三种输入源 + 三种输出目标全部支持                                    |
| Obsidian | ❌ 本技能不写 vault：产出成文后交接 `obsidian-writer`                  |

## 核心边界

- ✅ 读 ima / 飞书（云文档、云盘文件夹、wiki 节点树）/ 本地，写成文，写回本地、飞书、ima 三个目标
- ✅ 大规模作业用子 agent 扇出分片（见 `references/synthesis-protocol.md`）
- ❌ **不做 Obsidian 写入**（交给独立技能 `obsidian-writer`）；也不替它生成 note plan
- ❌ 不在 `validate` 通过前 finalize
- ❌ 不把模型推断写成原文事实；不静默择一处理来源冲突
- ❌ 覆写飞书/ima 已有文档前必须拿到用户确认（覆写属 high-risk-write）

## 路径常量

```
SKILL   ~/.workbuddy/skills/knowledge-combine
脚本     scripts/job.py  normalize.py  collect_local.py  collect_lark.py
        collect_ima.py  synthesize.py  images.py  emit.py
契约     references/ir-contract.md  references/manifest-contract.md
源细节   references/source-{ima,lark,local}.md
综合规范 references/synthesis-protocol.md
输出细节 references/output-targets.md
模板     assets/outline-templates.md  assets/final-report-template.md
暂存根   ~/.workbuddy/data/knowledge-combine
Python   /Users/spirit/.workbuddy/binaries/python/envs/default/bin/python
```

下文所有 `python3` 一律替换成上面这个解释器路径（已装 pymupdf / python-docx /  
python-pptx / openpyxl / beautifulsoup4 / requests / pyobjc-Vision）。

## 参数（从用户请求中抽取，缺失项按下表默认）

```
source.type      ima | lark | local                   必填
source.locator   目录/文件/关键词/链接/路径             必填
source.filters   recursive, max_docs, updated_after, include/exclude glob
intent           主题 + 读者 + 体裁 + 目标字数          缺失时用默认：决策者视角综述，3000-6000 字
output.target    local | lark | ima                   必填
output.location  路径 / 飞书文件夹 token / ima 库+目录   必填
output.format    md | docx | pdf（ima 受限，见输出细节）
output.mode      create | overwrite
```

用户想写进 Obsidian 时：`output.target` 仍填 `local`（本技能只负责把成文落盘），
拿到成文与 `manifest.json` 后再交接 `obsidian-writer`。

## 工作流

### 1. 建 job

```
python3 scripts/job.py init --source-type local \
  --selection-json '{"mode":"folder","path":"<abs>"}' \
  --output-json '{"target":"local","location":"<abs>","format":"md","mode":"create"}' \
  --policy-json '{"max_chars_per_chunk":12000,"chunk_overlap":300,"max_docs":200,"confirm_threshold":50,"allow_partial":true,"image_policy":"recover"}'
```

记录返回的 `manifest_path`。后续所有命令都带 `--manifest <path>`。

### 2. 输出授权（唯一必须确认的门禁）

- 读 manifest 的 `authorized_scopes`；本次输出 scope 已在列 → 直接放行
- 不在列 → 向用户确认「写入位置 + 新建/覆写 + 格式」，通过后

```
python3 scripts/job.py authorize --manifest <M> --scope "<scope 字符串>" --mode create
```

scope 例：`local:/Users/spirit/Docs/out/`、`lark:folder:fldcnXXX`、`ima:kb:<id>/folder:<fid>`。

### 3. 采集（按 source.type 三选一）

```
source-ima.md   → MCP 调用由 agent 执行，再用 scripts/collect_ima.py record 登记
source-lark.md  → python3 scripts/collect_lark.py collect ...   （内部 shell 调 lark-cli）
source-local.md → python3 scripts/collect_local.py collect ...
```

三个采集器产出统一的 IR JSONL（契约见 `references/ir-contract.md`）。  
采集阶段不写正文进上下文，只落盘。

飞书侧支持三种入口：单个云文档链接、云盘文件夹（递归）、**wiki 节点（递归整棵子文档树）**。
递归展开、并发限流、异步导出、bitable/PDF 的特殊处理见 `references/source-lark.md`。

### 4. 归一化与去重

```
python3 scripts/normalize.py run --manifest <M> --dedupe
```

- 指纹 = 正文 sha256（去空白后）；标题+指纹同时命中判为重复，保留优先级 local > lark > ima
- 标记 `completeness`：`no_truncation_detected` / `suspected_truncated` / `parse_failed`
- 输出 `dedupe_report`；疑似截断与解析失败**不自动丢弃**，列入报告

### 5. 规模评估

```
python3 scripts/synthesize.py plan --manifest <M>
```

返回：文档数、总字符、预估 token、分层方案。  
仅当 `docs > confirm_threshold` 或 `est_tokens > 400k` 时**问一次**「继续 / 收窄范围」，否则直接推进。  
用户可随时说「不要问我」，把 `confirm_threshold` 设为极大值即可。

规模护栏（按量级决定打法）：

| 阈值 | 动作 |
| --- | --- |
| 选中 > 20 篇 | 先报数量与体量，确认后再继续 |
| 正文总量 > 500,000 字符 | 提示拆分或确认 |
| 单篇 > 12,000 字符 | 启用分块 map-reduce（`synthesize.py chunk`） |
| 文档 > 50 篇 | 派子 agent 并行总结（隔离上下文），主流程只做合并与落盘 |

### 6. 分层综合

严格按 `references/synthesis-protocol.md`：

```
python3 scripts/synthesize.py chunk  --manifest <M>          # 分块清单
python3 scripts/synthesize.py tasks  --manifest <M>          # 逐块生成摘要任务包
python3 scripts/synthesize.py merge  --manifest <M> --level group|global
```

- 每篇先出 source summary（≤ 800 字，保留原文关键数据与引语）
- 分组归并（按主题/来源目录分组），显式列出冲突
- 全局阶段由模型直接出大纲 → 成文，不回问用户
- 每条结论挂 `[S<n>]` 锚点；推断句必须以「推断：」开头
- 缓存：`scripts/normalize.py cache-get/cache-put`，同指纹复用摘要
  （`cache-get` 命中时直接打印摘要正文，未命中打印 `{"hit": false}`）

### 7. 配图还原（best effort）

```
python3 scripts/images.py recover --manifest <M>
```

按 `references/output-targets.md` 的「配图」章节执行；失败降级为「图注 + 文字描述」，并在报告列出。

### 8. 输出

```
python3 scripts/emit.py local --manifest <M> --file <成文 md>
python3 scripts/emit.py lark  --manifest <M> --file <成文 md> --parent-token <token> [--mode overwrite --token <doc>]
python3 scripts/emit.py ima   --manifest <M> --file <成文 md> --kb-id <id> [--folder-id <fid>]
```

ima 输出中 `create_media` / `add_knowledge` 是 MCP 工具，必须由 agent 调用；脚本负责前置校验与 COS 上传。

### 9. 收尾

```
python3 scripts/job.py validate --manifest <M>     # 必须 0 error
python3 scripts/job.py finalize --manifest <M>     # 自动判 ready / partial
python3 scripts/job.py report   --manifest <M>     # 覆盖矩阵 + 跳过项 + 疑似截断
```

状态：`ready`（全纳入）/ `partial`（有跳过，允许）/ `failed`（输出未落盘）。

### 10. 交接 Obsidian（仅当用户要写进 vault）

本技能**到此为止**，不碰 vault。把下面三项交给 `obsidian-writer`：

```
HANDOFF
skill: obsidian-writer
manifest_path: <job_dir>/manifest.json       # 必须是绝对路径
job_id: <job_id>
status: ready | partial
```

交接前必须做的事：

- 成文已 `emit.py local` 落盘（`manifest.outputs[]` 里有记录），否则下游找不到正文
- `status=partial` 时，先向用户说明缺了哪些来源（`job.py report`），**取得明确批准**后才能移交；
  下游会用 `policy.allow_partial` + `partial_approved_at` 卡住未经批准的不完整内容
- 正文里的来源引用保持 `[S<n>]` 锚点形式，下游据此还原「这篇笔记引用了哪些来源」

## 常见错误

| 错误                          | 正确做法                                   |
| --------------------------- | -------------------------------------- |
| 用 ima 搜索片段当正文               | 片段只用于选候选，正文必须 `fetch_media_content` 回源 |
| ima 目录用 `filter_out FOLDER` | 逐层 `folder_id` 递归，否则漏子文件夹              |
| 飞书 wiki 递归用 `--page-limit` 截断 | 用 `--page-all --page-limit 0 --page-size 50` 走完，别让子节点被截掉 |
| 拉飞书正文并发开很高                | `max_workers=2`，高了触发限流；失败项多轮重跑（已存在的跳过）  |
| 不等导出任务完成就解析               | `drive +export` 是异步的，未完成要补 `drive +task_result` |
| 把疑似截断标为完整                   | 只写 `suspected_truncated` 并在报告列出        |
| 来源冲突时挑一个写                   | 并列呈现 + 标注时间与来源                         |
| 跳过 `validate` 直接 finalize   | 必须先 validate 且 0 error                 |
| 未授权就写 ima/飞书                | 先 `authorize`，scope 变了重新确认             |
| 一次请求里混两种来源                  | 拆成两次 job，各自单独运行                        |
| 在本技能里顺手写 Obsidian             | 交接 `obsidian-writer`，本技能不碰 vault         |
| 飞书写完不回读校验                   | `docs +fetch` 回读字符数，与源文件比对确认没丢内容        |
