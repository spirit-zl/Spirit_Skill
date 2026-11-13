# 抓取链路与技术细节

## 平台结构

- 域名：`app.cn.sztg.com`（页面）、`api.cn.sztg.com`（接口）、`cx3oss.ceniu.sztg.com`（回放分片）、`web.ceniu.sztg.com`（登录页）。
- 页面是 React SPA + Aliplayer，**静态抓取只能拿到标题**，正文全靠 JS。
- **整站登录墙**：未登录打开直播页会 302 到 `web.ceniu.sztg.com/#/login`，所有接口返回 `{"errcode":66015,"errmsg":"token为空"}`。
- 登录方式：微信扫码，或账号登录。登录后 `bossdk.ahinfo.loginstatus === 1`。

## 登录态持久化

- profile 目录：`~/.workbuddy/data/sztg-replay/profile`（Chrome user-data-dir，约 250MB）。
- 只要 Cookie 不过期，直接复用即可，不需要再次扫码。实测隔一段时间仍有效；失效时 `status` 退出码为 4。
- 不要把 profile 提交进 git。仓库 `.gitignore` 已排除。

## 关键接口

前缀 `https://api.cn.sztg.com`，公共参数 `uniqueid=<ahinfo.uniqueid>,<随机数>` 与 `bostoken=<ahinfo.bostoken>`，两者都能从页面的 `bossdk.ahinfo` 读到。

| 接口 | 用途 |
|---|---|
| `/ZhiboSystem/GetZhiboSystem.html?id=<roomid>` | 直播间信息、课程列表（`schedules`，含 `livestream*` 直播地址） |
| `/ZhiboSystem/GetZhiboSystemSchedulePlaybackList.html?scheduleid=` | 回放列表（实测常返回 null，以页面点击为准） |
| `/v4/GetLiveCoverageTotal.html?classid=` | 图文直播（该课为空，说明内容只有视频） |
| `/auth/v3web.html` | 鉴权，匿名也能返回 0 但 `bostoken` 为空，不足以调业务接口 |

## 抓回放的技术路线

1. 用 CDP 打开直播间页面，确认 `loginstatus===1`。
2. 读 `.item` 列表（标题 + 时间），按 `--index` 或 `--date` 选一期。
3. 开启 `Network`，点击该项，监听 `Network.responseReceived`，拦到形如
   `https://cx3oss.ceniu.sztg.com/d/<livestreamid>/<n>_<YYYYMMDDHHMMSS>.m3u8` 的地址。
4. 用**文件名里的 8 位日期**确定回放日期（比页面文案可靠）。
5. 下载 m3u8 → 并发抓 TS（12 并发，带 `Referer: https://app.cn.sztg.com/`）→ 写 concat.txt。
6. ffmpeg concat 后抽 16k 单声道 48k mp3，31 分钟约 11MB，可直接整段转写。
7. 时长用 `ffmpeg -i audio.mp3` 的 Duration 解析，写进 meta.json。

## 转写

- 用 AudioTranscribe，整段投递（上限 1GB / 5 小时，本项目远低于）。
- 输出形如 `[起始,结束,发言人] 文本`，保留时间戳，方便回查原声。
- 转写有固定错字：低吸→DC、PCB→PDB、CPO→cpou、奥士康→奥斯康/奥斯康、超声电子→超声电阻、福莱新材→福莱精彩/福莱精彩。定稿前按画面行情软件校正。

## 已知坑

1. **Chrome 在沙箱里跑不起来**：必须带 `--no-sandbox --disable-gpu-sandbox --disable-dev-shm-usage --disable-gpu`，否则 network service 崩溃、DevTools 端口连不上。
2. **内置浏览器不可用**：`dom_cua_get_visible_dom` 返回空字符串、`tab_screenshot` 报超过 10MB，所以整条链路走本机 Chrome + CDP，不用 mcp__browser__*。
3. **Node 22 自带 WebSocket**，不必装 puppeteer/playwright；CDP 客户端自己实现 60 行足够。
4. **没有 ffmpeg**：`python3 -m pip install imageio-ffmpeg` 拿静态二进制，`imageio_ffmpeg.get_ffmpeg_exe()` 取路径。
5. **lark-cli 的 `--content @file` 有目录白名单**，只允许 cwd、/tmp、~/files。脚本改成 `--content -` 从 stdin 传入，避免临时目录被判越界。
6. **文档标题渲染不一致**：`docs +fetch --doc-format markdown` 有时把标题渲染成 `<title>`、有时渲染成 `# 标题`。查重和校验前先剥掉 `<title>` 标签，否则 H1 计数会错。
7. **不要用 append 代替前插**：append 只会插到文末，文档就变成正序了。前插固定用 `block_insert_after --block-id 0`（0 表示文档开头）。
8. **抽帧核对**：个股名和价位以画面行情软件为准。`ffmpeg -f concat -safe 0 -i concat.txt -ss <秒> -frames:v 1` 抽帧，用 ViewImage 读图核对公司名、代码、缺口标注。

## 换机器

1. 装 Chrome、Node 18+、Python 3、lark-cli，并完成 `lark-cli auth login`。
2. `python3 -m pip install imageio-ffmpeg`。
3. 把 profile 目录拷到 `~/.workbuddy/data/sztg-replay/profile`；没有就 `node scripts/capture.cjs qr` 扫一次。
4. 写 `~/.workbuddy/data/sztg-replay/config.json`：
   ```json
   {"doc": "https://my.feishu.cn/docx/...", "room": "568", "cid": "7552", "eid": "7061"}
   ```
5. `python3 scripts/doctor.py --deep`，全 OK 才算可用。
