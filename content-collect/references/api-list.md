# dtk API 清单（Douyin_TikTok_Download_API v5.1.3 自建实例）

- 实例地址：http://127.0.0.1:8000
- OpenAPI：3.1.0　标题：dtk　版本：5.1.3
- 路径模板：89 条　操作：104 个
- 原始文件：openapi.json（同目录）

## 一览

| 分组 | 路径数 |
|---|---|
| setup — 首次初始化 | 2 |
| admin — 身份池 / 代理 / 用户 / Key / 设置 / 维护 | 36 |
| archive — 内容归档（采集入库） | 11 |
| auth — 登录与会话 | 7 |
| downloads — 媒体下载与存储 | 7 |
| ios — 快捷指令 | 1 |
| other | 1 |
| system — 实例状态 | 1 |
| tasks — 异步任务 | 3 |
| tools — 链接/签名/批量工具 | 5 |
| content — 平台内容读取（抖音/TikTok） | 15 |

## setup — 首次初始化

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/setup/init` | Create the first administrator account |
| GET | `/api/setup/status` | Whether this instance has an administrator yet |

## admin — 身份池 / 代理 / 用户 / Key / 设置 / 维护

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/admin/api-keys` | List API keys |
| POST | `/api/v1/admin/api-keys` | Create an API key |
| DELETE | `/api/v1/admin/api-keys/{key_id}` | Revoke an API key |
| GET | `/api/v1/admin/audit` | Read the audit trail |
| GET | `/api/v1/admin/backup` | List backup archives |
| POST | `/api/v1/admin/backup` | Create a backup archive |
| POST | `/api/v1/admin/backup/restore` | Restore a backup archive |
| GET | `/api/v1/admin/demo` | Show the demo account |
| POST | `/api/v1/admin/demo/rotate` | Reissue the demo credentials |
| POST | `/api/v1/admin/diagnose` | Run the six-step self check |
| GET | `/api/v1/admin/endpoints/access` | Which endpoints are served without a key |
| GET | `/api/v1/admin/endpoints/health` | Per-endpoint health and circuit state |
| GET | `/api/v1/admin/identities` | List identities with state and health |
| POST | `/api/v1/admin/identities/export` | Export identities as a file |
| POST | `/api/v1/admin/identities/import` | Import a logged-in cookie jar |
| POST | `/api/v1/admin/identities/import/bundle` | Import an export document |
| POST | `/api/v1/admin/identities/mint` | Mint guest identities |
| GET | `/api/v1/admin/identities/pool` | Pool level against its low-water mark |
| DELETE | `/api/v1/admin/identities/{identity_id}` | Retire an identity |
| GET | `/api/v1/admin/identities/{identity_id}/cookies` | What one identity's jar holds |
| GET | `/api/v1/admin/identities/{identity_id}/cookies/reveal` | Read one identity's cookies in full |
| POST | `/api/v1/admin/identities/{identity_id}/reset` | Return an identity to rotation |
| POST | `/api/v1/admin/identities/{identity_id}/session` | Check whether an identity's login is still valid |
| POST | `/api/v1/admin/identities/{identity_id}/test` | Probe one identity |
| GET | `/api/v1/admin/logs/requests` | Read the request log |
| GET | `/api/v1/admin/metrics/timeseries` | Request volume and outcomes over time |
| POST | `/api/v1/admin/notifications/test` | Send a test alert |
| GET | `/api/v1/admin/proxies` | List proxies |
| POST | `/api/v1/admin/proxies` | Add one proxy |
| POST | `/api/v1/admin/proxies/import` | Bulk import a pasted proxy list |
| DELETE | `/api/v1/admin/proxies/{proxy_id}` | Delete a proxy and retire what used it |
| PUT | `/api/v1/admin/proxies/{proxy_id}` | Update one proxy |
| POST | `/api/v1/admin/proxies/{proxy_id}/test` | Probe one proxy |
| GET | `/api/v1/admin/settings` | Read every runtime setting |
| DELETE | `/api/v1/admin/settings/{key}` | Reset one setting to its inherited value |
| PUT | `/api/v1/admin/settings/{key}` | Change one runtime setting |
| GET | `/api/v1/admin/users` | List console accounts |
| POST | `/api/v1/admin/users` | Create a console account |
| DELETE | `/api/v1/admin/users/{user_id}` | Delete a console account |
| PUT | `/api/v1/admin/users/{user_id}` | Change a role or reset a password |
| GET | `/api/v1/admin/watchlist` | List watched targets |
| POST | `/api/v1/admin/watchlist` | Watch a target |
| POST | `/api/v1/admin/watchlist/pause` | Pause every watched target |
| DELETE | `/api/v1/admin/watchlist/{entry_id}` | Stop watching a target |
| PATCH | `/api/v1/admin/watchlist/{entry_id}` | Change or pause a watched target |

## archive — 内容归档（采集入库）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/archive` | Search the archive |
| POST | `/api/v1/archive/backfill` | Archive an author's history |
| GET | `/api/v1/archive/collections` | List collections |
| POST | `/api/v1/archive/collections` | Create a collection |
| DELETE | `/api/v1/archive/collections/{collection_id}` | Delete a collection |
| PATCH | `/api/v1/archive/collections/{collection_id}` | Rename a collection |
| POST | `/api/v1/archive/collections/{collection_id}/items` | Add posts to a collection |
| POST | `/api/v1/archive/collections/{collection_id}/items/remove` | Take posts out of a collection |
| POST | `/api/v1/archive/delete` | Delete archived posts |
| GET | `/api/v1/archive/export` | Export the archive |
| POST | `/api/v1/archive/recheck` | Re-check what still exists |
| GET | `/api/v1/archive/stats` | Archive totals |
| GET | `/api/v1/archive/{platform}/{content_id}` | One archived post |

## auth — 登录与会话

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/auth/demo` | The demo credentials, for the login page |
| POST | `/api/v1/auth/login` | Exchange a password for a session cookie |
| POST | `/api/v1/auth/logout` | Revoke the current session |
| GET | `/api/v1/auth/me` | The authenticated principal |
| POST | `/api/v1/auth/password` | Change your own password |
| DELETE | `/api/v1/auth/sessions` | Log out every other device |
| GET | `/api/v1/auth/sessions` | List your live sessions |
| DELETE | `/api/v1/auth/sessions/{session_ref}` | Revoke one of your sessions |

## downloads — 媒体下载与存储

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/downloads` | List downloads |
| POST | `/api/v1/downloads` | Store a post's media |
| POST | `/api/v1/downloads/deduplicate` | Remove duplicate downloads |
| POST | `/api/v1/downloads/retry` | Try a download again |
| GET | `/api/v1/downloads/storage` | Media storage usage |
| DELETE | `/api/v1/downloads/{download_id}` | Cancel a download |
| GET | `/api/v1/downloads/{download_id}` | One download |
| GET | `/api/v1/downloads/{download_id}/files/{name}` | Download one stored file |
| POST | `/api/v1/downloads/{download_id}/pin` | Pin or unpin |

## ios — 快捷指令

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/ios/shortcut` | iOS Shortcut release information |

## other

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/parse` | Parse any supported link |

## system — 实例状态

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/system/status` | Instance status |

## tasks — 异步任务

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tasks/batch` | Submit many links at once |
| DELETE | `/api/v1/tasks/{task_id}` | Cancel a task |
| GET | `/api/v1/tasks/{task_id}` | Fetch a task |
| GET | `/api/v1/tasks/{task_id}/events` | Stream a task's progress |

## tools — 链接/签名/批量工具

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/tools/decode` | Read a signature parameter back |
| POST | `/api/v1/tools/identity` | Mint a guest identity and return it |
| POST | `/api/v1/tools/parse-batch` | Identify many links and ids at once |
| GET | `/api/v1/tools/parse-url` | Identify a share link |
| POST | `/api/v1/tools/sign` | Sign a platform API URL |

## content — 平台内容读取（抖音/TikTok）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/{platform}/collection` | One saved folder's own name, cover and size |
| GET | `/api/v1/{platform}/collection/posts` | Posts inside one of an author's saved folders |
| GET | `/api/v1/{platform}/mix/posts` | Posts inside a mix or playlist |
| GET | `/api/v1/{platform}/search/posts` | Search posts by keyword（**本项目新增**，见 patches/） |
| GET | `/api/v1/{platform}/user` | Author profile |
| GET | `/api/v1/{platform}/user/bookmarks` | Posts an author has saved |
| GET | `/api/v1/{platform}/user/collections` | The folders an author has organized bookmarked posts into |
| GET | `/api/v1/{platform}/user/followers` | Accounts that follow an author |
| GET | `/api/v1/{platform}/user/following` | Accounts an author follows |
| GET | `/api/v1/{platform}/user/likes` | Posts an author has liked |
| GET | `/api/v1/{platform}/user/posts` | Author post list |
| GET | `/api/v1/{platform}/user/reposts` | Other people's posts an author reposted |
| GET | `/api/v1/{platform}/video` | One post, video or image album |
| GET | `/api/v1/{platform}/video/comments` | Top level comments on a post |
| GET | `/api/v1/{platform}/video/comments/replies` | Replies under one comment |
