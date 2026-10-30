# 技能总览（INDEX）

> 本文件由 `_hub/sync.py` 自动生成，请勿手工编辑。
> 生成时间：2026-10-30 19:53:01　技能数：11

任何平台只要读取本文件与下列绝对路径，就能使用这些技能，无需平台原生支持。

| 技能 | 一句话说明 | 路径 |
|---|---|---|
| `content-collect` | 根据提供的主页/关键词/作品链接采集作品信息到指定飞书表格。支持抖音（自建 dtk 实例）与小红书（受控 Chrome 复用本机登录态）两个来源，登录态自动抓取、13 字段固定契约… | `/Users/spirit/Spirit_Skill/content-collect/SKILL.md` |
| `doc-merge-to-feishu` | 把多个本地文档（Word/PDF/Markdown/TXT/EPUB）按文件名顺序转换并写入飞书云文档，扫描版 PDF 会自动 OCR 出文字；既支持合并成一份，也支持按文件名顺序… | `/Users/spirit/Spirit_Skill/doc-merge-to-feishu/SKILL.md` |
| `image-to-feishu-table` | 把截图里的文字原样转成可用文本：表格版面输出可粘进飞书文档并自动转成原生普通表格的 GFM 表，纯文字版面输出不带标题样式的 Markdown；当用户发来截图要转文字、识别图中文字… | `/Users/spirit/Spirit_Skill/image-to-feishu-table/SKILL.md` |
| `knowledge-combine` | 把「一批资料」汇总整理成一篇或多篇成文文档。用于用户要求从 ima 知识库（指定目录/文件/关键词）、飞书云文档或飞书知识库 wiki 节点（含其下整棵子文档树）、或本地文件夹里的… | `/Users/spirit/Spirit_Skill/knowledge-combine/SKILL.md` |
| `novel-collect` | 给一个小说任意章节的网页链接，自动抓取全本并合并成单个纯文本 Markdown 文件（文件名=小说名，章节名作二级标题），自动清除"本章未完请翻页""阅读网址"等站点广告，合并后自… | `/Users/spirit/Spirit_Skill/novel-collect/SKILL.md` |
| `obsidian-writer` | 把已经成文的汇总稿按 Obsidian 规范写入本机 vault。当用户要求「写入 Obsidian / 导入 Obsidian / 存进知识库 / 放进 000inbox / 把… | `/Users/spirit/Spirit_Skill/obsidian-writer/SKILL.md` |
| `reading-notes` | 为书籍生成结构化读书笔记并归档到飞书云文档文件夹。按内置四类提示词模板（科学知识类9节、想象文学类5节、历史题材9节、人物传记9节）逐节生成 Markdown 笔记（主标题为书名本… | `/Users/spirit/Spirit_Skill/reading-notes/SKILL.md` |
| `skill-architect` | 把用户创建skill的需求转成高质量的SKILL.md文件，或者把用户已有的SKILL.md进行审查评分并给出优化建议；当用户说创建/帮我写/生成 Skill 或优化/检查/看看 … | `/Users/spirit/Spirit_Skill/skill-architect/SKILL.md` |
| `skill-portability` | 把一个已经能跑但绑定了本机环境的技能（或脚本项目）改造成跨平台、可发布到 git 的仓库。当用户要求「做成跨平台的 / 跨系统的 / 换台机器也能用 / 发布到 git / 开源出… | `/Users/spirit/Spirit_Skill/skill-portability/SKILL.md` |
| `wechat-article-collect` | 微信公众号文章采集：以飞书多维表格为驱动，按第一列「文章链接」抓取正文，提取发表日期、文章标题、文章正文（去掉广告、自我介绍等无关内容）和不超过 200 汉字的文章精华，缺列自动补… | `/Users/spirit/Spirit_Skill/wechat-article-collect/SKILL.md` |
| `yitang-to-feishu` | 把一堂课件（yitang.top 的 fs-doc 课程文档）或普通网页抓成 Markdown 并发布到指定飞书云文档目录，图片全部下载后按原位置内联、可附整页长截图；同时适用于公… | `/Users/spirit/Spirit_Skill/yitang-to-feishu/SKILL.md` |

## 怎么用（平台无关）

1. 读本文件，按上表挑技能。
2. 打开对应的 `SKILL.md`，按其正文执行。
3. 技能若带 `scripts/`、`references/`，路径都相对于该技能目录。

## 各平台接入状态

- **WorkBuddy**：`~/.workbuddy/skills`（已接入 11 个）
- **Agents 标准 (~/.agents)**：`~/.agents/skills`（已接入 11 个）
- **Claude Code**：未接入（`~/.claude/skills` 不存在）
- **CodeBuddy CLI**：未接入（`~/.codebuddy/skills` 不存在）
- **DeepSeek Harness**：dsh 用 cordis 插件机制（~/.dsh/profiles/*/cordis.yml），没有 SKILL.md 扫描目录。接入方式是：让 dsh 里的 agent 直接读本仓库 INDEX.md 与各技能绝对路径，或另行封装 cordis 插件。
