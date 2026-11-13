#!/usr/bin/env python3
"""render_xml.py —— 把一期直播回放的结构化内容渲染为飞书 DocxXML

用法：
    python3 render_xml.py episode.json -o episode.xml

输入 JSON 结构：
{
  "title": "趋势复利（2026-11-12）",
  "meta":  "讲师：秦蕾（投顾执业编号 A0050625120043）｜策牛科技｜19:25 场次｜回放 31 分 06 秒",
  "sections": [
    {"h2": "一、大盘情绪判断", "blocks": [
        {"p": "正文段落"},
        {"sub": "小标题（独立成行、加粗）"},
        {"ol": ["第一项。", "第二项。"]},
        {"ul": ["要点一。", "要点二。"]},
        {"table": {"headers": ["A", "B"], "rows": [["1", "2"]]}}
    ]}
  ]
}

排版规则（代码内固化，勿在内容里手工补空段）：
  1. 标题正下方直接接 meta 或正文，不留空段；
  2. 每个 h2 章节标题之前留 1 个空段；
  3. 小标题之前留 1 个空段，但上方紧邻章节标题时不留；小标题下方直接接正文；
  4. 列表、表格之后留 1 个空段；
  5. 任何位置不出现连续两个空段。
"""

import argparse
import json
import sys


def esc(text):
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\n", "<br/>")
    )


class Renderer:
    def __init__(self):
        self.out = []
        self.last_blank = False
        self.last_kind = None

    def blank(self):
        if not self.last_blank:
            self.out.append("<p></p>")
            self.last_blank = True

    def emit(self, xml, kind):
        self.out.append(xml)
        self.last_blank = False
        self.last_kind = kind

    def render(self, data):
        title = data.get("title") or ""
        meta = data.get("meta") or ""
        self.emit(f"<h1>{esc(title)}</h1>", "h1")
        if meta:
            self.emit(f"<p>{esc(meta)}</p>", "meta")

        for sec in data.get("sections", []):
            self.blank()
            self.emit(f"<h2>{esc(sec['h2'])}</h2>", "h2")

            for blk in sec.get("blocks", []):
                if "p" in blk:
                    self.emit(f"<p>{esc(blk['p'])}</p>", "p")

                elif "sub" in blk:
                    if self.last_kind not in ("h2", "blank"):
                        self.blank()
                    self.emit(f"<p><b>{esc(blk['sub'])}</b></p>", "sub")

                elif "ol" in blk:
                    items = "".join(f"<li>{esc(x)}</li>" for x in blk["ol"])
                    self.emit(f"<ol>{items}</ol>", "list")
                    self.blank()

                elif "ul" in blk:
                    items = "".join(f"<li>{esc(x)}</li>" for x in blk["ul"])
                    self.emit(f"<ul>{items}</ul>", "list")
                    self.blank()

                elif "table" in blk:
                    t = blk["table"]
                    head = "".join(
                        f'<th background-color="light-gray"><p>{esc(h)}</p></th>'
                        for h in t["headers"]
                    )
                    body = ""
                    for row in t["rows"]:
                        body += "<tr>" + "".join(f"<td><p>{esc(c)}</p></td>" for c in row) + "</tr>"
                    self.emit(
                        f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>",
                        "table",
                    )
                    self.blank()

                else:
                    raise SystemExit(f"未知块类型：{list(blk.keys())}")

        return "".join(self.out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("episode_json")
    ap.add_argument("-o", "--out", default="-")
    args = ap.parse_args()

    with open(args.episode_json, encoding="utf-8") as f:
        data = json.load(f)

    xml = Renderer().render(data)
    if args.out == "-":
        sys.stdout.write(xml)
    else:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(xml)
        print(f"written {args.out} ({len(xml)} chars)")


if __name__ == "__main__":
    main()
