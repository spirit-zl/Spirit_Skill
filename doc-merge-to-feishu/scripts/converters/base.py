"""转换器公共数据结构。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class DocResult:
    markdown: str = ""
    title: str = ""
    headings: list = field(default_factory=list)  # [(level, text)]
    image_count: int = 0
    warnings: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {
            "title": self.title,
            "chars": len(self.markdown),
            "headings": len(self.headings),
            "images": self.image_count,
            "warnings": self.warnings,
        }


class ImageSink:
    """图片落盘器：由主流程提供，返回可直接写进 Markdown 的占位符。"""

    def __init__(self, out_dir: str, prefix: str):
        self.out_dir = out_dir
        self.prefix = prefix
        self.count = 0
        self.records = []  # [{file, alt}]

    def save(self, blob: bytes, ext: str, alt: str = "") -> str | None:
        import os
        import re

        from .common import IMG_TOKEN

        ext = (ext or "").lower().lstrip(".")
        if ext in ("emf", "wmf", "tiff", "tif", "psd", "svg"):
            return None
        if not blob:
            return None
        if ext not in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
            ext = "png"
        os.makedirs(self.out_dir, exist_ok=True)
        self.count += 1
        name = f"{self.prefix}_{self.count:03d}.{ext}"
        path = os.path.join(self.out_dir, name)
        with open(path, "wb") as f:
            f.write(blob)
        token = IMG_TOKEN.format(self.count)
        clean_alt = re.sub(r"\s+", " ", (alt or "")).strip()[:60]
        self.records.append({"token": token, "file": name, "alt": clean_alt})
        return token

    def token_for(self, index: int) -> str:
        from .common import IMG_TOKEN

        return IMG_TOKEN.format(index)

    def rollback_to(self, count: int) -> None:
        """丢掉最近存的图片（扫描页做了 OCR 就不需要整页图）。"""
        import os

        for record in self.records[count:]:
            try:
                os.unlink(os.path.join(self.out_dir, record["file"]))
            except OSError:
                pass
        self.records = self.records[:count]
        self.count = count
