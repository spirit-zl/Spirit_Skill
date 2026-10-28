# 环境搭建与排障笔记（本机实测）

本文件记录 **2026-10-15 在本机跑通 dtk v5.1.3** 时踩过的坑与可复现的做法。
换机器或重装时先读这里，能省掉大半时间。

## 0. 换机器：先跑 doctor.py（第一入口）

换机部署不用照本文手工复刻 —— 除下面三步之外，其余都由 `scripts/doctor.py` 代劳：

```bash
S=~/.workbuddy/skills/content-collect/scripts/douyin
python3 "$S/doctor.py check"      # 只读体检：逐项说缺什么、怎么补
python3 "$S/doctor.py install"    # 引导安装：已完成步骤自动跳过（--yes 免确认；非交互环境必须加）
```

**三步只能人工**（脚本只能检查，没法替你操作）：

1. **容器运行时**（`install` 的前置条件）：装 OrbStack（推荐）或 Docker Desktop，
   启动到 `docker info` 能通。缺它会直接提示后退出，其余什么都不做。
   **不想装 Docker 也可以**：Docker 在这里只提供 Postgres + Redis，用本机原生的
   一对服务就行 —— `brew install postgresql@17 redis`，把连接串写进仓库 `.env`
   的 `DTK_DATABASE_URL` / `DTK_REDIS_URL`，然后 `stack.sh up --no-docker`
   （只跑迁移 + 起 API/worker，不碰容器）。`stack.sh deps` 可以先探通不通。
   注意 dtk 只支持 Postgres，没有 SQLite 模式。
2. **Chrome 登录抖音**：`install` 第 10 步从本机 Chrome 抓 cookie 导入身份池
   （首次读钥匙串会弹授权，点「始终允许」）。没有 Chrome / 非 macOS：跳过这步，
   按 §5 手动导出导入。
3. **飞书授权**：要落多维表格时 `lark-cli auth login` 扫码；只采集可跳过。

`install` 覆盖的链路（后面各节即这套流程的展开）：

> 镜像（官方拉不到时按 §2 自建）→ 容器（命名卷 compose）→ 克隆源码并切到补丁锚定
> commit → 打搜索补丁（§7.1）→ `uv sync` → 生成 `.env`（含随机 `DTK_SECRET_KEY`）→
> 迁移 → 启动 API+worker（`stack.sh`）→ 建管理员与 API Key（§4）→ 导入登录态（§5）→
> 读一个真实账号验证（§6）

**卡在哪一步就修哪一步，重跑时已完成的步骤自动跳过。** 以下几个细节也已内建：

- GitHub 直连失败自动换镜像站（ghfast.top / gitclone.com）克隆；
- PyPI 直连失败自动换清华源 `uv sync`；
- Postgres 镜像（`timescale/timescaledb-ha:pg17`）拉不到时，自动探测基础镜像的
  Debian 代号与 CPU 架构、从 packagecloud 取匹配 deb 自建等价镜像 —— 即 §2 那套
  流程的自动化版本；
- 凭据文件（`$DTK_HOME/credentials.env`）合并写入、`chmod 600`，已有键不覆盖；
  复用有效 API Key 时完全不动它。

路径约定（脚本自动发现；本文其余各节写的是本机实测值）：

- 数据目录 `$DTK_HOME`，默认 `~/.workbuddy/data/douyin-data`；
- 源码仓库按 环境变量 `DTK_REPO` → 凭据文件里的 `DTK_REPO` → `$DTK_HOME/repo`、
  `~/douyin_api`、`~/src/douyin_api`、`~/WorkBuddy/*/douyin_api` 的顺序找。

## 1. 组件与端口

| 组件 | 位置 | 说明 |
|---|---|---|
| 源码仓库 | `/Users/spirit/WorkBuddy/spirit_WorkBuddy/douyin_api` | `git clone --depth 1 https://github.com/Evil0ctal/Douyin_TikTok_Download_API.git`，默认分支 main = v5.1.3 |
| Postgres 17 + TimescaleDB | `127.0.0.1:55432` | 容器 `dtk-data-postgres-1`，库 `dtk_test`，用户 `dtk` / 密码 `dtk_test_password` |
| Redis 8 | `127.0.0.1:56379` | 容器 `dtk-data-redis-1`，无密码，AOF 打开 |
| API 服务 | `127.0.0.1:8000` | `uv run dtk serve`，健康检查 `/healthz`、就绪 `/readyz` |
| worker | 无端口 | `uv run dtk worker`，异步任务（解析/采集/回填/身份探测）都靠它执行 |
| 数据目录 | `~/.workbuddy/data/douyin-data` | 凭据、cookie、API 清单、日志、compose.yml |

## 1.1 容器用的是自己生成的 compose，不是仓库自带那份

`scripts/stack.sh` 第一次 `up` 时会写出 `$DTK_HOME/compose.yml`（compose 项目名
`dtk-data`），与仓库自带的 `docker/compose.test.yml` 有两处**故意的**不同：

1. **数据落命名卷**（`dtk-pgdata` / `dtk-redisdata`），不是 tmpfs。
   仓库那份是测试 fixture，Postgres 数据目录挂在 tmpfs 上 —— 容器一停，
   身份池、归档、API Key、管理员账号**全部消失**。真实使用必须改成命名卷。
2. `restart: unless-stopped`，Redis 开 AOF。

想彻底清空数据用 `docker compose -p dtk-data down -v`（**会删掉卷**，谨慎）。
只是想停服务就用 `stack.sh down`。

## 2. 为什么 Postgres 镜像是自己构建的

`docker/compose*.yml` 里写的是 `timescale/timescaledb-ha:pg17`（约 3.9 GB，多架构）。
本机网络下**这个镜像拿不到**：

- Docker Hub 直连不可达（`registry-1.docker.io` 超时）。
- 守护进程配的镜像站对 `timescale` 这个 org 一律拒绝：
  - `docker.nju.edu.cn` → 403
  - `docker.m.daocloud.io` → 403
  - `docker.1ms.run` → 传输中断（short read）
  - 1panel.live / hub.rat.dev / xuanyuan.me / dockerpull.org / dockerproxy.net / agsv.top / registry.dockermirror.com → 均失败

**解法：用官方 `postgres:17` + TimescaleDB 官方 deb 自建镜像，并直接打成同名标签**，
这样两个 compose 文件都不用改。

```bash
# 1) 拉基础镜像（library 镜像能过镜像站）
docker pull docker.m.daocloud.io/library/postgres:17
docker tag  docker.m.daocloud.io/library/postgres:17 postgres:17   # 实测 arm64 / PG 17.11

# 2) 取与基础镜像匹配的 deb —— 关键：镜像基于 Debian 13 (trixie)，别下 bookworm 的
#    packagecloud 可达（走 CloudFront CDN），选 arm64
curl -sL -o timescaledb-2-postgresql-17.deb \
  "https://packagecloud.io/timescale/timescaledb/debian/pool/trixie/main/t/timescaledb-2-postgresql-17/timescaledb-2-postgresql-17_2.30.2~debian13-1711_arm64.deb"
curl -sL -o timescaledb-2-loader-postgresql-17.deb \
  "https://packagecloud.io/timescale/timescaledb/debian/pool/trixie/main/t/timescaledb-2-2.30.2-postgresql-17/timescaledb-2-loader-postgresql-17_2.30.2~debian13-1711_arm64.deb"

# 3) 构建（上下文见 $DTK_REPO/.build-pg-timescale/）
docker build --platform linux/arm64 -t timescale/timescaledb-ha:pg17 .
```

镜像构建要点（`Dockerfile`）：

- 用 `dpkg -i` 而不是 `apt-get install`：依赖（`postgresql-17`、`libc6`、loader）都已就位，
  这样构建**不需要**访问 Debian 源。
- 装完把 `shared_preload_libraries = 'timescaledb'` 追加到
  `/usr/share/postgresql/*/postgresql.conf.sample`（initdb 会据此生成 postgresql.conf）。
  缺这一句 `CREATE EXTENSION timescaledb` 会报「must be loaded via shared_preload_libraries」。
- 索引里能查到每个包的 SHA256，下完务必核对。

> 索引地址规律：`https://packagecloud.io/timescale/timescaledb/debian/dists/<trixie|bookworm>/main/binary-<arm64|amd64>/Packages`
> （需要跟随 302 重定向，`curl -L`）。发布版要跟基础镜像的 Debian 代号一致，
> 版本要在 `Packages` 里挑最新的 `timescaledb-2-postgresql-17`（本机为 2.30.2）。

## 3. Python 环境与凭据

```bash
cd "$DTK_REPO"
export UV_DEFAULT_INDEX=https://pypi.tuna.tsinghua.edu.cn/simple   # 直连 PyPI 慢
uv sync --all-extras      # 生成 .venv（本机解析到 Python 3.12.14）
```

`.env`（**必须在仓库根目录运行命令**，pydantic-settings 按工作目录读 `.env`）：

```ini
DTK_SECRET_KEY=<openssl rand -base64 48>          # 少于阈值会直接拒绝启动
DTK_DATABASE_URL=postgresql+asyncpg://dtk:dtk_test_password@127.0.0.1:55432/dtk_test
DTK_REDIS_URL=redis://127.0.0.1:56379/0
DTK_BIND_HOST=127.0.0.1
DTK_BIND_PORT=8000
DTK_LOG_LEVEL=info
DTK_LOG_JSON=false        # 首次初始化时便于读 stdout 里的 setup 令牌横幅
```

`DTK_SECRET_KEY` 是**凭据加密主密钥**：身份池里的 cookie 用它加密（AAD 绑定身份 id）。
换 key 会让已入库的 cookie 全部解不开，务必随数据一起备份。

## 4. 首次初始化（只做一次）

1. 启动 API 后，**stdout 会打印一段横幅**，里面有一次性 setup 链接：
   `http://127.0.0.1:8000/setup?token=<TOKEN>`；同一令牌也写在 Redis 的 `setup:token`（TTL 24h）。
2. 用令牌建管理员：

```bash
curl -s -X POST http://127.0.0.1:8000/api/setup/init \
  -H 'Content-Type: application/json' \
  -d '{"token":"<TOKEN>","username":"admin","password":"<自定>"}'
```

3. 登录拿会话 cookie，再建一把全权限 API Key（**密钥只在创建时返回一次**）：

```bash
curl -s -c /tmp/c.txt -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' -d '{"username":"admin","password":"<自定>"}'

curl -s -b /tmp/c.txt -X POST http://127.0.0.1:8000/api/v1/admin/api-keys \
  -H 'Content-Type: application/json' \
  -d '{"name":"douyin-data","scopes":["douyin:read","tiktok:read","identity:manage",
       "archive:read","archive:export","media:read","media:write","admin"]}'
```

可用 scope：`douyin:read` `tiktok:read` `identity:manage` `archive:read`
`archive:export` `media:read` `media:write` `admin`。
请求头名是 **`X-API-Key`**。密钥与账号存到 `$DTK_HOME/credentials.env`（建议 `chmod 600`）。

## 5. 导入 / 刷新抖音登录态（身份池）

cookie 由「身份池」管理，**必须带配套 User-Agent**，否则 `report.usable=false`，
导入接口会以 400 拒绝（理由：无法据此匹配 TLS 指纹）。

**首选方式：`scripts/cookie.py` 从本机 Chrome 自动抓取（macOS）**，省掉手工
抄 cookie 的四个环节：

```bash
python3 scripts/cookie.py status            # 存档 cookie 还剩几天 + 身份池现状
python3 scripts/cookie.py refresh --dry-run # 抓取 + 服务端体检，不落盘、不导入
python3 scripts/cookie.py refresh --test    # 抓取 → 备份旧档 → 导入 → 真实探测
```

- 原理：读 Chrome 的 SQLite（`Chrome/Default/Cookies`），用钥匙串
  "Chrome Safe Storage" 的密钥做 AES-128-CBC 解密；UA 按本机 Chrome 真实
  版本拼出。实测（2026-10-15）：56 条 cookie，与手工存档里的 sessionid 逐字节一致；
  导入后真实探测 `ok=true / status=200`。
- 前提：Chrome 里处于登录状态。首次读钥匙串会弹一次授权（点「始终允许」）。
  **沙箱会挡住授权框**（表现为卡住直到超时）：在 WorkBuddy 会话里执行要放到
  沙箱外；普通终端不受影响。
- 存档写在 `$DTK_HOME/douyin_cookie.txt`（自动 `chmod 600`），旧份自动备份为
  `douyin_cookie.bak`。旧身份不用手动清理，cookie 失效后在池里自然冷却。

手工方式（其他浏览器 / 其他系统）：

```bash
# 先体检（dry_run 不写入），看 detected_format / authenticated / usable / missing_required
python3 scripts/dtk.py identity-import "$DTK_HOME/douyin_cookie.txt" --ua "<确切 UA>" --dry-run
python3 scripts/dtk.py identity-import "$DTK_HOME/douyin_cookie.txt" --ua "<确切 UA>"
```

- UA 从导出 cookie 的浏览器取（DevTools → Network → 任一 douyin.com 请求 → Request Headers）。
- 导入时会从 UA 推断浏览器大版本（实测 `browser_family=chrome`、`browser_major=155`），
  transport 侧会据此选 TLS 模拟档位（实测请求日志里出现 `emulation=Chrome153`）。
- 建议配 `language=zh-CN`、`timezone=Asia/Shanghai`，与出口 IP 一致。
- cookie 里若有结构化字段（base64/URL 编码的 JSON），导入前可解码自检，
  能发现手工誊抄导致的错字。**cookie 等同于账号密码，建议用小号。**
- 登录态有有效期：`sid_guard` 里写明过期时间（本次为 2026-12-14）。过期后重新导入。

## 6. 验证「服务真的能用」

```bash
curl -s http://127.0.0.1:8000/readyz          # {"status":"ok","components":{"postgres":{"ok":true},"redis":{"ok":true}}}
python3 scripts/dtk.py apilist                # 路径模板 89 条 / 操作 104 个（含搜索补丁）
curl -s -H "X-API-Key: $KEY" http://127.0.0.1:8000/openapi.json | head -c 200
```

身份的**真实**探针（会发一次真请求，最能说明 cookie 是否有效）：

```bash
python3 scripts/dtk.py identity-test <identity_id>
# 期望：state=done，data.ok=true，data.status=200
# 实测：endpoint=douyin.content_detail status=200 bytes≈131KB latency≈1.4s
```

若 `readyz` 正常、但任务一直 `running`，是 **worker 没起**。

## 7. 排障对照

| 现象 | 原因 | 处理 |
|---|---|---|
| `pull access denied / 403 Forbidden` 拉 timescaledb 镜像 | 镜像站屏蔽该 org | 走第 2 节自建镜像 |
| 迁移报 `timescaledb extension is not available` | 扩展未预加载或 deb 没装 | 确认 `SHOW shared_preload_libraries;` 为 `timescaledb` |
| 迁移报 `pg_trgm` 相关错误 | 缺 contrib | 官方 `postgres:17` 自带 pg_trgm 1.6，正常 |
| `dtk backend` 启动报 secret key 太短 | `DTK_SECRET_KEY` 未设或过短 | 用 `openssl rand -base64 48` |
| 导入 cookie 返回 400 `cannot be used as an identity` | 缺 UA / 缺必需 cookie | 看响应里的 `missing_required`，补齐 UA |
| 任务永远 `running` | worker 未启动 | 跑 `uv run dtk worker` |
| 控制台页面 404 | 前端未构建（不影响 REST） | 需要界面时按官方文档构建 `web/`（Node 22） |
| `docker ps` 报连不上 daemon | Docker Desktop 没启动 | `open -a Docker`，等 `docker info` 成功 |
| API 连 `/system/status` 都报 500 | 多半是 Docker Desktop 退出了（socket 消失，Postgres/Redis 一起断） | `open -a Docker` + `docker compose -f $DTK_HOME/compose.yml up -d`；容器会随 `restart: unless-stopped` 自动回来，API/worker 自愈（实测 1 分钟内） |
| `cookie.py refresh` 卡住、报「读取钥匙串超时」 | 钥匙串授权弹窗被沙箱挡住 | 在普通终端或沙箱外执行；首次授权点「始终允许」 |
| 本地 API 报 502 / Connection refused，但服务确实起着 | 环境里有全局 `HTTP_PROXY`，代理连不到回环地址 | 客户端已自动对 `127.0.0.1` 绕开代理；手写 curl 时加 `--noproxy '*'` |
| 采集任务 `state=failed`，日志里 `AttributeError` | 打的补丁与仓库版本不匹配 | 按 `patches/README.md` 重新打补丁并重启 API+worker |
| `search/posts` 返回 404 | 补丁没打（该能力不是上游自带） | 同上 |
| 搜索只回一页就没下文 | 第二页被平台要求校验（`verify_check`），非本项目能绕过 | 见 7.2；服务端现已如实报成风控，客户端保留第一页 |
| pytest 报几十个 `mkdir` 被拒的 ERROR | 沙箱不允许 pytest 写系统临时目录（`TMPDIR=/tmp` 也会被解析成 `/private/tmp`） | 加 `--basetemp=.pytest-tmp`（仓库内可写目录） |

## 7.1 关键词搜索是本项目的补丁

抖音的 `general/search/single` 接口上游没有接线，本机给它打了补丁才通
（14 个文件，见 `patches/README.md`）。**仓库重装/升级后第一件事是确认补丁还在**：

```bash
curl -s -o /dev/null -w '%{http_code}\n' --noproxy '*' \
  -H "X-API-Key: $DTK_API_KEY" \
  "http://127.0.0.1:8000/api/v1/douyin/search/posts?keyword=test&count=20&wait=30"
# 200 = 补丁在；404 = 补丁没了
```

另外记住搜索接口的两个数：**每页上限 30**（超过会被路由拒成 400，
上游则会给空页）、**每页至少按 20 条要**（要 1 条会拿到空页）。

## 7.2 搜索的第二页：平台要求校验，只能取一页

实测记录（2026-10-15，关键词「操盘手」）：

```
第 1 页  ->  200，19 条作品，cursor=30，has_more=true
第 2 页  ->  上游响应 data=[]、has_more=0，但 search_nil_info.search_nil_type
             = "verify_check"  ->  服务端判成 UPSTREAM_RISK_CONTROL（502）
等待 180 秒后再打第 2 页  ->  仍然被拦
```

结论：**这不是临时限流，等多久都一样**；要过它需要在浏览器里完成人工校验
（本机未部署指纹浏览器侧车）。所以关键词搜索的实际产出就是**第一页，约
15–20 条**。想要某个话题的更多作品，改走「用关键词找到作者 → 采作者主页」，
主页翻页正常。

补丁之前 `detect_risk_control()` 不认这个信号，第二页会被当成"列表到底了"，
搜索在第一页静默截断 —— 现在它返回 `search_nil:verify_check`，身份进冷却，
客户端保留已采数据并明确报告。

## 8. 待确认 / 已知近似项

- **关键词搜索只到第一页**（见 7.2）：不是缺陷而是平台要求校验，绕过它需要
  浏览器侧的人工校验，本机没有那个侧车。功能上表现为"关键词输入最多约 20 条"，
  想要更多走作者主页。
- **导入身份用的 User-Agent**：首次导入用的是近似串（macOS + Chrome 155 标准串）；
  之后 `cookie.py refresh` 改为按本机 Chrome 的真实版本生成（当前
  `Chrome/154.0.8037.95`），已实测可用。若出现签名类失败，先核对 UA 与浏览器版本。
- `emulation=Chrome153` 与 UA 里的 155 不完全一致，属 transport 侧档位映射，实测不影响。
- 侧车组件（`downloader` 媒体下载器、`browser`/CloakBrowser 指纹浏览器）本次**未部署**：
  `DTK_DOWNLOADER_URL` 与 `DTK_BROWSER_RPC_URL` 留空。影响：
  - `POST /api/v1/downloads` 需要下载器侧车，未部署时该能力不可用（归档与内容读取不受影响）；
  - 自动 mint 访客身份不可用，身份池只能靠手动导入。
