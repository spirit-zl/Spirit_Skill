# dtk 上游源码补丁

技能依赖的 dtk 实例（`DTK_REPO` 指向的仓库，默认
`/Users/spirit/WorkBuddy/spirit_WorkBuddy/douyin_api`）本身**不带关键词搜索**。
这里留一份补丁，重装仓库后照着打，才能恢复 `douyin.search_posts` 这条链路。

## dtk-search-posts.patch

给 dtk v5.1.3 接入抖音关键词搜索接口
`GET /aweme/v1/web/general/search/single/`，对外的路由是：

```
GET /api/v1/{platform}/search/posts?keyword=...&cursor=...&count=...&wait=...
```

改到的 15 个文件（源码 13 + 测试 2），按仓库既有的分层各改一处：

| 文件 | 改了什么 |
| --- | --- |
| `platforms/douyin/params.py` | 新增 `search_posts_params()` 与 `SEARCH_CHANNEL_GENERAL` |
| `platforms/douyin/endpoints.py` | 新增稳定键 `SEARCH_POSTS` 与对应 `EndpointSpec`（`risk_weight=2.5`） |
| `platforms/douyin/parser.py` | 新增 `parse_search_posts()`（混合流里按 `aweme_info` 选作品）；`detect_risk_control()` 增加搜索的 `verify_check` 分支 |
| `platforms/base.py` | 适配器协议加 `parse_search_posts` |
| `platforms/douyin/adapter.py` | 委托给 parser |
| `platforms/tiktok/adapter.py` | 抛 `UnsupportedContent`（TikTok 搜索未实现） |
| `platforms/paging.py` | 新增 `MAX_SEARCH_PAGE_SIZE`（抖音 30）与 `max_search_page_size()` |
| `worker/registry.py` | `Capability.SEARCH_POSTS`、`KEYWORD` 常量、参数映射、缓存 TTL、`parse()` 分派 |
| `api/routes/operations.py` | `Operation.SEARCH_POSTS` |
| `api/routes/content.py` | `KEYWORD_QUERY` 与 `/search/posts` 路由 |
| `scheduler/policies.py` | `douyin.search_posts` 的限流与风险权重 |
| `i18n/locales/{en,zh}.json` | `openapi.op.search_posts` 与 `openapi.param.keyword` 词条 |
| `tests/unit/test_paging.py` | 抖音搜索上限的断言（仓库自带测试风格） |
| `tests/replay/test_douyin_parser.py` | 搜索风控信号的两条断言（内联 payload，见下） |

### 怎么打

```bash
cd "$DTK_REPO"
git apply --check /Users/spirit/.workbuddy/skills/content-collect/references/patches/dtk-search-posts.patch
git apply /Users/spirit/.workbuddy/skills/content-collect/references/patches/dtk-search-posts.patch
```

打完后重启 API 与 worker（`stack.sh` 里没有 restart 子命令，用 `down` 再 `up`）。

### 怎么验

```bash
cd "$DTK_REPO" && TMPDIR=/tmp .venv/bin/python -m pytest tests/unit tests/replay -q --basetemp=.pytest-tmp
```

（`--basetemp` 的必要性见文末「关于 pytest 的临时目录」。跑完 `rm -rf .pytest-tmp`。）

仓库自带的三条测试专门盯着这类改动，改动不合规会直接红：

- `tests/unit/test_policy_coverage.py`：每个注册端点都要有显式限流策略，
  不能静默继承 `DEFAULT_POLICY`。
- `tests/unit/test_openapi_completeness.py`：每个操作和参数都要有描述，
  且中文文档里必须真的是中文。
- `tests/unit/test_repo_hygiene.py`：Python 源码里不许出现 CJK 字符。

### 搜索的测试怎么加（以及为什么不进 fixture 名单）

仓库的 `tests/replay/` 用一套**双平台共享**的 fixture 名单
（`tests/replay/support.py` 的 `FIXTURE_NAMES`），并由
`test_platform_contract.py` 断言「两个平台目录下的 fixture 文件集合完全一致」。
搜索只有抖音有，往这套名单里加一个只有抖音存在的 fixture，就要给这份
跨平台契约开一个洞 —— 那是比"少一条测试"更糟的事。

所以搜索的两条断言写成**内联 payload**，落在 `test_douyin_parser.py` 已有的
风控测试旁边（那里本来就区分"页面形状变化"与"页面被扣下"）：

- `test_a_withheld_search_page_is_not_the_end_of_the_list`：`verify_check` 的响应
  必须被判成风控，不能当最后一页。
- `test_a_search_that_found_nothing_is_still_an_honest_empty_page`：真没结果的
  空页仍然是正常空页 —— 守卫必须窄。

其余行为在真实环境端到端验证（见 `references/setup-notes.md`）。

### 搜索风控：`verify_check`

搜索的第二页会被平台拦下，响应**长得和"没有更多"一模一样**：

```
status_code: 0 | has_more: 0 | data: []
search_nil_info: {"search_nil_type": "verify_check",
                  "is_load_more": "is_load_more",
                  "search_nil_item": "verify_check", "text_type": 9}
```

关键字段是 `search_nil_type`/`search_nil_item` = `verify_check`：这一页被扣下
等待校验，不是列表到底了。仓库原有的 `detect_risk_control()` 只认
`status_code` 命中风险码、`verify_center_decision_conf`、`captcha`/`verify_page`
和空的 `aweme_detail`/`user`，**不认这个信号** —— 于是搜索会在第一页静默截断，
把一段短列表当成完整答案交出去，而真正被拦的身份连冷却都没有。

补丁给它加了一条分支（`search_nil:verify_check`），于是：

- `fetch` 层把它记成 `RISK_CONTROL`，身份进入冷却；
- 解析器抛 `UpstreamRiskControl`，不会再伪装成"最后一页"；
- 客户端保留已采到的第一页并明确说明（实测：等待 3 分钟后再打第二页**仍然
  被拦**，所以这不是临时限流，跳过它需要浏览器侧的人工校验 —— 本机未部署）。

守卫写得很窄：只有 `verify_check` 命中，正常的空结果不受影响（测试里钉住了
这一条）。

### 关于 `count`

搜索返回的是一个**混合流**：抖音按 `count` 返回这么多条**条目**，条目里
混着作品、账号、直播间，只有带 `aweme_info` 的才是作品。所以
`items` 的长度通常小于 `count`，这是正常的，不是丢数据 —— 实测
`count=20` 时 20 条条目里有 18 条作品。调用方按 `items` 实际长度取值。

**每页上限是 30，不是抖音其它列表的 50。** 实测：

```
count=30  ->  完整一页，has_more=true
count=31  ->  data 为空，has_more=false
```

这个"拒绝"长得和"没结果"一模一样 —— 要多了会被读成"这个关键词什么都没搜到"。
所以上限写进了 `platforms/paging.py`，路由层直接把 `count>30` 拒成 400
（`INVALID_PARAM`，details 里带 `maximum: 30`），**拒绝而不是截短**，
和 `wait` 的处理方式一致。`count` 太小同样会拿到空页（实测 `count=1` 时
第二页一条都不给），所以客户端每页至少按 20 条要。

### 关于 pytest 的临时目录

这台机器的沙箱不允许 pytest 往系统临时目录里建 `pytest-of-*`：
`TMPDIR=/tmp` 会被解析成 `/private/tmp`，照样被拒，于是用 tmp_path 的用例
成片 ERROR。显式给一个可写的 basetemp 就好：

```bash
cd "$DTK_REPO" && TMPDIR=/tmp .venv/bin/python -m pytest tests/unit tests/replay -q --basetemp=.pytest-tmp
```

`TMPDIR=/tmp` 仍然要带着（纯 Python 的 `tempfile` 用它），`--basetemp` 管 pytest
自己那一份。`.pytest-tmp` 落在仓库里，别提交。这不是代码问题：
全绿是 **2583 passed**。

如果命令确实被关在沙箱里（而不是放行），还会剩下 12 个
`test_worker_ops_backup.py` 的失败 —— 那些用例要往临时目录里写归档文件，
沙箱里写不进去。放行后即全绿，与本次改动无关。
