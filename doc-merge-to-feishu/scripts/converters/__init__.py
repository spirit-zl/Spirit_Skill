"""转换器分发。"""
from __future__ import annotations

import os

from .base import DocResult, ImageSink

SUPPORTED_EXTS = {".docx", ".pdf", ".md", ".markdown", ".txt", ".epub"}
LEGACY_EXTS = {".doc": ".docx", ".wps": ".docx", ".rtf": ".docx"}

PDF_OPTIONS = {
    "max_pages", "ocr", "ocr_headings", "ocr_dpi",
    "ocr_max_pages", "ocr_keep_image", "ocr_engine",
}


def ext_of(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def convert_file(path: str, sink: ImageSink, **options) -> DocResult:
    from . import docx_conv, epub_conv, pdf_conv, text_conv

    ext = ext_of(path)
    if ext == ".docx":
        return docx_conv.convert(path, sink)
    if ext == ".pdf":
        kwargs = {k: v for k, v in options.items() if k in PDF_OPTIONS}
        if "ocr_engine" in kwargs:
            kwargs["ocr_engine_name"] = kwargs.pop("ocr_engine")
        return pdf_conv.convert(path, sink, **kwargs)
    if ext == ".epub":
        return epub_conv.convert(path, sink)
    if ext in (".md", ".markdown", ".txt"):
        return text_conv.convert(path, sink)
    if ext in LEGACY_EXTS:
        raise ValueError(f"不支持旧的 {ext} 格式，请先用 Word/WPS 另存为 {LEGACY_EXTS[ext]}")
    raise ValueError(f"不支持的文件类型：{ext or '无扩展名'}")
