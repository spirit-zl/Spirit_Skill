#!/usr/bin/env python3
"""把采集结果写进飞书多维表格。

消费 ``xhs.mjs --json`` 的产出（13 字段与抖音那条链路是同一份契约，所以这个
脚本与抖音引擎共用），把每一行按**列名**填进去。同一条作品
（按「作品链接」判定）只更新、不新增。目标有两种给法：

* **给一个文件夹**（默认，也是历史行为）：在文件夹下找到（或新建）名为
  ``table_name`` 的多维表格；
* **给一个现成的多维表格**（``--base``）：直接写进去。用户说「更新这个表」
  并贴了一条 ``/base/<token>`` 链接时用这种 —— 此时**绝不新建**表格，
  表里没有对应的工作表就报错退出。

三条设计约束，都是用户明确提过的：

1. **按列名填充。** 列在表里的前后顺序可以随便调，写入只认列名，所以取字段
   用的是名字而不是位置。表里已存在的列一律沿用它的类型，我们不按自己的
   口味去改别人的表。
2. **只更新，不新增。** 先按「作品链接」把表里已有的记录读成一张
   ``作品链接 -> record_id`` 的表，命中就批量更新，没命中才新建。
3. **列名不变。** 13 个列名是契约，缺哪列补哪列；已有的列不动。
   表里还有 13 字段之外列（例如「来源」）时，用 ``--set 列名=值`` 补常量。

依赖 ``lark-cli``（已登录、且当前身份对该文件夹/多维表格有编辑权限）。用法：

    node xhs.mjs "<主页链接>" --out rows.json
    python3 feishu_bitable.py --rows rows.json                    # 用「上次用过的」文件夹
    python3 feishu_bitable.py --rows rows.json --folder <token>   # 本次改用别的文件夹
    python3 feishu_bitable.py --rows rows.json --base <base链接> --set 来源=小红书
    python3 feishu_bitable.py --show-folder                       # 看当前默认文件夹地址
    python3 feishu_bitable.py --set-folder <链接或token>           # 换默认文件夹

文件夹的约定（用户定下的）：默认文件夹就是 ``credentials.env`` 里的
``FEISHU_FOLDER_TOKEN`` —— 也就是**上一次用过的**那个。落表前先把它的地址
发给用户确认；用户回一个新地址就用 ``--set-folder`` 换掉（从此它就是新的
默认），没有异议就用默认的写。每次写入都会打印实际使用的文件夹地址，便于复核。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
from datetime import datetime
from pathlib import Path

XHS_HOME = Path(os.environ.get("XHS_HOME", "~/.workbuddy/data/xiaohongshu-cdp")).expanduser()

#: 飞书的文件夹 token 存在哪：先看本技能自己的数据目录，再退回抖音那边的
#: ``credentials.env``。两处都是「用户上次用过的那个文件夹」，同一个人同一个
#: 偏好，没必要让他在第二个技能里再配一遍；本技能自己写的优先。
CRED_CANDIDATES = (
    Path(os.environ["XHS_FEISHU_ENV"]).expanduser() if os.environ.get("XHS_FEISHU_ENV") else None,
    XHS_HOME / "credentials.env",
    Path("~/.workbuddy/data/douyin-data/credentials.env").expanduser(),
)
CRED_FILE = next((p for p in CRED_CANDIDATES if p and p.exists()), CRED_CANDIDATES[1])

#: 文件夹地址的展示形态（token 取自链接 /drive/folder/<token> 的那一段）。
FOLDER_URL_TEMPLATE = "https://my.feishu.cn/drive/folder/{token}"

#: 单批上限，服务端定的。同一个表必须串行写，并行会撞 1254291 并发冲突。
BATCH = 200

#: 首表的表名。多维表格（Base）本身用 table_name 命名，里面这张工作表用这个名字。
SHEET_NAME = "作品列表"

#: 身份与作品身份列。
IDENTITY = "作品链接"

#: 超链接列读回来是 ``[显示文本](目标地址)``。
_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)]*)\)")

_LARK = os.environ.get("LARK_CLI", "lark-cli")


class FeishuError(RuntimeError):
    """调用飞书失败，原因已经足够清楚，不需要堆栈。"""


# --------------------------------------------------------------------------- #
# 列定义
# --------------------------------------------------------------------------- #
#: 13 列的默认类型。只在**表里还没有这一列**时用来建列；已经存在的列沿用原样，
#: 所以用户自己把「发布日期」改成文本、或把「作品标签」改成多选，都不会被改回去。
COLUMNS: tuple[dict, ...] = (
    {"name": "账号名称", "type": "text"},
    {"name": "账号主页链接", "type": "text", "style": {"type": "url"}},
    {"name": "搜索关键词", "type": "text"},
    {"name": "发布日期", "type": "datetime", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"name": "作品标题", "type": "text"},
    {"name": "作品标签", "type": "text"},
    {"name": "作品文案", "type": "text"},
    {"name": "点赞", "type": "number", "style": {"type": "plain", "precision": 0,
                                              "thousands_separator": True}},
    {"name": "评论", "type": "number", "style": {"type": "plain", "precision": 0,
                                              "thousands_separator": True}},
    {"name": "收藏", "type": "number", "style": {"type": "plain", "precision": 0,
                                              "thousands_separator": True}},
    {"name": "分享", "type": "number", "style": {"type": "plain", "precision": 0,
                                              "thousands_separator": True}},
    {"name": "作品链接", "type": "text", "style": {"type": "url"}},
    # 视频直链是带签名的临时地址，又长又会过期。它是**数据**，不是给人点的
    # 入口，所以留纯文本；要给人的入口是「作品链接」。
    {"name": "视频链接", "type": "text"},
)


# --------------------------------------------------------------------------- #
# lark-cli
# --------------------------------------------------------------------------- #
def lark(*args: str, timeout: int = 300) -> dict:
    """跑一条 lark-cli 命令，返回解析后的 ``data``。

    加了 ``--as user``：写用户云空间、建多维表格都必须用用户身份，应用身份
    没有这些权限。
    """
    cmd = [_LARK, *args, "--as", "user"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FeishuError(
            f"找不到 {_LARK}。先按 lark-shared 技能装好并登录 lark-cli，"
            "或用 LARK_CLI=<路径> 指定。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise FeishuError(f"lark-cli 超时：{' '.join(cmd[:3])}…") from exc

    raw = (proc.stdout or "").strip()
    if not raw:
        raise FeishuError(
            f"lark-cli 没有输出（退出码 {proc.returncode}）：{' '.join(cmd[:3])}…\n"
            f"{(proc.stderr or '').strip()[:400]}"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise FeishuError(f"lark-cli 输出不是 JSON：{raw[:400]}") from exc
    if isinstance(payload, dict) and payload.get("ok") is False:
        raise FeishuError(f"飞书拒绝了这次调用：{json.dumps(payload, ensure_ascii=False)[:600]}")
    if proc.returncode != 0:
        raise FeishuError(f"lark-cli 退出码 {proc.returncode}：{raw[:400]}")
    return payload.get("data", payload) if isinstance(payload, dict) else {"data": payload}


def folder_url(token: str) -> str:
    return FOLDER_URL_TEMPLATE.format(token=token)


def token_from(value: str) -> str:
    """从文件夹链接或裸 token 里取出 token。"""
    value = value.strip()
    match = re.search(r"/folder/([A-Za-z0-9]+)", value)
    return match.group(1) if match else value


#: 多维表格地址的展示形态。用户给的链接常带 ``?table=tblxxx&view=vewxxx``，
#: 那两个参数本身就是有用的信息（他指着哪张表），别当噪音丢掉。
BASE_URL_TEMPLATE = "https://my.feishu.cn/base/{token}"


def split_base(value: str) -> tuple[str, str | None]:
    """从多维表格链接（或裸 token）里取出 ``(base_token, table_id 或 None)``。

    接受这几种写法，都是用户手上真会有的形态：

    * ``https://xxx.feishu.cn/base/AGdBbPfUYa6ZxysQz9bc8994nUb``
    * ``https://xxx.feishu.cn/base/AGdB...?table=tblxxx&view=vewxxx``
    * ``AGdBbPfUYa6ZxysQz9bc8994nUb``

    链接里带 ``table=`` 时，用户其实已经指着某一张表了 —— 顺带用它，
    免得再按名字猜一次。
    """
    value = value.strip()
    token = value
    match = re.search(r"/base/([A-Za-z0-9]+)", value)
    if match:
        token = match.group(1)
    table = None
    query = urllib.parse.urlsplit(value).query if "?" in value else value
    for part in query.split("&"):
        if part.startswith("table="):
            table = part.split("=", 1)[1] or None
    return token, table


def base_url(token: str) -> str:
    return BASE_URL_TEMPLATE.format(token=token)


def set_default_folder(value: str) -> str:
    """换默认文件夹：写进**本技能自己**的 credentials.env（= 下次的「上次用过的」）。

    为什么写自己这份而不是回退到抖音那份：读的时候共享（同一个人同一个偏好），
    写的时候各管各的 —— 改别人的配置文件是那种「今天没事、半年后莫名其妙」的操作。
    本技能这份一旦存在，就会排在候选第一位，从此以它为准。
    """
    token = token_from(value)
    if not token:
        raise FeishuError("没有从参数里解析出文件夹 token")
    target = XHS_HOME / "credentials.env"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    if target.exists():
        lines = target.read_text(encoding="utf-8").splitlines()
    replaced = False
    for index, line in enumerate(lines):
        if line.strip().startswith("FEISHU_FOLDER_TOKEN="):
            lines[index] = f"FEISHU_FOLDER_TOKEN={token}"
            replaced = True
    if not replaced:
        lines.append(f"FEISHU_FOLDER_TOKEN={token}")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return token


def load_folder(explicit: str | None) -> str:
    """目标文件夹 token：命令行 > credentials.env（上次用过的）> 报错。

    「用户给定的文件夹」的规则是用户定的：没给新的就继续用**上次用过的**
    （credentials.env 里记的）；给了新的就用新的、并用 ``--set-folder`` 记下来。
    两处都没有时宁可直接失败，也不猜一个位置往人家云空间里写东西。
    """
    if explicit:
        return token_from(explicit)
    if CRED_FILE.exists():
        for line in CRED_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("FEISHU_FOLDER_TOKEN="):
                value = line.split("=", 1)[1].strip()
                if value:
                    return value
    raise FeishuError(
        "还没有目标文件夹。请向用户索要飞书文件夹链接（形如 "
        "https://xxx.feishu.cn/drive/folder/<token>），然后：\n"
        "  1) 本次用 --folder <链接或token> 指定；或\n"
        f"  2) 用 --set-folder <链接> 记进 {CRED_FILE}，之后它就是默认（上次用过的）。"
    )


# --------------------------------------------------------------------------- #
# Base / Table
# --------------------------------------------------------------------------- #
def find_base(folder_token: str, name: str) -> str | None:
    """文件夹下同名多维表格的 token；没有则 None。

    直接列文件夹的清单，而不是全局搜标题：同名表格在别处可能有，写错了地方
    比找不到更糟。
    """
    data = lark("drive", "files", "list", "--folder-token", folder_token)
    for item in data.get("files") or []:
        if item.get("name") == name and item.get("type") == "bitable":
            return item.get("token")
    return None


def create_base(folder_token: str, name: str) -> tuple[str, str | None]:
    """在文件夹下新建多维表格（含首表与 13 列），返回 ``(base_token, url)``。"""
    data = lark(
        "base", "+base-create",
        "--name", name,
        "--folder-token", folder_token,
        "--table-name", SHEET_NAME,
        "--fields", json.dumps(list(COLUMNS), ensure_ascii=False),
        "--time-zone", "Asia/Shanghai",
    )
    token = data.get("base_token") or (data.get("base") or {}).get("base_token")
    if not token:
        raise FeishuError(f"建表成功了但没拿到 base_token：{json.dumps(data, ensure_ascii=False)[:300]}")
    return token, data.get("url")


def _id_of(entity: dict) -> str | None:
    """表/字段的 ID。

    lark-cli 在两处用了不同拼写（``+table-list`` 给 ``id``、``+field-list``
    给 ``id``，而写入侧参数叫 ``table_id`` / ``field_id``），版本之间也可能
    挪窝，所以两种都认。
    """
    for key in ("id", "table_id", "field_id"):
        value = entity.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def table_list(base_token: str) -> list[dict]:
    data = lark("base", "+table-list", "--base-token", base_token)
    tables = data.get("items") or data.get("tables") or []
    return [t for t in tables if isinstance(t, dict) and _id_of(t)]


def first_table(base_token: str) -> str:
    tables = table_list(base_token)
    if not tables:
        raise FeishuError(f"这个多维表格里没有工作表：{base_token}")
    for table in tables:
        if table.get("name") == SHEET_NAME:
            return _id_of(table)
    return _id_of(tables[0])


def pick_table(base_token: str, prefer: str | None = None) -> str:
    """在已有的多维表格里选一张工作表。

    ``prefer`` 可以是工作表 ID，也可以是名字（用户复述表名时说的是名字）。
    指名要了却没有，就报错并列出实际有哪些 —— 这个场景是「用户指着自己的表
    说更新这里」，猜一张写进去比写失败严重得多。
    """
    tables = table_list(base_token)
    if not tables:
        raise FeishuError(f"这个多维表格里没有工作表：{base_token}")
    if prefer:
        for table in tables:
            if prefer in (_id_of(table), table.get("name")):
                return _id_of(table)
        names = "、".join(str(t.get("name")) for t in tables)
        raise FeishuError(f"多维表格里没有名为「{prefer}」的工作表。实际有：{names}")
    for table in tables:
        if table.get("name") == SHEET_NAME:
            return _id_of(table)
    return _id_of(tables[0])


def fields_by_name(base_token: str, table_id: str) -> dict[str, dict]:
    data = lark("base", "+field-list", "--base-token", base_token, "--table-id", table_id)
    items = data.get("items") or data.get("fields") or []
    out: dict[str, dict] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        name, field_id = item.get("name"), _id_of(item)
        if name and field_id:
            out[name] = {**item, "field_id": field_id}
    return out


def ensure_columns(base_token: str, table_id: str, have: dict[str, dict]) -> list[str]:
    """缺哪列补哪列。已有的列不碰 —— 用户可能已经改过它的类型。

    返回新建的列名。一次 ``+field-create`` 传一个数组：按顺序创建，遇到第一个
    失败就停，所以一批比一条条试更快也更好读日志。
    """
    missing = [column for column in COLUMNS if column["name"] not in have]
    if not missing:
        return []
    lark(
        "base", "+field-create",
        "--base-token", base_token, "--table-id", table_id,
        "--json", json.dumps(missing, ensure_ascii=False),
    )
    return [column["name"] for column in missing]


# --------------------------------------------------------------------------- #
# 记录
# --------------------------------------------------------------------------- #
def plain_text(value: object) -> str:
    """把一个 CellValue 读成纯文本，用于身份比对。

    超链接类型的文本列（``style.type=url``）读回来是 Markdown 链接
    ``[https://…](https://…)``，富文本列读回来是片段数组。两种都要还原成原始
    字符串才能和采集结果里的裸链接对上 —— 否则每次运行都会把同一批作品
    再新增一遍，正是"只更新、不新增"要避免的事。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        match = _MD_LINK.fullmatch(value.strip())
        return (match.group(1) or match.group(2)).strip() if match else value.strip()
    if isinstance(value, list):
        return "".join(
            plain_text(part.get("text") if isinstance(part, dict) else part) for part in value
        )
    if isinstance(value, dict):
        for key in ("text", "url", "link"):
            if isinstance(value.get(key), str):
                return plain_text(value[key])
    return str(value)


def identity_key(value: object) -> str:
    """作品身份：把链接归一成「协议＋域名＋路径」，丢掉查询串与尾斜杠。

    这条不是洁癖，是小红书特有的坑：``作品链接`` 里带着 ``xsec_token``，
    而 token 每次采集都可能不一样。直接拿整条链接当身份，同一篇笔记第二次
    进来就成了新记录 —— 表里会一行一行地长。作品的真实身份只有路径里那段
    ``note_id``，那就按 ``note_id`` 认人。
    """
    text = plain_text(value)
    if not text:
        return ""
    if "?" in text:
        text = text.split("?", 1)[0]
    if "#" in text:
        text = text.split("#", 1)[0]
    return text.rstrip("/")


def existing_records(base_token: str, table_id: str, link_field_id: str,
                     scratch: Path) -> dict[str, str]:
    """表里已有的记录：``作品链接 -> record_id``。

    只投影身份列，不把整张表拖下来 —— 表可能有几万行、每行还带着整段文案。

    ``scratch`` 必须落在 lark-cli 允许写的位置（当前目录、``/tmp``、``~/files``）：
    它自己的守卫会拒绝别处的 ``--output``，所以调用方给的是 ``/tmp`` 下的临时目录。
    """
    out = scratch / "existing.ndjson"
    if out.exists():
        out.unlink()
    offset = 0
    index: dict[str, str] = {}
    while True:
        lark(
            "base", "+record-list",
            "--base-token", base_token, "--table-id", table_id,
            "--field-id", link_field_id,
            "--offset", str(offset), "--limit", "2000",
            "--format", "ndjson", "--output", str(out),
            "--overwrite",
        )
        rows = []
        if out.exists():
            for line in out.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    rows.append(json.loads(line))
        if not rows:
            break
        for row in rows:
            record_id = row.get("record_id")
            key = identity_key(row.get(IDENTITY))
            if record_id and key:
                index[key] = record_id
        if len(rows) < 2000:
            break
        offset += len(rows)
    return index


def cell(value: object, field: dict) -> object:
    """把一个值按**目标列的实际类型**整形。

    这里是"按列名填充"落地的地方：列名决定写哪一列，列的类型决定写成什么形状。
    我们只对自己新建的列有自己的类型偏好；用户改过的列一律迁就。
    """
    kind = field.get("type")
    if value is None or value == "":
        return None
    if kind == "number":
        try:
            return float(value) if isinstance(value, str) else value
        except (TypeError, ValueError):
            return value
    if kind == "datetime":
        # lark-cli 的 happy path 是 "2026-03-24 10:00"，且按多维表格自身的时区解释；
        # 建表时已把时区设成 Asia/Shanghai，而 collect.py 给的就是北京时间。
        text = str(value)
        return text[:16] if len(text) >= 16 else text
    if kind == "select":
        return [str(value)]
    if kind == "checkbox":
        return bool(value)
    return str(value)


def row_payload(row: dict, fields: dict[str, dict],
                constants: dict[str, str] | None = None) -> dict:
    """一行 13 字段（＋常量列）-> 一条 record 的 CellValue 映射，按列名取列。

    ``constants`` 是给「表里还有 13 字段之外的列」准备的，例如「来源」这种
    整批都一样的列。常量先铺、行值覆盖 —— 采集到的真实值永远比常量可信。
    """
    payload = {}
    merged = {**(constants or {}), **row}
    for name, value in merged.items():
        field = fields.get(name)
        if field is None:  # 表里没有这一列（被删了），跳过而不是重建
            continue
        shaped = cell(value, field)
        if shaped is None:
            continue
        payload[name] = shaped
    return payload


def write_batches(base_token: str, table_id: str, updates: dict[str, dict],
                  creations: list[dict], *, quiet: bool = False) -> None:
    """串行分批写。同一张表并行写会撞 1254291 并发冲突。"""

    def note(message: str) -> None:
        if not quiet:
            print(f"  · {message}", file=sys.stderr)

    items = list(updates.items())
    for start in range(0, len(items), BATCH):
        chunk = dict(items[start:start + BATCH])
        lark("base", "+record-batch-update",
             "--base-token", base_token, "--table-id", table_id,
             "--json", json.dumps({"update_records": chunk}, ensure_ascii=False))
        note(f"更新 {len(chunk)} 条（累计 {min(start + BATCH, len(items))}/{len(items)}）")

    for start in range(0, len(creations), BATCH):
        chunk = creations[start:start + BATCH]
        lark("base", "+record-batch-create",
             "--base-token", base_token, "--table-id", table_id,
             "--json", json.dumps({"create_records": chunk}, ensure_ascii=False))
        note(f"新增 {len(chunk)} 条（累计 {min(start + BATCH, len(creations))}/{len(creations)}）")


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #
def sync(plan: dict, *, folder_token: str | None = None, base: str | None = None,
         table: str | None = None, constants: dict[str, str] | None = None,
         scratch: Path, dry_run: bool = False, quiet: bool = False) -> dict:
    """把一份采集结果写进目标表。

    目标二选一：``base``（现成的多维表格，不再新建）或 ``folder_token``
    （在文件夹里按 ``table_name`` 找/建）。两种路径之后的步骤完全一样 ——
    补列、按「作品链接」认记录、串行写 —— 因为对调用方来说差别只在
    「表在哪」，不在「怎么写」。
    """
    def note(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    rows = plan.get("rows") or []
    name = plan.get("table_name") or ""
    if not rows:
        raise FeishuError("这份采集结果里一行都没有，不写空表。")
    if not base and not name:
        raise FeishuError("采集结果里没有 table_name，无法确定多维表格的名字。")

    created_base = False
    url = None
    if base:
        token, table_in_url = split_base(base)
        if not token:
            raise FeishuError(f"没有从 --base 里解析出多维表格 token：{base}")
        note(f"目标多维表格（用户指定，不新建）：{base_url(token)}")
        table_id = pick_table(token, table or table_in_url)
        note(f"工作表：{table_id}")
    else:
        token = find_base(folder_token, name)
        if token is None:
            if dry_run:
                note(f"[dry-run] 会在文件夹 {folder_token} 下新建多维表格「{name}」")
                return {"base_token": None, "table": None, "created_base": True,
                        "created": len(rows), "updated": 0, "dry_run": True}
            note(f"文件夹里没有「{name}」，新建一个")
            token, url = create_base(folder_token, name)
            created_base = True
            note(f"已建：{url or token}")
        else:
            note(f"复用已有的多维表格「{name}」：{token}")
        table_id = first_table(token)

    fields = fields_by_name(token, table_id)
    added = ensure_columns(token, table_id, fields) if not dry_run else []
    if added:
        note(f"补齐缺少的列：{'、'.join(added)}")
        fields = fields_by_name(token, table_id)

    identity_field = fields.get(IDENTITY)
    if identity_field is None:
        raise FeishuError(f"表里没有「{IDENTITY}」列，无法判断哪条是哪条。")
    unknown = [key for key in (constants or {}) if key not in fields]
    if unknown:
        note(f"  ! 表里没有这些列，--set 的值无处可写：{'、'.join(unknown)}")

    # dry-run 也读一遍已有记录：它只读，且「会新增 126 条」和「会更新 126 条」
    # 是两件完全不同的事，干跑的意义就在于把这件事算准。
    index = ({} if created_base
             else existing_records(token, table_id, identity_field["field_id"], scratch))
    note(f"表里已有 {len(index)} 条记录")

    updates: dict[str, dict] = {}
    creations: list[dict] = []
    for row in rows:
        payload = row_payload(row, fields, constants)
        key = identity_key(row.get(IDENTITY))
        record_id = index.get(key)
        if record_id:
            updates[record_id] = payload
        else:
            creations.append(payload)

    note(f"待写入：更新 {len(updates)} 条，新增 {len(creations)} 条")
    title = base_title(token) if not created_base else (name or "")
    if dry_run:
        return {"base_token": token, "table": table_id, "created_base": created_base,
                "base_title": title, "created": len(creations), "updated": len(updates),
                "dry_run": True}
    write_batches(token, table_id, updates, creations, quiet=quiet)
    return {"base_token": token, "table": table_id, "url": url or base_url(token),
            "base_title": title, "created_base": created_base, "created": len(creations),
            "updated": len(updates), "rows": len(rows)}


def base_title(token: str) -> str:
    """多维表格的名字。取不到就返回空串 —— 名字只用于「上次用的是哪张表」的提示。"""
    try:
        data = lark("drive", "+inspect", "--url", base_url(token))
    except FeishuError:
        return ""
    title = data.get("title") if isinstance(data, dict) else None
    return str(title) if title else ""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="feishu_bitable.py",
        description="把 13 字段作品列表写进飞书多维表格（按列名填充，同作品只更新不新增）。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "例：\n"
            '  python3 collect.py "操盘手" --json --out rows.json\n'
            "  python3 feishu_bitable.py --rows rows.json --folder LzVPfeVo3li6hId67VycezOcnPf\n"
        ),
    )
    p.add_argument("--rows", help="collect.py --json 产出的 JSON 文件（--show-folder / --set-folder 时不用给）")
    p.add_argument("--folder", help="本次使用的目标文件夹（链接或 token）；不给则用凭据文件里记的（上次用过的）")
    p.add_argument("--base", metavar="URL_OR_TOKEN",
                   help="直接写进这个现成的多维表格（/base/<token> 链接或 token）；给了就不再按文件夹找/建")
    p.add_argument("--table", metavar="NAME_OR_ID",
                   help="多维表格里的工作表（名字或 ID）；不给则用「作品列表」，再退到第一张")
    p.add_argument("--set", metavar="NAME=VALUE", action="append", default=[],
                   help="给 13 字段之外的列补常量，例如 --set 来源=抖音（可重复）")
    p.add_argument("--show-folder", action="store_true", help="打印当前默认文件夹地址后退出")
    p.add_argument("--set-folder", metavar="URL_OR_TOKEN",
                   help="换默认文件夹：接受文件夹链接或裸 token，写进凭据文件后退出")
    p.add_argument("--dry-run", action="store_true", help="只报告会做什么，不写任何东西")
    p.add_argument("--json", action="store_true", help="输出 JSON 结果")
    return p


def parse_constants(pairs: list[str]) -> dict[str, str]:
    """把 ``--set 来源=抖音`` 解析成字典。

    值里允许出现等号（``--set 备注=a=b`` 取 ``a=b``）：第一刀切在列名上，
    剩下的整段是值 —— 列名不含等号，值可能含。
    """
    out: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise FeishuError(f"--set 要写成 列名=值，收到的是「{pair}」")
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            raise FeishuError(f"--set 的列名是空的：{pair}")
        out[name] = value.strip()
    return out


def scratch_dir() -> Path:
    """lark-cli 的 ``--output`` 只允许写在当前目录、``/tmp`` 或 ``~/files``。

    用 ``/tmp``：它既不污染工作目录，也不受 ``$TMPDIR`` 那个不在白名单里的
    默认值影响。
    """
    return Path(tempfile.mkdtemp(prefix="dtk-feishu-", dir="/tmp"))


def main() -> int:
    args = build_parser().parse_args()

    if args.set_folder:
        try:
            token = set_default_folder(args.set_folder)
        except FeishuError as exc:
            print(f"换文件夹失败：{exc}", file=sys.stderr)
            return 2
        print(f"默认文件夹已切换：{folder_url(token)}")
        return 0

    if args.show_folder:
        try:
            token = load_folder(args.folder)
        except FeishuError as exc:
            print(f"还没有默认文件夹：{exc}", file=sys.stderr)
            return 2
        print(f"默认文件夹：{folder_url(token)}")
        return 0

    if not args.rows:
        build_parser().error("需要 --rows（或用 --show-folder / --set-folder）")

    plan = json.loads(Path(args.rows).read_text(encoding="utf-8"))
    scratch = scratch_dir()
    try:
        try:
            constants = parse_constants(args.set)
            if args.base:
                # 用户指明了表：不再解析文件夹，也不新建任何东西。
                folder = None
                result = sync(plan, base=args.base, table=args.table, constants=constants,
                              scratch=scratch, dry_run=args.dry_run, quiet=args.json)
                result["folder_url"] = None
            else:
                folder = load_folder(args.folder)
                result = sync(plan, folder_token=folder, constants=constants,
                              scratch=scratch, dry_run=args.dry_run, quiet=args.json)
                result["folder_url"] = folder_url(folder)
        except FeishuError as exc:
            print(f"写飞书失败：{exc}", file=sys.stderr)
            return 2
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        where = result.get("url") or result.get("base_token") or "（dry-run）"
        lines = []
        if result.get("folder_url"):
            lines.append(f"目标文件夹：{result['folder_url']}")
        lines.append(f"多维表格：{where}")
        lines.append(f"工作表：{result.get('table') or '（dry-run）'}　"
                     f"{'新建' if result['created_base'] else '复用已有'}")
        if constants:
            lines.append("常量列：" + "、".join(f"{k}={v}" for k, v in constants.items()))
        lines.append(f"写入：新增 {result['created']} 条，更新 {result['updated']} 条")
        print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
