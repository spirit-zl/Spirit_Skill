---
name: content-collect
description: 根据提供的主页/关键词/作品链接采集作品信息到指定飞书表格。支持抖音（自建 dtk 实例）与小红书（受控 Chrome 复用本机登录态）两个来源，登录态自动抓取、13 字段固定契约、按作品链接只更新不新增；也可只采集不落表。
agent_created: true
---

# content-collect

给一个**主页链接 / 作品链接 / 关键词**，采成固定的 13 字段，写进你**指定的飞书表格**。

```bash
C=~/.workbuddy/skills/content-collect/scripts/collect.py
python3 "$C" "https://www.douyin.com/user/MS4wLjABAAAA..."       --to <表格链接>
python3 "$C" "https://www.xiaohongshu.com/user/profile/<id>"     --to <表格链接>
python3 "$C" "操盘手" --to <表格链接>                              # 关键词（仅抖音）
python3 "$C" "<链接>" --out /tmp/rows.json                        # 只采不写
```

平台**自动判断**（看域名和 ID 形状），不用先说；判断错了用 `--platform` 强制指定。
落表时「来源」列自动填「抖音」/「小红书」，不用手写。

## 每次开始前必须问的两件事

**不要一上来就采。** 缺哪样就问哪样，别替用户决定：

1. **采什么** —— 平台的主页链接、作品链接，或关键词（关键词只有抖音支持）。
   用户只说「帮我采一下」时，问清楚要采哪个账号 / 哪个词。
2. **写到哪张表** —— 先看有没有「上次用过的表」：

| 情况 | 怎么做 |
|---|---|
| 有上次的表 | 把**名称 + 链接**原样报给用户，问「还继续用这张吗？」<br>→ 是：加 `--reuse-last`；否：要新链接后加 `--to <新链接>` |
| 没有上次的表 | 直接要一条飞书多维表格链接（`/base/<token>`）或文件夹链接 |

命令层面对应三种退出码，模型据此决定下一步：

- **`3`** = 有上次的表、但没确认 → 打印「名称 / 链接 / 上次写入时间」，等用户回答
- **`4`** = 一张表都没有 → 等用户给链接
- **`0`** = 成功（写成功后自动记住这张表；`--dry-run` 不会记）

## 换机器：先装环境，再干活

顺序别反 —— 先看缺什么，问过用户再动手，装完复检：

```bash
S=~/.workbuddy/skills/content-collect/scripts
python3 "$S/doctor.py" plan            # ① 列清单：哪些就绪、哪些缺、缺的用什么命令装
# ② 把清单原样告诉用户，问「要我装吗？」—— 不要不问就装
python3 "$S/doctor.py" install --yes   # ③ 用户同意后执行（只用小红书：加 --skip-douyin）
python3 "$S/doctor.py" check           # ④ 复检
```

④ 通过（或只剩「受控浏览器未启动」这类提示）之后，才告诉用户「可以用了」。

**这两步只能人工**，脚本只能检查不能代劳，要明确写出来让用户做：
- Chrome 里**登录**对应平台（cookie 唯一来源）
- `lark-cli auth login`（要落表时）
- 首次读钥匙串会弹系统授权 → 点「始终允许」；沙箱里会卡住，要在普通终端跑

安装器只走本机已有的通道（有 brew 用 brew、有 npm 用 npm），都没有就给出官网地址；
**不会静默 `sudo`，也不会替你装 Docker。**

## 以后加一个平台，改这四处

1. `scripts/<平台>.mjs` 或 `scripts/<平台>/collect.py`：产出同一份 13 字段
2. `scripts/collect.py`：`detect()` 加域名/ID 规则、`SOURCE_LABEL` 加来源名、`collect()` 加分支
3. `scripts/doctor.py`：`tools()` 加该平台的依赖，`check_<平台>()` 加一组体检
4. 本文件：参数表加平台专属开关，字段来源表加一列

```
content-collect/
├── SKILL.md
├── scripts/
│   ├── collect.py          # 统一入口：认平台 → 调引擎 → 落表
│   ├── doctor.py           # 两个平台一起体检
│   ├── feishu_bitable.py   # 落表（13 字段契约、只更新不新增）
│   ├── xhs.mjs             # 小红书引擎（受控 Chrome + CDP）
│   ├── xhs_cookie.py       # 小红书 cookie 自动抓取（Chrome 钥匙串解密）
│   └── douyin/             # 抖音引擎（REST 直连自建 dtk）
│       ├── collect.py      #   三种输入 → 13 字段
│       ├── dtk.py          #   API 客户端
│       ├── cookie.py       #   cookie 自动抓取 + 身份池
│       ├── stack.sh        #   起停 dtk（支持 --no-docker）
│       └── doctor.py       #   抖音链路的深度体检
└── references/             # dtk 的 API 清单、部署笔记、搜索补丁
```

## 参数

| 参数 | 作用 |
|---|---|
| `--to <链接或 token>` | 目标表格。`/base/<token>` 直接写那张表（绝不新建）；`/drive/folder/<token>` 在那文件夹下按账号名找表 |
| `--reuse-last` | 沿用**上次写过的那张表**（用户说「还用上次那张」时加这个） |
| `--forget-last` | 忘掉「上次用过的表」 |
| `--no-write` | 只采集不落表（预览/排障用），不影响「上次用过的表」 |
| `--platform` | `auto`（默认）/ `douyin` / `xiaohongshu` |
| `--count N` | 最多采多少条；不传就采全 |
| `--no-detail` | 小红书专用：只拉列表不逐条开详情，快，但文案/标签/评论数会空 |
| `--with-token` | 小红书专用：作品链接带上 `xsec_token`（能直接点开，但会过期） |
| `--set 列名=值` | 额外常量列（来源列不用写，脚本会补） |
| `--dry-run` | 采是真的，写只报告不落表（也不会记住表） |
| `--out` / `--json` | 另存 13 字段 JSON / 输出机器可读摘要 |

「上次用过的表」记在 `~/.workbuddy/data/content-collect/config.json`，
内容只有名称、链接、token、上次写入时间 —— 不存任何凭据。

## 两个引擎（都在本技能里）

它不采集：认清输入 → 调平台引擎 → 交给飞书写入器。两个引擎都在 `scripts/` 下，
各是一套完整实现，也能单独用：

| 平台 | 引擎 | 位置 | 为什么是它 |
|---|---|---|---|
| 抖音 | `collect.py`（REST 直连自建 dtk 实例） | `scripts/douyin/` | 接口直连最省；**关键词搜索只有它能做** |
| 小红书 | `xhs.mjs`（受控 Chrome） | `scripts/xhs.mjs` | 小红书接口签名在页面 JS 里、网页纯前端渲染，只能让浏览器代劳 |

想给引擎换位置，用环境变量 `CONTENT_COLLECT_DOUYIN` / `CONTENT_COLLECT_XHS` 指过去。

### 抖音为什么不能用浏览器那条路（实测，别再试）

和小红书同样的思路试过：受控 Chrome 打开抖音用户页，拦 `aweme/post` 接口。结果
**直接跳「验证码中间页」**，三种环境都试了：

| 方式 | 结果 |
|---|---|
| 无头 Chrome + 注入 cookie | 验证码中间页 |
| 有头 Chrome + 注入 cookie | 验证码中间页 |
| 有头/无头 + 真实 Chrome profile 副本（59MB，含 Local Storage / WebStorage） | 验证码中间页 |
| 先访问首页种 `__ac_nonce`/`msToken` 再进用户页 | 首页就已被拦 |

抖音的风控比小红书严一个量级，所以抖音只能走「服务端签名 + 接口」这条路（dtk）。

## 要不要 Docker（换机器最关心的事）

**小红书：完全不需要。** 只要本机有 Chrome + Node 22，cookie 自动从 Chrome 抓。

**抖音：需要一对 Postgres + Redis，但不需要 Docker。** Docker 在这个项目里只是
那对数据库的载体，换成机器上现成的服务一样跑：

```bash
# 有 Docker（默认）：容器 + 迁移 + API + worker
bash scripts/douyin/stack.sh up

# 没有 Docker：只要求 .env 里的库可达，跳过容器
bash scripts/douyin/stack.sh deps            # 先探一下通不通
bash scripts/douyin/stack.sh up --no-docker  # 迁移 + 起 API + worker
```

`--no-docker` 做的事：读 `.env` 里的 `DTK_DATABASE_URL` / `DTK_REDIS_URL`，
探测主机端口可达 → 跑迁移 → 起 API 与 worker。换机器时的最小安装是
`brew install postgresql@17 redis`（或指向公司现成的库），把这两个连接串写进
`douyin_api/.env` 即可，不用装 Docker Desktop。

> 另外说明：dtk 只支持 Postgres，没有 SQLite 模式（仓库里 grep 不到 sqlite），
> 所以「连数据库都不要」是做不到的。真要做到零依赖，只能放弃抖音通道。

## 13 字段契约

两个平台产出同一组字段名，下游按列名认列，**名字改不得**：

| 字段 | 抖音 | 小红书 |
|---|---|---|
| 账号名称 | 作者昵称 | 作者昵称 |
| 账号主页链接 | 作者主页 URL | `.../user/profile/<user_id>` |
| 搜索关键词 | 关键词输入的词；链接输入为空 | 恒空（不支持关键词） |
| 发布日期 | 北京时间 `YYYY-MM-DD HH:MM:SS` | 同（笔记 `time` 毫秒换算） |
| 作品标题 | 描述首行 | 详情页 `title`，退回列表 `display_title` |
| 作品标签 | 话题标签「、」连接 | 详情 `tagList` ＋ 正文 `#话题#`，去重 |
| 作品文案 | 完整描述 | 详情页 `desc` 原文不裁剪 |
| 点赞 / 评论 / 收藏 / 分享 | 采集时刻快照 | 详情页 `interactInfo`；小红书不给游客看的项留空 |
| 作品链接 | `douyin.com/video/<id>` | `xiaohongshu.com/explore/<id>`（默认**不带** token） |
| 视频链接 | 带签名临时直链（会过期） | 视频笔记的 `masterUrl`；图文为空 |

**采不到就留空，不编**：`None`/空串直接跳过不写；新建记录自然是空的，
更新已有记录时保留原值（不拿空值覆盖别人填过的内容）。

## 落表规则

- **用户给 `/base/<token>` 就只写那张表**：不新建、不改名、不在别处留副本；
  工作表名对不上直接报错并列出现有表，不猜。
- **只更新、不新增**：按「作品链接」判定。比对前把链接归一成「协议＋域名＋路径」
  （去掉 `?` 之后的一切），所以小红书 `xsec_token` 变了也不会长出新行。
  单批 200 条、同一张表串行写。
- **列名与 13 字段逐字一致**，表里列的顺序随便调；已存在的列沿用它的类型。
- 依赖 `lark-cli` 已登录且对该表有编辑权限（脚本固定 `--as user`）。

## 登录态（严格自动，两边都不用你动手）

| 平台 | 怎么来的 |
|---|---|
| 抖音 | 开跑前调 `cookie.py ensure`：无可用身份或 cookie 临期（默认 3 天）就自动从本机 Chrome 抓一份；撞 `IDENTITY_POOL_EXHAUSTED` / `UPSTREAM_RISK_CONTROL` 会刷一次 cookie 再重试一次 |
| 小红书 | 每次跑都调 `xhs_cookie.py export`：从 Chrome 解密 `xiaohongshu.com` 的 cookie 注入受控浏览器；取不到会说明原因并降级成游客 |

前提只有一个：**本机 Chrome 里登录着对应平台**。首次读钥匙串会弹一次系统授权
（点「始终允许」），之后静默；**沙箱会挡住授权框**，在 WorkBuddy 会话里要放到沙箱外跑。

**不会悄悄用旧凭据**：抖音刷新失败且池里没有可用身份时直接报错退出（`state=blocked`），
不会拿过期的 cookie 去空跑；小红书的降级会在 stderr 明说「按游客跑」。

## 小红书怎么保证「采全」（三个坑，都踩过）

1. **首屏 30 条不发请求**：主页第一屏是服务端渲染进 HTML 的，页面不调
   `user_posted`。只认接口响应会一条都拿不到 —— 必须在页面内扫
   `section.note-item` 卡片并持续累计。
2. **列表是虚拟滚动**：节点会被回收，`scrollTo(bottom)` 只渲染末尾一屏。
   要逐屏滚（每轮约 1.2 屏）＋页内累计。
3. **`has_more` 不能当结束信号**：拦到的那页常是尾页，`has_more` 天然 false，
   按它停会停在半截。只认「连续 15 轮滚不出新卡片」。

另外主页底部会混进「相关推荐」（别人的笔记），已按卡片 href 里的作者 uid 过滤。
采完条数应与主页「笔记」数量一致；对不上就重跑一次（幂等，不会重复）。

## 小红书作品链接要不要 token

默认输出**裸链** `.../explore/<note_id>`：干净、可长期保存。代价是小红书现在对
裸链跳 404（`error_code=300031`），带 token 才打得开 —— 同一篇笔记、同一浏览器、
同一分钟实测对照过。要能直接点开就加 `--with-token`（token 会过期）。

## 边界

- **小红书不支持关键词**：站内搜索对非登录会话直接返回空，脚本会明确报错并给出
  两条替代路（换主页链接 / 关键词交给抖音）。
- **一次一个账号**：小红书要占用一个受控 Chrome（端口 9333、独立 profile），
  别并行开多个实例；滚动与翻详情之间有 500ms 间隔（`--delay` 可调，别调到 0）。
- **只采公开内容**：私密账号、需登录才可见的笔记采不到，会如实报错。
- **受控 Chrome 不碰你正在用的窗口**：它用自己的 profile
  （`~/.workbuddy/data/xiaohongshu-cdp/`），第一次跑自动拉起（无头、不可见），
  之后复用；要关：`node scripts/xhs.mjs --stop`。

## 排查

```bash
S=~/.workbuddy/skills/content-collect/scripts
python3 "$S/doctor.py" check        # 完整检查：工具链 + 两平台 + 飞书
python3 "$S/doctor.py" plan         # 只看「装没装、缺的怎么装」
bash "$S/douyin/stack.sh" deps      # 只探抖音的库通不通
```

| 现象 | 原因与处理 |
|---|---|
| 命令退出码 `3` | 有上次的表等你确认：`--reuse-last` 沿用，或 `--to <新链接>` 换 |
| 命令退出码 `4` | 还没目标表：向用户要一条飞书多维表格链接 |
| 抖音 `API 不可达` | dtk 没起：`stack.sh up`（有 Docker）或 `up --no-docker`（用本机库） |
| 抖音 `worker 没跑` | 采集会永远卡在 `running`；`stack.sh up` 会把 API 和 worker 一起起 |
| `等超时：主页首屏` | 小红书账号不存在/被限制，或登录态过期（`xhs_cookie.py status`）；括号里带当前停在哪 |
| `（当前停在 …/website-login/captcha）` | 撞安全验证，等一会儿再跑，或先在受控浏览器里过一次验证 |
| `受控 Chrome 没能在 20 秒内起来` | 端口被占；看 `~/.workbuddy/data/xiaohongshu-cdp/chrome.log` |
| 小红书条数比主页少 | 虚拟列表没滚完 / 风控少加载；重跑（幂等） |
| 表里出现重复行 | 检查 `作品链接` 列类型（应为文本/超链接，不是多选） |
