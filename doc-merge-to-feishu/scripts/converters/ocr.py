"""扫描版 PDF 的文字识别。

引擎优先级：
1. macOS Vision（系统自带，离线，中文准确率高，无需装任何东西）
2. tesseract 命令行（装了就用，跨平台）
3. 飞书 OCR 接口（需要应用申请 optical_char_recognition:image 权限，没有 bbox）

识别结果是带归一化 bbox 的行；组装时先按 y 分组归行，再按行距合并成段落。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass

from .common import escape_md, smart_join

CJK = r"\u3000-\u303f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef"
SENTENCE_END = "。！？；：.!?;:）)」』】"


@dataclass
class OcrLine:
    text: str
    x0: float = 0.0
    y0: float = 0.0
    x1: float = 0.0
    y1: float = 1.0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.001)


class EngineError(RuntimeError):
    pass


# ---------------------------------------------------------------- 引擎


class BaseEngine:
    name = ""
    has_bbox = True

    def available(self) -> bool:
        return False

    def recognize(self, png_path: str) -> list[OcrLine]:
        raise NotImplementedError


class VisionEngine(BaseEngine):
    name = "macOS Vision"
    has_bbox = True

    def available(self) -> bool:
        try:
            import Quartz  # noqa: F401
            import Vision  # noqa: F401
        except Exception:
            return False
        return True

    def recognize(self, png_path: str) -> list[OcrLine]:
        import Quartz
        import Vision
        from Foundation import NSURL

        url = NSURL.fileURLWithPath_(png_path)
        source = Quartz.CGImageSourceCreateWithURL(url, None)
        if source is None:
            raise EngineError("Vision 读不了这张图")
        image = Quartz.CGImageSourceCreateImageAtIndex(source, 0, None)
        if image is None:
            raise EngineError("Vision 解码失败")

        handler = Vision.VNImageRequestHandler.alloc().initWithCGImage_options_(image, None)
        request = Vision.VNRecognizeTextRequest.alloc().init()
        request.setRecognitionLevel_(0)  # 0 = accurate，1 = fast（中文基本不可用）
        request.setUsesLanguageCorrection_(True)
        try:
            request.setRecognitionLanguages_(["zh-Hans", "zh-Hant", "en-US"])
        except Exception:
            pass
        try:
            request.setAutomaticallyDetectsLanguage_(True)
        except Exception:
            pass

        ok, err = handler.performRequests_error_([request], None)
        if not ok:
            raise EngineError(f"Vision 识别失败：{err}")

        lines: list[OcrLine] = []
        for obs in request.results() or []:
            candidates = obs.topCandidates_(1)
            if not candidates:
                continue
            text = str(candidates[0].string() or "").strip()
            if not text:
                continue
            box = obs.boundingBox()
            x0, y0 = box.origin.x, box.origin.y
            w, h = box.size.width, box.size.height
            lines.append(OcrLine(text=text, x0=x0, y0=y0, x1=x0 + w, y1=y0 + h))
        return lines


class TesseractEngine(BaseEngine):
    name = "tesseract"
    has_bbox = True

    def available(self) -> bool:
        return bool(shutil.which("tesseract"))

    def recognize(self, png_path: str) -> list[OcrLine]:
        binary = shutil.which("tesseract")
        if not binary:
            raise EngineError("找不到 tesseract")
        langs = "chi_sim+eng"
        try:
            listed = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30).stdout
            if "chi_sim" not in listed:
                langs = "eng"
        except Exception:
            pass
        proc = subprocess.run(
            [binary, png_path, "-", "-l", langs, "--psm", "3", "tsv"],
            capture_output=True, text=True, timeout=300,
        )
        if proc.returncode != 0:
            raise EngineError(f"tesseract 失败：{proc.stderr.strip()[:200]}")
        rows: dict[tuple, list] = {}
        for raw in proc.stdout.splitlines()[1:]:
            cols = raw.split("\t")
            if len(cols) < 12:
                continue
            text = cols[11].strip()
            if not text:
                continue
            key = (cols[2], cols[3], cols[4])  # page, block, par
            rows.setdefault(key, []).append((int(cols[6]), int(cols[7]), int(cols[8]), int(cols[9]), text))
        if not rows:
            return []
        # tesseract 的坐标是像素，先按最大宽高归一化
        max_x = max(v[2] for v in sum(rows.values(), [])) or 1
        max_y = max(v[3] for v in sum(rows.values(), [])) or 1
        lines = []
        for parts in rows.values():
            text = smart_join([p[4] for p in parts])
            x0 = min(p[0] for p in parts) / max_x
            y0 = min(p[1] for p in parts) / max_y
            x1 = max(p[2] for p in parts) / max_x
            y1 = max(p[3] for p in parts) / max_y
            lines.append(OcrLine(text=text, x0=x0, y0=1 - y1, x1=x1, y1=1 - y0))
        return lines


class FeishuEngine(BaseEngine):
    name = "飞书 OCR"
    has_bbox = False

    def available(self) -> bool:
        try:
            import feishu  # noqa: F401

            return bool(shutil.which("lark-cli"))
        except Exception:
            return False

    def recognize(self, png_path: str) -> list[OcrLine]:
        import base64
        import json

        import feishu

        with open(png_path, "rb") as f:
            payload = base64.b64encode(f.read()).decode()
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8")
        try:
            json.dump({"image": payload}, tmp)
            tmp.close()
            out = feishu.run(
                ["api", "POST", "/open-apis/optical_char_recognition/v1/image/basic_recognize",
                 "--data", f"@{tmp.name}", "--as", "bot", "--json"],
                timeout=120,
            )
        finally:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
        data = out.get("data") or {}
        texts = data.get("text") or []
        if isinstance(texts, str):
            texts = [texts]
        lines = []
        for i, text in enumerate(texts):
            text = str(text).strip()
            if text:
                # 没有 bbox，用递减的 y 让每行各自成段
                lines.append(OcrLine(text=text, y0=1 - i * 0.03, y1=1 - i * 0.03 + 0.01))
        return lines


ENGINES = [VisionEngine, TesseractEngine, FeishuEngine]


def pick_engine(preferred: str | None = None) -> BaseEngine | None:
    for cls in ENGINES:
        engine = cls()
        if preferred and cls.name.lower().find(preferred.lower()) < 0:
            continue
        if engine.available():
            return engine
    return None


def engine_report() -> list[tuple[str, bool]]:
    return [(cls.name, cls().available()) for cls in ENGINES]


# ---------------------------------------------------------------- 组装


def fix_spacing(text: str) -> str:
    """修 OCR 常见的数字断字：'2 0 24' → '2024'，'45 %' → '45%'。

    只合并 1–2 位的数字碎片，避免把 '2024 2025' 这种相邻年份粘在一起。
    """
    prev = None
    out = text
    while prev != out:
        prev = out
        out = re.sub(r"(?<!\d)(\d{1,2}) (\d{1,2})(?!\d)", r"\1\2", out)
    out = re.sub(r"(?<=\d) +(?=[%％])", "", out)
    out = re.sub(rf"(?<=[{CJK}]) +(?=[{CJK}])", "", out)
    out = re.sub(rf"(?<=[{CJK}]) +(?=[，。；：、！？）》」』])", "", out)
    return out


def _group_lines(lines: list[OcrLine]) -> list[OcrLine]:
    """把同一水平位置的多块合成一行（Vision 会把一行切成几段返回，且顺序不保证）。"""
    if not lines:
        return []
    ordered = sorted(lines, key=lambda l: (-(l.y0 + l.y1) / 2, l.x0))
    groups: list[list[OcrLine]] = []
    for line in ordered:
        center = (line.y0 + line.y1) / 2
        placed = False
        for group in groups:
            head = group[0]
            head_center = (head.y0 + head.y1) / 2
            height = max(head.height, line.height)
            if abs(head_center - center) < height * 0.6:
                group.append(line)
                placed = True
                break
        if not placed:
            groups.append([line])

    merged: list[OcrLine] = []
    for group in groups:
        group.sort(key=lambda l: l.x0)
        merged.append(OcrLine(
            text=smart_join([l.text for l in group]),
            x0=min(l.x0 for l in group),
            y0=min(l.y0 for l in group),
            x1=max(l.x1 for l in group),
            y1=max(l.y1 for l in group),
        ))
    return sorted(merged, key=lambda l: (-(l.y0 + l.y1) / 2, l.x0))


def lines_to_markdown(lines: list[OcrLine], headings: bool = False, heading_min_ratio: float = 1.3) -> str:
    lines = _group_lines(lines)
    if not lines:
        return ""
    heights = sorted(l.height for l in lines)
    median = heights[len(heights) // 2] or 0.02

    paragraphs: list[str] = []
    buffer: list[str] = []
    last: OcrLine | None = None

    def flush():
        if buffer:
            text = fix_spacing(smart_join(buffer))
            if text:
                paragraphs.append(_escape_paragraph(text))
            buffer.clear()

    for line in lines:
        text = line.text.strip()
        if not text:
            continue
        if headings and line.height >= median * heading_min_ratio and len(text) <= 40:
            flush()
            paragraphs.append("## " + escape_md(fix_spacing(text)))
            last = None
            continue
        if last is None:
            buffer.append(text)
        else:
            # 用基线间距 / 字高 判断是不是同一段：正常行距 1.2–1.8，段落间距更大
            height = max(line.height, last.height, 0.001)
            ratio = (last.y0 - line.y0) / height
            same_para = ratio < 1.9
            if same_para and not (last.text and last.text[-1] in SENTENCE_END):
                buffer.append(text)
            else:
                flush()
                buffer.append(text)
        last = line
    flush()
    return "\n\n".join(paragraphs)


def _escape_paragraph(text: str) -> str:
    escaped = escape_md(text)
    # 行首 # 会被当成标题
    return re.sub(r"^(\s*)#", r"\1\\#", escaped)


# ---------------------------------------------------------------- 页面


def ocr_page(page, engine: BaseEngine, dpi: int = 200, headings: bool = False) -> tuple[str, int]:
    """渲染一页并识别，返回 (markdown, 行数)。"""
    pix = page.get_pixmap(dpi=dpi)
    handle, path = tempfile.mkstemp(suffix=".png", prefix="dmocr_")
    os.close(handle)
    try:
        pix.save(path)
        lines = engine.recognize(path)
        return lines_to_markdown(lines, headings=headings), len(lines)
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
