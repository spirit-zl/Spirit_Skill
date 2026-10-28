# 排障

按症状查。每条都给出判断依据和改法，不要盲改代码。

## CDP 连不上

症状：`CDP 连接失败` / `CDP 超时 ... Page.navigate` / `WebSocket error 403`。

1. 先跑自检：`node scripts/capture.mjs --check`，看 `debugging` 是不是 `null`。
2. 走 `--tab` 接管用户浏览器时，必须在 Chrome 打开 `chrome://inspect/#remote-debugging` 并勾选
   **Allow remote debugging for this browser instance**；这是 Chrome 137 之后的手动开关，不能靠命令行补。
3. 端口探测会先读 `DevToolsActivePort` 文件，再试 9222/9333/9334。**Chrome 155 起这个端点不再提供
   `/json/version`（一律 404）**，只能用文件第二行那个带 UUID 的 `/devtools/browser/<uuid>` 路径。
4. 导航本身超时（站点慢或被墙）不代表失败：脚本会吞掉这个超时继续等页面内容。真拿不到内容再看下一条。

## 端口在 listen，但握手一直超时

先查有没有**残留的客户端进程**占着这个单客户端端点：

```bash
lsof -nP -iTCP:9222 | grep ESTABLISHED
```

如果有 node 进程挂着，`kill -TERM <pid>`（别用 `-9`）就能释放，不用重新勾选。
脚本自身的 SIGTERM 处理会先关 WebSocket 再退出。

如果没有任何 ESTABLISHED 连接、仍然握手超时，才是端点真卡住：让用户在
`chrome://inspect/#remote-debugging` 取消勾选、等 3 秒、再勾上。

## 只抓到最后一屏 / 内容少了一大半

看页面里有没有 `.virtual-list`：

```bash
node scripts/capture.mjs --tab <id> --probe    # 再看 meta.json 的 pageHeight
```

高度几万像素、正文却只有几千字，基本就是虚拟列表。脚本会自动逐屏收集（日志会打印
「检测到虚拟列表」和「收集到 N 块」）。如果仍然偏少，把 `--wait` 调大再跑一次；
排查时可以 `--no-virtual` 对比单屏结果。

注意：这类页面的整页截图同样会大片空白，脚本会自动改成「逐屏截图再拼接」
（日志打印「逐屏截图」），耗时随页高线性增长。

## 无头 Chrome 起不来

症状：日志刷 `sandbox initialization failed: Operation not permitted`，随后 network service 崩溃。

原因：Chrome 自带 sandbox 不能在已沙箱化的环境里再初始化一层。
脚本启动时已固定带 `--no-sandbox`，如果自己手动拉 Chrome 复现问题，也要加这个参数。
用临时 profile（`--user-data-dir=/tmp/...`）避免和正在运行的 Chrome 抢同一个用户目录。

## 图片一张都没抓到

1. 看 `meta.json` 的 `images` 是不是空数组。
2. 微信公众号、知乎、掘金这类站点把真图放在 `data-src` / `data-original`，`src` 是 1px 的
   `data:image/svg+xml` 占位图。`extract-in-page.js` 已经优先取懒加载属性；遇到新站点先检查它用的是哪个属性名。
3. 图标类图片会被尺寸规则丢掉：小于 100×100 像素（`w * h < 10000`）直接跳过。正文配图一般远大于这个阈值。
4. 图片在 `picture > source` 或 shadow DOM 里时抓不到，需要扩 `extract-in-page.js` 的选择器。

## 图片下载失败，文档里变成远程链接

日志形如 `图片 12 失败（TypeError: Failed to fetch），保留远程地址`。

- 页面内 `fetch` 失败会自动回退到 Node 直连（带 Referer/UA）；两边都失败才会保留远程 URL。
- 常见原因是防盗链（校验 Referer 域名）或图片域名需要自家 cookie。用 `--tab` 接管已登录的标签页再抓，
  请求就会带上真实凭证。
- 保留远程 URL 不是致命错误：`lark-cli` 导入 Markdown 时会再下载一次，能下就仍然会落到文档里。

## 文档创建失败

- `no permission` / `forbidden`：文件夹 token 不是当前授权用户有写权限的目录；换 `--folder` 或让所有者授权。
- 报 `unsafe file path`：`lark-cli` 的 `@file` 只接受**当前工作目录下的相对路径**。
  `publish.mjs` 已经 `chdir` 到抓取目录再调用，不要手动改成绝对路径。
- 文档里出现字面量 `<b>`：Markdown 里的尖括号会被当成 XML 标签解析，写内容时左尖括号要转义成 `\<`。

## 长截图失败或只剩分片

- 页面高度超过 16000px 会分片（每片 8000px）再用 Pillow 拼接。找不到带 Pillow 的 Python 时保留
  `fullpage_tile*.png`，文档附件会缺失。
- 指定解释器：`PYTHON=/path/to/python node scripts/capture.mjs ...`。
- 超长页（>30000px）在高 DPR 下容易把渲染进程压垮，把 `--dpr 1 --width 1000` 降下来，或直接 `--no-shot`。

## 抓到的是登录页 / 内容不全

用 `--tab` 接管用户已登录的标签页：`--list` 找到 targetId，再 `--tab <targetId>`。
`--url` 模式用的是临时 profile，没有登录态。

## 标题太长或不对

`--title "自定义标题"`。默认取 `og:title`，其次页面第一个 `h1`，再次 `<title>`。

## 只抓到开头一小段，后面章节全没有

飞书文档、语雀、Notion 这类是**懒渲染**：不滚到那个位置，DOM 里压根没有那些块。判断方法：

```bash
node scripts/capture.mjs --tab <id> --probe   # textLength 很小，但页面很高 → 就是它
```

脚本已经改成多轮滚动（每轮滚到底、等 900ms、比较正文字数是否继续增长，最多 4 轮）。
如果还是不全，把 `--wait` 调大、或者手动在浏览器里先滚一遍再抓。

注意：滚动期间页面会自己动、视口会被临时改成 `--width` 指定的宽度，抓完会还原。

## 长截图拼接失败

- `OSError: broken data stream when writing image file`：JPEG 单边上限 65535px，页面比这更高。
  脚本已按比例缩到 65000px 以内；如果手改过参数，注意这个硬上限。
- 拼接后底部一大片纯白：画布要用白底（`Image.new(..., "white")`），黑底会在最后一行留一条黑线，
  让「按空白行裁剪」失效。
- 找不到带 Pillow 的 Python：`PYTHON=/path/to/python` 指定，或 `--no-shot` 跳过截图。

## 标题全变成普通段落

文档查看器（飞书、语雀、一堂这类）常把标题写成带 `font-size` 的 div/span，没有 `h1-h6`。
脚本按「正文基准字号（出现最多的字号）+ 1.28 倍」判定标题，并且只采用出现 3 次以上的字号档位，
避免封面大标题把层级带偏。`meta.json` 里的 `debug.fsHistogram` 就是字号分布，调阈值前先看它。

## 想确认到底发了什么

```bash
lark-cli docs +fetch --as user --doc <docId> --doc-format markdown | head -60
```

## 一堂（yitang.top/fs-doc）这类「块级虚拟列表」

现象：`--probe` 报 `textLength` 只有两三千、`pageHeight` 却有 5～6 万 px，抓下来只有开头几屏，
结尾正好卡在「全文评论」前。

根因有三条，缺一不可：

1. 正文块在一个 `.virtual-list` 里，但**它不是滚动容器**（`clientHeight === scrollHeight`，
   真正的滚动发生在 `window`）。对着它设 `scrollTop` 等于没滚。
2. 块用 `role="listitem"` 渲染，每块内部 `.block` 带 `index="N"`（0 起、稳定），
   离开视口就被回收，所以只能边滚边收，且必须按 `index` 去重（不能按 `offsetY`）。
3. 页面 6 万 px ÷ 视口 500px ≈ 120 屏，把「逐屏收集 + 逐块算字号」塞进一个
   `Runtime.evaluate` 必然超时（脚本里 `harvestVirtual` 的 330s 上限就是这么撞的）。

可用做法：把收集拆成多次 `Runtime.evaluate`，每次 40s、状态挂在 `window.__YT__` 上，
按 `index` 存 `outerHTML`（只存 HTML，不在这轮算字号），滚完再离屏重组 + 跑 `extract-in-page.js`：

```js
// 每批：window.scrollTo(0, y) → 等 130ms → 取 group.children 里每个 listitem
const inner = el.querySelector('.block');
const idx = parseInt(inner.getAttribute('index'), 10);   // 去重键
// 顺手把 img 的 currentSrc 写进 data-wb-src、尺寸写进 data-wb-size
```

标题层级不用字号反推：块类名就写着 `docx-heading1-block` / `docx-heading2-block`，
`extract-in-page.js` 的 `data-wb-h` 预扫描能直接认出来。

两个后处理坑：

- 有序/无序列表的序号是独立 DOM 节点（`.order` / `.bullet-dot-style`），提取时会被丢掉。
  重组前把序号文字塞回 `.list-content` 第一行，提取后会得到 `1. xxx` / `• xxx`；
  再在 markdown 里把 `• ` 换成 `- `、`^\\(\d+)\.` 还原成 `1.`，飞书才能识别成列表。
- 提取器会把行首 `1.` 转义成 `\1.`（多一个反斜杠），记得统一修掉。

顺带：该站有 `/api/feishu/get-doc-blocks`，但返回的 `data` 是加密串，还要带 `X-token-1/2`，
不如直接抓 DOM。

### blocklist 掉一两张图（`图片 207 失败：terminated`）

2026-10-24 实测：244 张里 1 张 1.8MB 的大图在 `fetch` 阶段被掐断（`terminated`），原实现不重试，
文件编号就空一格（001–206、208–244 共 243 个），正文里那条留成远程 URL。

已修：`download()` 改成 45 秒超时 + 3 次退避重试。万一还漏，可以手工补：

```bash
curl -sSL -H "Referer: https://yitang.top/" -o images/207.png "<失败的图片 URL>"
# 然后把 content.feishu.md 里的 ![](<URL>) 换成 ![](@./images/207.png)，content.md 同步
```

补图时**文件名要按它在正文里的位置补**（不是顺延到 244），否则编号会和正文顺序错位。

## 飞书文档复制：图片少几张 / 文档里残留 `feishu.cn/file/...`2026-10-22 前的 `feishu_copy.mjs` 用 `!\[([^\]]*)\]\(...\)` 匹配图片，**alt 里出现 `\]` 转义时整条匹配失败**
（飞书自动生成的图片描述里经常带 `\[红心\]` 这类转义），表现为：日志说「资源取回 116/116」，
但 markdown 里仍留着两三条远程 URL。

已改成允许转义的正则 `!\[(?:\\.|[^\]\\])*\]\(...\)`，并按 token 去重、按首次出现顺序连续编号。
自检：

```bash
grep -c 'feishu.cn/file/' content.feishu.md      # 正常应为 0
ls images | wc -l                                # 应等于 meta.json 的 imageTotal
```

如果还有残留，先看那条 alt 里是否有别的转义字符（`\*`、`\_`），再决定是否放宽正则。

## 给图片重排序号，别用链式 split/join

`md.split('(017)').join('(018)')` 这种写法会连锁命中：改完 017→018，接着 018→019 又把刚改好的那条改了，
整篇图号串位、正文对不上图。**要改号就一次性 `replace(re, cb)` 按 token 回调**，或先算好新旧名字再统一写。

## 知识库节点被写满 / 应该加子文档却覆盖了节点

`--folder` 给 `/wiki/<token>` 时应走「建子文档」，`--doc` 才是覆盖正文。
误用 `--doc <wiki 链接>` 会把占位节点的正文整篇替换掉（节点就不再是容器了）。
改目标前先 `publish.mjs --dry-run` 看它判成 `wiki` 还是 `folder`。

## 旧版文档 `/docs/doccn…`：docs +fetch 报 unsupported

症状：`docs +fetch` 提示 `unsupported --doc input ".../docs/doccn..."`；
换 `drive +export --doc-type doc` 又报 `permission_denied`（1069902，跨租户）。

这类是**旧版文档（etherpad 渲染）**，不是 docx。用 `scripts/legacy-doc.mjs --url <链接>`。

抓的时候有三个坑，都是 2026-10-22 实测踩到的：

1. **正文虚拟化**：`.ace-line[data-node]` 离屏即回收，页面只有 28 行、
   `textLen` 一两千字，但容器高 2.6 万 px。必须按行坐标边滚边收，用动态高度循环
   （滚动过程中高度会涨到 5 万+）。
2. **滚动容器不是 window**：是 `.etherpad-container-wrapper`（`sh 26473 / ch 849`）。
   对 window 做 `scrollTo` 等于没滚。
3. **图片全是 1×1 占位**：`img.src` 是 `data:image/png;base64,iVBORw0KGgo...`，
   真实地址在 `img[data-src]`（`internal-api-drive-stream.feishu.cn/space/api/box/stream/download/preview/...`）。
   页面懒加载经常不触发（79 张里只自动加载 7 张），所以别等它加载：
   用 CDP `Network.getAllCookies` 取 cookie，在 Node 侧直接下载 `data-src`。
