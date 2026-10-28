---
name: yitang-to-feishu
version: 1.4.0
display_name: 一堂课件转飞书
description: 把一堂课件（yitang.top 的 fs-doc 课程文档）或普通网页抓成 Markdown 并发布到指定飞书云文档目录，图片全部下载后按原位置内联、可附整页长截图；同时适用于公众号文章、登录墙内的课程文档与报告归档，能完整收割块级虚拟列表文档。
---

# 一堂课件转飞书

把「一个网页」变成「一篇飞书文档」：正文按原顺序转 Markdown，图片全部下载并留在原来的位置，另存一张整页长截图，最后落到你指定的飞书文件夹。

## 前置条件

- Node.js 22+（`node -v`）
- 本机装了 Chrome（macOS 默认 `/Applications/Google Chrome.app`）
- `lark-cli` 已授权 user 身份：`lark-cli auth status` 里 `identities.user.status` 为 `ready`

## 两种取页方式

### A. 公开页面：直接给 URL

```bash
node ~/.workbuddy/skills/yitang-to-feishu/scripts/run.mjs \
  --url "https://example.com/article" \
  --folder "https://my.feishu.cn/drive/folder/xxxxxxxx"
```

自己拉起一个临时 profile 的无头 Chrome，不碰用户的浏览器。

### B. 需要登录的页面：接管用户的标签页

登录墙（SSO、付费文档、内部系统）破解成本高，正确做法是**借用用户已登录的会话**，而不是 OCR：

1. 让用户在 Chrome 打开目标页面（也可以自己用 CDP `Target.createTarget` 在对方浏览器里开一个后台标签页，
   登录态直接复用，不用让用户手动开）
2. 打开 `chrome://inspect/#remote-debugging`，勾选 **Allow remote debugging for this browser instance**
3. `node .../capture.mjs --list` 找到目标 `targetId`
4. `node .../capture.mjs --tab <targetId> --out <目录>`，再 `publish.mjs --dir <目录>`

几点必须知道：

- Chrome 137+ 的这个开关是**每个浏览器进程一次**，重启 Chrome 后要重新勾
- 从 Chrome 155 起，这个端点**不再提供 `/json/version`**，只能读
  `~/Library/Application Support/Google/Chrome/DevToolsActivePort` 第二行的 WebSocket 路径
- 该端点同一时间只接受一个调试客户端。**不要用 `kill -9` 打断抓取进程**，否则端点会卡死（端口还在 listen，
  但握手一直超时），只能让用户取消勾选再重新勾上来恢复
- 抓完会还原被改动的样式和视口，并清掉抓取标记，不会污染用户页面

## 先体检，再抓取

拿不准页面结构时先跑：

```bash
node .../capture.mjs --tab <targetId> --probe
```

输出 `textLength` / `imgCount` / `canvasCount` / `iframes`：

| 体检结果 | 说明 |
|----------|------|
| `textLength` 几千、`canvasCount` 0 | 正常 DOM，直接抓，文字和原图都能拿到（最佳） |
| `canvasCount` 大、`textLength` 很小 | 文字被渲染成 canvas，只能截图识别，见「已知边界」 |
| `iframes` 非空 | 正文可能在 iframe 里，脚本会自动逐个 frame 重试提取 |

### 一个链接背后是多篇文档（课程、手册、知识库）

症状：抓下来的正文只有几千字，结尾直接出现「全文评论 / ×× 评论」，但左侧目录还列着十几节。

原因：目录节点是**独立页面**，不是同一页的锚点；默认只抓了打开的那一篇。

```bash
node .../capture.mjs --tab <targetId> --toc --out <目录>
```

行为：展开目录树 → 读所有节点 → 逐个点击 → 每章等导航、滚动、提取、下载图片 →
合并成一篇（章节标题为 `##`，章节内标题整体降一级，图片按 `章节号-序号` 命名）。
文首会生成一份纯文本目录。

注意：`--toc` 默认不截整页长图（每章各截一张没有意义），需要的话加 `--shot-each`，图存 `shots/`。

### 块级虚拟列表（一堂 `fs-doc` 这类）

症状：`--probe` 报 `pageHeight` 五六万像素、`textLength` 只有两三千，抓下来只有开头几屏
（一堂实测只拿到 41 块 / 4 图，后面 800 多块全丢）。原因是正文在 `.virtual-list` 里，
块离开视口即回收，而且**滚动发生在 window 上**（`.virtual-list` 自身不可滚）。

```bash
node .../blocklist.mjs --url "<页面地址>" --out <目录> --folder <wiki节点|文件夹>
```

按 `.block[index]` 做身份去重、分批 evaluate 收割（每批 40 秒，避免单次调用超时），
补回被丢掉的列表序号，离屏重组后走同一套 `extract-in-page.js`，最后自动调 `publish.mjs`。

**Chrome 155 的 `chrome://inspect` 勾选是一次性的**：客户端断开后端点还在 listen 但握手永远超时。
所以 `blocklist.mjs` 不再依赖它，而是自建一个**专用调试 Chrome**：无头、独立 profile
（`~/.workbuddy/data/chrome-profile`）、固定端口 9333，端点可反复连接，首次登录后长期复用。

### 登录态：三个小脚本

| 脚本 | 作用 |
|------|------|
| `ensure-chrome.mjs` | 复用/拉起专用调试 Chrome，输出 ws 地址（`--json` 带 mode） |
| `sync-login.mjs` | 把用户 Chrome 里该站点的 cookie + localStorage token 搬到专用 Chrome |
| `login.mjs` | 兜底：截微信二维码到 png、轮询到登录成功（二维码 50 秒自动刷新） |

`blocklist.mjs` 已串好：拿端点 → 打开页面 → 落到登录页就先 `sync-login` 再重试 →
仍然要登录才提示跑 `login.mjs` 扫码。**用户 Chrome 不需要勾选、不需要双击任何文件**；
只要它开着（有调试端点）就能顺带同步登录态，没有就退回扫码。

停掉专用 Chrome：`pkill -f "remote-debugging-port=9333"`（下次运行会自动拉起，登录态还在）。

## 飞书文档 / 知识库链接：走 API，别抓页面

目标本身就是飞书文档（`*.feishu.cn/wiki/...` 或 `/docx/...`）时，抓 DOM 是绕远路：

```bash
node ~/.workbuddy/skills/yitang-to-feishu/scripts/feishu_copy.mjs \
  --url "https://xxx.feishu.cn/wiki/XXXX"
```

- `docs +fetch` 拿到的是**标准 markdown**（标题层级、callout、附件都保留），比 DOM 提取更准，也更快
- **跨租户**（如 `yitanger.feishu.cn` 的一堂课文档）也能用 `docs +fetch --as user` 直读，
  不用退化成抓 DOM；只要文档对「拿到链接的人」可读就行
- 图片在 markdown 里是 `https://feishu.cn/file/<token>`。跨租户文档没有导出权限，
  `docs +media-download` 会报 `permission denied`；改用 `docs +media-preview --token <tk>` 可以取回原图
- **飞书图片的 alt 是自动生成的一大段描述**，直接转 markdown 会让每张图挂一段 caption，
  复制时要丢掉（脚本已处理：alt 一律置空）
- 附件 `<source token="...">` 取回后写成 `<source path="@./files/xxx.pdf"/>` 再上传

## 旧版文档（`/docs/doccn…`，etherpad 渲染）

链接长这样：`https://xxx.feishu.cn/docs/doccnXXXXXXXX`（注意是 `/docs/`，不是 `/docx/`）。
一个 token 是 `doccn` 开头的老文档，**两条常规路都不通**：

- `docs +fetch` 直接拒：`unsupported --doc input ... use a docx URL/token or a wiki URL that resolves to docx`
- `drive +export` 可导 docx/pdf，但跨租户会报 `permission_denied`（1069902）

走专用脚本：

```bash
node ~/.workbuddy/skills/yitang-to-feishu/scripts/legacy-doc.mjs \
  --url "https://yitanger.feishu.cn/docs/doccnXXXXXXXXXXXXXXXX" \
  --folder "https://my.feishu.cn/wiki/XXXX"
```

它自己会：拉起/复用专用调试 Chrome（9333）→ 打开页面（必要时从用户 Chrome 同步登录态）→
按 `.ace-line` 的行坐标滚动收集（离屏行会被回收，必须边滚边收）→ 转 markdown → 下载图片 → 调 `publish.mjs`。

两个必须知道的点：

- 正文在 `#innerdocbody` 的 `.ace-line[data-node]` 里，**滚动发生在 `.etherpad-container-wrapper` 上**
  （不是 window）；滚动过程中 `scrollHeight` 会从 2.6 万长到 5.3 万，所以循环要用「动态高度 + 到底判定」，
  不能先算总步数
- 图片的 `src` 常常是 1×1 的 `data:image/png` 占位（lazy 加载不一定触发），真实地址在 **`img[data-src]`**。
  这个脚本不依赖图片加载，直接拿 `data-src` + CDP 取到的 cookie 在 Node 侧并发下载，比等页面加载稳
- 输出与 `capture.mjs` 一致（`content.md` / `content.feishu.md` / `images/` / `meta.json`），可继续用 `publish.mjs` 重发

## 目标位置：给什么链接，就落在什么位置

`--folder` 接三种链接，**按链接类型自动判定**（2026-10-22 定，别自己拿 token 猜）：

| 你给的链接 | 行为 | 落地结果 |
|------------|------|----------|
| `https://my.feishu.cn/drive/folder/<token>` | 新建文档 | 文件夹下多一篇新文档 |
| `https://my.feishu.cn/wiki/<node>` | **在该知识库节点下新建子文档** | 节点正文不动，左侧目录多一个子节点 |
| `https://my.feishu.cn/docx/<token>` 或 `--doc <链接>` | 原地覆盖 | 链接不变，正文整篇替换 |

要点：

- wiki 节点是**容器**，不是文档：`docs +create --parent-token <wiki token>` 会自动建成它的子节点，
  返回的是 `my.feishu.cn/docx/<id>`，`publish.mjs` 会再查一次子节点列表，把 `my.feishu.cn/wiki/<node>`
  链接还给你（最好直接把这个链接给用户）
- **不要**对 wiki 节点用 `--doc`：那是覆盖节点自身正文，会把占位节点写满，跟「加子文档」的预期相反
- 新建时 `--title` 由飞书设置标题，正文里同名首行 `# 标题` 会被自动去掉，避免标题出现两遍
- 目标链接会被记进 `config.json` 的 `lastTarget`；`--folder` 不传时沿用上次的

## 拆开用

### 1. 只抓取，不发布

```bash
node .../capture.mjs --url "<地址>" [--out <目录>] [--tab <targetId>]
```

输出目录（默认 `./webpage-captures/<slug>-<时间戳>/`）：

| 文件 | 内容 |
|------|------|
| `content.md` | 本地可读版，图片引用 `images/001-xxx.png` |
| `content.feishu.md` | 飞书导入版，图片引用 `@./images/001-xxx.png` |
| `images/` | 按出现顺序编号的图片 |
| `fullpage.png` / `fullpage.jpg` | 整页长截图（超高页面用 JPEG） |
| `meta.json` | 标题、来源、图片清单、页面高度、字号诊断 |

### 2. 从已有目录发布

```bash
node .../publish.mjs --dir <抓取目录> [--folder <文件夹|知识库节点>] [--doc <文档>] [--no-attach] [--dry-run]
```

`--dry-run` 只打印识别到的目标类型和标题，不落任何东西，改目标前先跑一遍最稳。

### 3. 一次给多个链接

```bash
node .../batch_copy.mjs --folder <文件夹|wiki节点> \
  --url "https://xxx.feishu.cn/wiki/AAA" --url "https://xxx.feishu.cn/wiki/BBB" \
  [--urls-file list.txt] [--out-root 目录] [--dry-run]
```

逐个跑 `feishu_copy`：先用 `drive +inspect` 取真实标题（有些文档正文里没有 `<title>`，
不取就只能叫「未命名文档」），再抓正文/图片/附件并发布。**单篇失败不影响整批**，
结果写进 `<out-root>/result.json`，退出码 1 表示有失败。

- 目标类型照旧自动判定：文件夹 → 一篇新文档；wiki 节点 → 一个子文档
- `--dry-run` 只把标题和顺序列出来，适合先确认清单对不对
- 附件多、图片多的文档会慢（实测 71 图 139 秒），批量时耐心等，别中途 Ctrl-C

## 参数

| 参数 | 说明 | 默认 |
|------|------|------|
| `--url <URL>` | 自己开标签页抓（必要时自动拉起无头 Chrome） | — |
| `--tab <targetId>` | 接管用户已打开的标签页（要登录态时用） | — |
| `--list` | 列出可接管的标签页 | — |
| `--probe` | 只体检页面，不产出 | — |
| `--toc` | 按侧边栏目录逐章抓，合并成一篇 | — |
| `--toc-max <n>` | 最多抓多少章 | 60 |
| `--shot-each` | 每章各截一张长图（存 `shots/`） | — |
| `--no-virtual` | 关掉虚拟列表收集（排查用） | — |
| `--out <dir>` | 输出目录 | `./webpage-captures/...` |
| `--width <n>` | 视口宽度，宽表格页面调大 | 1200 |
| `--dpr <n>` | 设备像素比，2 更清晰但体积翻 4 倍 | 1 |
| `--wait <ms>` | 加载等待上限 | 20000 |
| `--no-shot` | 不出长截图 | — |
| `--no-attach` | 长截图不作为附件放进文档 | — |
| `--folder <链接\|token>` | 目标**文件夹或知识库节点**（按链接类型决定新建/加子文档） | 配置文件里的默认值 |
| `--doc <链接>` | 原地覆盖这篇文档（链接不变） | — |
| `--title "..."` | 覆盖文档标题 | 网页标题 |
| `--dry-run` | 只打印目标判定，不执行 | — |
| `--own-chrome` | 强制另起无头 Chrome | — |

默认目标记在 `~/.workbuddy/data/yitang-to-feishu/config.json`，`--folder` 只第一次要给。

## 工作原理

1. `Page.navigate` 打开页面，等 `readyState=complete` + DOM 元素数量稳定
2. 展开 `overflow:auto/scroll` 的内部滚动容器
3. **多轮滚动触发懒渲染**（飞书/语雀文档不滚到底，后面的块根本不在 DOM 里），直到正文字数不再增长
4. **虚拟列表单独处理**：页面有 `.virtual-list` 且高度 > 8000px 时，正文只在视口附近渲染，滚过的块会被回收。
   这时边滚边把每块 `outerHTML` 连同真实图片地址、原始字号一起收集下来，离屏重组成一个容器再统一提取
   （否则只能拿到最后一屏的内容，实测会少 80%）
4. 在页面里跑 `extract-in-page.js`：按文本密度 + 链接密度选正文根节点，剔除导航/广告/侧栏/评论区，再按块遍历生成 Markdown
5. 图片优先取 `data-src`（微信、知乎、飞书文档的真图在这里，`src` 只是占位图），页面内 `fetch` 带 cookie 下载，
   失败重试一次再回退 Node 直连（带 Referer/UA）
6. **标题层级按字号反推**：文档查看器常把标题渲染成带字号的普通 div。取正文基准字号（出现最多的那个），
   明显更大的短块升为标题；只采用出现 3 次以上的字号档位，避免封面大标题把阶梯带偏
7. 发布时 `lark-cli docs +create --doc-format markdown` 直接吃 `@./images/xxx`，图片由飞书上传并原位插入；
   长截图用 `docs +media-insert --type file` 追加成附件卡片
8. 整页截图：`captureBeyondViewport` 截取，超过 16000px 分片拼接；超过 20000px 改 JPEG（PNG 体积过大），
   超过 65535px 按比例缩放（JPEG 单边硬上限），最后裁掉文末空白

## 验收

```bash
ls <目录>/images | wc -l                       # 与 meta.json 里 file 非 null 的数量一致
lark-cli docs +fetch --as user --doc <docId> --doc-format markdown | head -40
```

## 已知边界

- 需要点击/输入才会出现的内容抓不到（只滚动，不模拟交互）
- 视频、iframe、canvas 不会转成 Markdown，只在长截图里保留
- 小于 100×100 像素的图按图标丢弃
- 图片防盗链且页面内 `fetch` 也失败时，保留远程 URL，交给飞书侧再试
- **canvas 渲染的文档**（文字是像素不是 DOM）：DOM 提取拿不到文字，只能把长截图切成可读块交给模型识别。
  先 `--probe` 确认 `canvasCount`，确实如此再走这条路，识别质量不如 DOM

排障看 `references/troubleshooting.md`。
