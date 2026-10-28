#!/usr/bin/env python3
"""飞书多维表格读写层：全部通过 lark-cli 子进程完成（纯标准库）。

只做四件事：
    resolve()        链接 → base_token / table_id / 标题
    ensure_fields()  缺哪列补哪列（已有的列不动类型）
    list_records()   把记录读成 [{record_id, 字段: 文本}]
    batch_update()   按字节切批写回（正文很长，一次几百条会顶到命令行上限）
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time

LARK = shutil.which("lark-cli") or "lark-cli"


class LarkError(RuntimeError):
    pass


def run(args: list[str], cwd: str | None = None, timeout: int = 180) -> dict:
    proc = subprocess.run(
        [LARK] + args,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    out = (proc.stdout or "").strip()
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        tail = (proc.stderr or out or "")[-800:]
        raise LarkError(f"lark-cli 返回非 JSON（{' '.join(args[:2])}）：{tail}") from None
    if not data.get("ok"):
        err = data.get("error") or {}
        raise LarkError(
            f"lark-cli 失败（{' '.join(args[:2])}）："
            f"{err.get('code')} {err.get('message') or err.get('msg') or data}"
        )
    return data.get("data") or {}


# ────────────────────────────── 定位 ──────────────────────────────

def resolve(url_or_token: str, table: str | None = None) -> dict:
    """支持 wiki / base 链接，或直接给 base_token。返回 base_token / table_id / 标题。"""
    base_title = ""
    if url_or_token.startswith("http"):
        d = run(["base", "+url-resolve", "--url", url_or_token, "--as", "user", "--format", "json"])
        base_token = d.get("base_token")
        if not base_token:
            raise LarkError(f"链接解析不出 base_token：{d}")
        base_title = d.get("title", "") or ""
        if d.get("table_id"):
            return {"base_token": base_token, "table_id": d["table_id"],
                    "title": d.get("table_name", ""), "base_title": base_title}
    else:
        base_token = url_or_token
        try:
            base_title = run(["base", "+base-get", "--base-token", base_token,
                              "--as", "user", "--format", "json"]).get("name", "")
        except LarkError:
            base_title = ""

    tables = run(
        ["base", "+table-list", "--base-token", base_token, "--as", "user", "--format", "json"]
    ).get("tables") or []
    if not tables:
        raise LarkError("这个多维表格里没有数据表")

    if table:
        hit = [t for t in tables if table in (t.get("name"), t.get("id"))]
        if not hit:
            names = "、".join(t.get("name", "") for t in tables)
            raise LarkError(f"找不到表「{table}」，现有：{names}")
        chosen = hit[0]
    else:
        chosen = max(tables, key=lambda t: t.get("records_count") or 0)
    return {"base_token": base_token, "table_id": chosen["id"],
            "title": chosen.get("name", ""), "base_title": base_title}


def field_list(base_token: str, table_id: str) -> list[dict]:
    d = run(
        ["base", "+field-list", "--base-token", base_token, "--table-id", table_id,
         "--as", "user", "--format", "json"]
    )
    return d.get("fields") or []


def ensure_fields(base_token: str, table_id: str, specs: list[dict]) -> list[str]:
    """返回本次新建的字段名。已有同名列一律沿用，不覆盖类型。"""
    existing = {f.get("name") for f in field_list(base_token, table_id)}
    missing = [s for s in specs if s["name"] not in existing]
    if not missing:
        return []
    run(
        ["base", "+field-create", "--base-token", base_token, "--table-id", table_id,
         "--json", json.dumps(missing, ensure_ascii=False), "--as", "user", "--format", "json"]
    )
    return [s["name"] for s in missing]


# ────────────────────────────── 单元格读取 ──────────────────────────────

URL_RE = re.compile(r"https?://[^\s)\]>\"'，。；]+")


def cell_url(value) -> str:
    """从单元格里取出文章链接：兼容纯文本、Markdown 链接、富文本数组。"""
    if value is None:
        return ""
    if isinstance(value, str):
        m = URL_RE.search(value)
        return m.group(0) if m else ""
    if isinstance(value, dict):
        for k in ("link", "text", "url"):
            if value.get(k):
                return cell_url(value[k])
        return ""
    if isinstance(value, list):
        for item in value:
            got = cell_url(item)
            if got:
                return got
        return ""
    return ""


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        return (value.get("text") or value.get("link") or "").strip()
    if isinstance(value, list):
        return "".join(cell_text(v) for v in value).strip()
    return str(value).strip()


def cell_date(value) -> str:
    """把日期单元格归一成 YYYY-MM-DD（读回来是 ISO 串）。"""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", cell_text(value))
    return m.group(1) if m else ""


def list_records(base_token: str, table_id: str, fields: list[str]) -> list[dict]:
    """翻页读全表（CLI 单页上限 200 行）。"""
    out: list[dict] = []
    offset = 0
    while True:
        d = run(
            ["base", "+record-list", "--base-token", base_token, "--table-id", table_id,
             "--as", "user", "--format", "json", "--limit", "200", "--offset", str(offset)]
        )
        names = d.get("fields") or []
        ids = d.get("record_id_list") or []
        rows = d.get("data") or []
        for rid, cells in zip(ids, rows):
            row = {"record_id": rid}
            for i, name in enumerate(names):
                row[name] = cells[i] if i < len(cells) else None
            for f in fields:
                row.setdefault(f, None)
            out.append(row)
        if not d.get("has_more") or not ids:
            break
        offset += len(ids)
    return out


# ────────────────────────────── 写入 ──────────────────────────────

def _chunks(updates: dict, max_bytes: int = 180_000, max_items: int = 200):
    batch, size = {}, 0
    for rid, payload in updates.items():
        cost = len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) + len(rid) + 8
        if batch and (size + cost > max_bytes or len(batch) >= max_items):
            yield batch
            batch, size = {}, 0
        batch[rid] = payload
        size += cost
    if batch:
        yield batch


def batch_update(base_token: str, table_id: str, updates: dict, payload_dir: str,
                 retries: int = 2) -> int:
    """按字节切批、串行写入。返回写入条数。

    刚创建完字段就立刻写记录时，服务端偶发 not_found（表结构还在刷新），
    这里退避重试一次即可，不必让整批失败。
    """
    total = 0
    for idx, batch in enumerate(_chunks(updates), 1):
        path = os.path.join(payload_dir, f"payload_{idx}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"update_records": batch}, fh, ensure_ascii=False)
        args = ["base", "+record-batch-update", "--base-token", base_token, "--table-id", table_id,
                "--json", "@" + os.path.basename(path), "--as", "user", "--format", "json"]
        for attempt in range(retries + 1):
            try:
                run(args, cwd=payload_dir)
                break
            except LarkError as exc:
                if attempt >= retries or "not_found" not in str(exc):
                    raise
                time.sleep(1.5 * (attempt + 1))
        total += len(batch)
    return total
