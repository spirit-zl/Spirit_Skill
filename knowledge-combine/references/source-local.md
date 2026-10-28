# 本地源采集

## 扫描规则

```
python3 scripts/collect_local.py collect --manifest <M> --path <dir> [--no-recursive]
  [--include "*.md,*.docx"] [--exclude ".git,node_modules"] [--max-docs 200] [--updated-after 2026-01-01]
```

- 扩展名白名单：`.md .markdown .txt .html .htm .pdf .docx .doc .pptx .xlsx .xls .csv .json`
- 默认排除：`.git` `.svn` `node_modules` `__pycache__` `.venv` `venv` `.DS_Store` `Library` `~$*`（Office 锁文件）
- 软链接：默认不跟随，防环；`--follow-links` 才跟随（仍用 realpath visited 防环）
- `max_docs` 只是**采集上限**，命中后按修改时间倒序保留并在报告注明截断
- 优先级最高（去重仲裁时保留本地版本）

## 解析器

| 扩展名 | 方式 |
|---|---|
| md / markdown / txt | 直接读（UTF-8，失败回退 GBK，再失败 latin-1） |
| html / htm | BeautifulSoup 去 script/style，转 Markdown 标题/列表/表格 |
| docx | python-docx：段落 + 表格（表格转 Markdown 表），图片落盘 |
| doc | 旧格式不支持 → `parse_failed`，提示先转 docx |
| pdf | pymupdf 逐页 `get_text()`；**文本层为空 → 判定扫描件 → Vision OCR** |
| pptx | python-pptx：逐页文本 + notes |
| xlsx / xls | openpyxl：每 sheet 转 Markdown 表（空 sheet 跳过） |
| csv | 直接转 Markdown 表 |
| json | 缩进化文本 |

## PDF 扫描件 OCR

判据：文本页平均字符 < 20 且页数 > 0。
走 macOS Vision（已装 `pyobjc-framework-Vision` / `Quartz`，**不要用 Swift，本机 swiftc 编不过**）：

```
python3 scripts/collect_local.py ocr --pdf <file> --out <md> [--pages 1-10]
```

OCR 成功仍标 `no_truncation_detected`（并记 `parse_note="ocr"`）；失败标 `parse_failed`。

## 配图

`image_policy=recover` 时：

- PDF：pymupdf `page.get_images()` 抽嵌入图；抽不到则整页渲染（150 dpi）
- DOCX：解 `word/media/*`
- PPTX：解 `ppt/media/*`
- 落盘 `<job_dir>/img/<source_id>_<page>.<ext>`，登记 `images[]`

## 常见坑

- Office 临时文件 `~$xxx.docx` 一定要排除，否则解析报错刷屏
- 中文路径 + 空格：所有 subprocess 调用必须走数组参数，不要拼字符串 shell
- 单文件 > 50MB 先打 warning，PDF 超 300 页提示用户确认是否全量
