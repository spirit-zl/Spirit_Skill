# 分层综合规范

目标：在有限上下文里产出「可溯源、不编造、冲突可见」的成文。

## 三层结构

```
Layer 1  分块摘要     每篇 → 每块 → ≤ 600 字，保留关键数字/结论/引语
Layer 2  分组归并     同主题/同目录的若干篇 → 一篇分组综述，显式列冲突
Layer 3  全局成文     所有分组综述 → 大纲 → 成文（模型直接出，不回问用户）
```

## 分块

`scripts/synthesize.py chunk` 按 **标题 / 段落 / 代码块** 边界切，默认 12000 字符 + 300 重叠；
单篇 ≤ 阈值时不切。切块结果写 `manifest.chunks[]`，每块带 `source_id` 与 `anchor`（页号或标题）。

## Layer 1 输出格式

```
## S12 《标题》（来源：ima/产品/2026-08，完整性：完整）
- 核心结论：……
- 关键数据：……（原文：「……」）
- 适用条件 / 边界：……
- 存疑点：……
```

规则：
- **只写原文有的东西**；模型推断必须以「推断：」开头，且单独成条
- 关键数字、结论、定义尽量保留原文措辞
- 每篇 ≤ 800 字，超过说明抓错了重点

## Layer 2 分组归并

```
python3 scripts/synthesize.py merge --manifest <M> --level group
```

- 同一事实多个来源不一致 → **并列列出**，标注 `[S3]（2026-03）vs [S7]（2026-08）`，不静默择一
- 时间更近或来源更权威的放前面，但不删除旧说法
- 输出「组内共识 / 组内分歧 / 组内空白」三段

## Layer 3 全局成文

1. 模型直接出大纲（参考 `assets/outline-templates.md` 选体裁），**不回问用户**
2. 按 `assets/final-report-template.md` 成文
3. 每条结论挂 `[S<n>]`；文末「资料清单」列 `S<n> → 标题 / 来源 / 路径`
4. 任何分组标注为「组内空白」的主题，正文中对应章节写「**未覆盖**：现有资料未涉及」
5. 所有 `suspected_truncated` 来源，其贡献的结论加脚注「来源可能不完整」

## 引用锚点

`[S12]` 对应 IR 的 `source_id`。成文里第一次出现时给全标题，后续可用短名。
禁止出现没有对应 source_id 的事实性断言（编辑性过渡句除外）。

## 缓存

```
python3 scripts/normalize.py cache-get --source-key <K> --content-sha256 <H> [--profile-version 1.0]
python3 scripts/normalize.py cache-put --source-key <K> --content-sha256 <H> --summary-file <F> [--profile-version 1.0]
```

缓存根 `~/.workbuddy/data/knowledge-combine/_cache`，文件名形如
`<source_key 前2位>/<content_sha256 前16位>-<profile 版本>.json`。

- 同指纹 + **同 profile 版本**命中即复用，避免重跑同一批资料
- `cache-get` 命中时直接输出摘要正文，未命中输出 `{"hit": false}`
- **总结口径/模板改了必须升 `profile_version`**（同时改 `policy.profile_version`，`synthesize.py tasks`
  会按它查缓存），否则会命中旧口径的摘要

## 大规模作业：子 agent 扇出

当 `plan` 报出 `est_tokens > 400k` 或几百篇文档时，逐篇串行摘要会撑爆上下文。
此时用**子 agent 扇出**分片，主 agent 只做装配：

```
1. 分片     按来源编号切成 15~20 片，每片派一个子 agent 做 Layer 1/2 摘要
2. 中间归并 同类素材 4 片一组，再派一轮子 agent 合并去重 → mid/
3. 终稿     最后一轮生成分片，再按文件名序拼接成终稿
```

每个子 agent 的 prompt 要点：

- 只用文档真实信息，**禁止新增**；信息要全（步骤/命令/参数/可复制原文/表格/避坑点）
- 去重：同一条信息只写一次，差异合并
- 输出纯 Markdown，用 `##` 分节，来源标 `（来源：文档标题）`；空壳文档跳过

### 扇出的两个硬坑

- **会触发 429 频率限制**（次日 09:54 UTC+8 左右重置），主 agent 自身对话不受影响。
  所以：分片数量宁少勿滥，别每篇派一个。
- **报错不等于白干**：终稿子 agent 可能在报错前已经落盘分片。收到报错后**先检查
  `<job_dir>/final/` 里分片是否齐全**，齐全就直接按文件名序拼接，不要傻等重置。

