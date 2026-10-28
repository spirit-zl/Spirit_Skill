"""飞书写入层：探测链接类型、创建文档、整篇覆盖、读取正文。"""
from __future__ import annotations

import json
import os
import shutil
import subprocess


class LarkError(RuntimeError):
    pass


def lark_bin() -> str:
    for candidate in (
        os.environ.get("LARK_CLI"),
        shutil.which("lark-cli"),
        os.path.expanduser("~/.workbuddy/binaries/node/cli-connector-packages/bin/lark-cli"),
    ):
        if candidate and os.path.isfile(candidate):
            return candidate
    raise LarkError("找不到 lark-cli，请先确认飞书连接器已安装并完成授权")


def run(args: list[str], cwd: str | None = None, timeout: int = 600) -> dict:
    cmd = [lark_bin()] + args
    try:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise LarkError(f"lark-cli 调用超时（{timeout}s）：{' '.join(args[:2])}") from exc
    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    payload = None
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            start = stdout.find("{")
            end = stdout.rfind("}")
            if start >= 0 and end > start:
                try:
                    payload = json.loads(stdout[start:end + 1])
                except json.JSONDecodeError:
                    payload = None
    if proc.returncode != 0:
        msg = ""
        if isinstance(payload, dict):
            msg = str(payload.get("error") or payload.get("message") or payload.get("msg") or "")
        raise LarkError(msg or stderr or stdout or f"lark-cli 退出码 {proc.returncode}")
    if payload is None:
        raise LarkError(f"lark-cli 返回无法解析：{stdout[:300] or stderr[:300]}")
    if isinstance(payload, dict) and payload.get("ok") is False:
        err = payload.get("error") or payload.get("message") or payload
        raise LarkError(str(err))
    return payload


def inspect(url: str) -> dict:
    out = run(["drive", "+inspect", "--url", url, "--as", "user", "--json"], timeout=120)
    data = out.get("data") if isinstance(out, dict) else None
    if not isinstance(data, dict):
        raise LarkError(f"无法识别链接：{url}")
    return data


def wiki_node(token: str) -> dict | None:
    try:
        out = run(["wiki", "+node-get", "--node-token", token, "--as", "user", "--json"], timeout=120)
    except LarkError:
        return None
    data = out.get("data")
    return data if isinstance(data, dict) else None


def fetch_markdown(doc_url: str) -> str:
    out = run(["docs", "+fetch", "--doc", doc_url, "--doc-format", "markdown", "--as", "user", "--json"], timeout=180)
    data = out.get("data") or {}
    for key in ("content", "markdown", "text"):
        if isinstance(data.get(key), str):
            return data[key]
    if isinstance(data.get("document"), dict):
        doc = data["document"]
        for key in ("content", "markdown", "text"):
            if isinstance(doc.get(key), str):
                return doc[key]
    return json.dumps(data, ensure_ascii=False)


def list_folder(folder_token: str) -> list[dict]:
    params = json.dumps({"folder_token": folder_token, "page_size": 200}, ensure_ascii=False)
    out = run(["api", "GET", "/open-apis/drive/v1/files", "--params", params, "--as", "user", "--json"], timeout=120)
    data = out.get("data") or {}
    files = data.get("files") or []
    return files if isinstance(files, list) else []


def create_doc(title: str, parent_token: str | None, md_rel: str, cwd: str) -> dict:
    args = [
        "docs", "+create", "--as", "user",
        "--doc-format", "markdown",
        "--content", f"@{md_rel}",
        "--title", title,
    ]
    if parent_token:
        args += ["--parent-token", parent_token]
    out = run(args, cwd=cwd, timeout=900)
    return _extract_doc(out)


def overwrite_doc(doc_url: str, md_rel: str, cwd: str) -> dict:
    args = [
        "docs", "+update", "--as", "user",
        "--doc", doc_url,
        "--command", "overwrite",
        "--doc-format", "markdown",
        "--content", f"@{md_rel}",
    ]
    out = run(args, cwd=cwd, timeout=900)
    return _extract_doc(out)


def append_doc(doc_url: str, md_rel: str, cwd: str) -> dict:
    """把一段 Markdown 追加到文档末尾（逐篇写入用）。"""
    args = [
        "docs", "+update", "--as", "user",
        "--doc", doc_url,
        "--command", "append",
        "--doc-format", "markdown",
        "--content", f"@{md_rel}",
    ]
    out = run(args, cwd=cwd, timeout=900)
    return _extract_doc(out)


def _extract_doc(out: dict) -> dict:
    data = out.get("data") or {}
    doc = data.get("document") if isinstance(data.get("document"), dict) else data
    url = None
    for key in ("url", "doc_url", "document_url"):
        if isinstance(doc.get(key), str):
            url = doc[key]
            break
    token = None
    for key in ("document_id", "doc_token", "token", "obj_token"):
        if isinstance(doc.get(key), str):
            token = doc[key]
            break
    return {"url": url, "token": token, "raw": data}
