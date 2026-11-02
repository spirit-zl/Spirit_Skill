<p align="center"><img src="assets/banner.png" alt="better-skill banner"></p>

# better-skill

<p align="center">
  <a href="CHANGELOG.md"><img src="https://img.shields.io/badge/version-v1.1.0-1D4ED8?style=flat" alt="version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-3DA639?style=flat" alt="license"></a>
  <a href="references/provenance.md"><img src="https://img.shields.io/badge/audited-332%20official%20skills-B45309?style=flat" alt="audited"></a>
  <a href="references/checklist.md"><img src="https://img.shields.io/badge/execution%20red%20lines-21-0F766E?style=flat" alt="red lines"></a>
  <a href="references/provenance.md"><img src="https://img.shields.io/badge/head%20to%20head-96%20vs%2085-B91C1C?style=flat" alt="head to head"></a>
  <img src="https://img.shields.io/badge/built%20by-%E4%B8%80%E5%A0%82%20YITANG-16365F?style=flat" alt="built by YITANG">
</p>

> **AI Agent 技能的医生手册。**
> 一套有证据支撑的标准：选型、体检、打分、建造——萃取自对官方 300+ 技能的全量审计。

[🇺🇸 English](README.en.md) | **🇨🇳 中文**

---

你的 Agent 技能越多，越需要一个医生。

大多数技能仓库教你怎么**写**技能；这一份管的是标准：动工前该选哪种架构、一个健康技能内部长什么样、以及怎么用数字证明一个技能比另一个好。

![better-skill 架构总览](assets/architecture.png)

## 为什么需要它

**Skill 可能做了几十个，但从来没有认真体检过一个。**

三个你大概熟悉的场景：

1. **技能写完就用，从没体检过。** 描述膨胀、方法内嵌正文、示例缺失——都是慢性病，发作的时候你只觉得"这技能不好用"，但说不出哪里坏。
2. **触发是玄学。** 该触发的时候不触发，不该触发的时候乱触发。几乎没人真的测过触发率——本标准把"描述触发"变成可以量化、可以迭代优化的工程问题。
3. **"我的技能比你的好"——没有统一标尺的对比就是吵架。** 五维标尺（ZSAR / 触发率 / 往返次数 / Token 占用量 / 自治等级）让对比有裁判。

## 30 秒上手

```bash
# 1. 克隆到你的技能目录（任何按 SKILL.md 加载技能的环境都可以）
git clone https://github.com/YTruman/better-skill.git <你的技能目录>/better-skill
```

```text
2. 对你的 Agent 说：
   "用 better-skill 体检一下我的 xx 技能"
3. 你会拿到：一份带证据行号的体检报告
```

（后续）一行命令安装与 YAI CLI 分发通道准备中——装一次，持续享受更新。

## 四种门诊，一条管线

| 模式 | 你说…… | 发生什么 | 你拿到什么 |
|---|---|---|---|
| **创建** | "我想做个新技能" | 范式选型 → 自由度定级 → 目录蓝图 → 施工 → 评测闭环 → 触发优化 | 一个带评测数据的新技能 |
| **体检** | "审计这个技能" | 四层 P0/P1/P2 扫描 + Not-To-Do 红线清单 | 体检报告：每个发现带证据行号与优先级 |
| **处方** | "升级这个技能" | 先出体检报告 → 只开 `str_replace` 级靶向补丁 | 最小 diff 补丁 + 复诊前后对比 |
| **会诊** | "哪个技能更好？" | 五维标尺统一度量 | 雷达对比表 + 保留/合并/退役建议 |

核心纪律：**先诊断，后开方。没有体检报告，禁止动刀。** 一条处方若指不回体检报告的具体条目，就是医疗事故。

## 一份真实体检长什么样

见 [`examples/perfect-audit-report.md`](examples/perfect-audit-report.md)：一次真实的四技能体检，抓出 6 项发现（全清单见 provenance『实战验证』节；示例文件为报告样式节选）——从 P0 的"缺初始对话协议"（方向跑偏风险），到一条刻意的"**不改动**"裁决（描述性中文是对话型技能的设计意图，不是缺陷）。

![四技能体检结果](assets/checkup-findings.png)

作者自己的用法：用它回头评审过去在 Codex、Claude Code 里封装的各个技能——看得一身冷汗。一个被 Agent 自查过好几轮、觉得已经无限完美的技能，在体检面前照样暴露出缺初始对话协议、缺触发关键词、缺任务路由。「怪不得我的工作流经常不稳定。」

知道什么时候不动刀，才是医生。

## 凭什么信这套标准

- **官方 300+ 技能全量审计**：332 个官方技能的调用模式逐一盘点，优先级从真实用法里排出来，不凭口味；
- **研究方法可复述**：12 小时集中研究五步——逐字学官方 skill-creator → 拆解官方推荐十几套最佳实践 → 合并工程范式定版 → 三源盲测交叉评估 → 封装后真实案例盲测，评分与稳定性显著领先；
- **旗舰案例深拆**：skill-creator、doc-coauthoring、canvas-design、mcp-builder、webapp-testing 等；
- **五轮对抗式打磨**：标准是被对撞砸出来的，不是一次写成的；
- **对撞有分数**：与官方 skill-creator 五维结构化对比，综合 **96 vs 85**。

> **如何读这组数字（诚实框）**：五维（物理原子性 / 逻辑架构力 / 工程生命周期 / 量化可测量性 / 审美与意图锚点）为结构化星级评分；96/85 为综合折算分，用于**同口径相对比较**，不是实验室级精确测量。完整评分表、对撞过程与已吸纳的官方细节见 [`references/provenance.md`](references/provenance.md)——欢迎按同口径复算。

![五维对撞评分表](assets/scoring-chart.png)

## 和官方 skill-creator 是什么关系

**互补，不是替代。** 官方管"能造出来"（准入证书），本标准管"造得好不好、活得久不久"（大师手册）。评测阶段直接采用官方 2026-08 "Test, measure, and refine" 版的评测闭环（with/without 对照、断言评分、触发优化），已反向吸纳其四件工程细节。

## 目录结构

```
SKILL.md                    # 路由 + 医生宪法（从这里开始）
references/principles.md    # Token 经济、自由度匹配、认知接地
references/paradigms.md     # 七大架构范式 + 选型决策树
references/checklist.md     # 四层 P0/P1/P2 检查表 + Not-To-Do 红线 + 报告模板
references/metrics.md       # ZSAR / 触发率 / 往返 / Token 占用量 / 自治 标尺
references/templates.md     # 全套文档模板 + Anti-Slop 反塑料词库
references/eval-loop.md     # with/without 评测、断言评分、触发优化
references/provenance.md    # 证据链：审计、评分、五轮进化史
examples/                   # 一份满分体检报告（含批注）
```

## FAQ

**支持哪些环境？** 任何按 SKILL.md 方式加载技能的 Agent 环境（Claude Code 及同类 Harness）。

**中文技能适用吗？** 适用——本标准本身就用中文写成并先在中文技能上实战，触发优化方法对中英描述都有效。

**我就一个技能，也值得体检？** 值得。单技能体检约一刻钟出报告，缺初始协议（P0）、示例缺失（P1）等问题通常当场可修。

**真能稳定到 90 分？** 这是设计目标，不是承诺：不依赖基座模型、不依赖 Harness，把封装质量托到 90 分水位。是不是好用——欢迎自己安排实验复测、交叉对比（评测方法见 `references/eval-loop.md`）。

**能帮我从零造一个技能吗？** 能。"创建"模式就是全管线：选型 → 蓝图 → 施工 → 评测 → 触发优化，产出的技能自带评测数据。

## Better 家族与路线图

这个仓库是一张组织架构图上的一个岗位：truman-skills——一个 AI 商业管理团队的岗位说明书（与本仓同日公开）。

入选标准三连：足够有效、足够稀缺、足够最佳实践——Star 的数量，就是开源的速度。

## 作者

**YTruman** — CEO @ 一堂（Yitang），一家伪造成教育公司的研究机构。Y = Y型创业者，我做了9年的商业科学规律研究。

## 许可

[MIT](LICENSE)
