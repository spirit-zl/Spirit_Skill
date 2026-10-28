# 解析与写入的实现细节

给后续维护者看的：为什么这么写、哪里容易坏、坏了怎么查。

## 为什么不用 pandoc / markdownify

- 本机没有 pandoc，用户机器也未必有；引入外部二进制会把「装个技能」变成「装一套工具链」。
- `markdownify`/`html2text` 对中文排版处理一般（中英文之间乱加空格），而且要额外装包。
- 现成工具都无法处理「标题必须从 H2 起」这个定制规则，反正要自己遍历，不如全用 Python 现有依赖：
  `python-docx`、`pymupdf`、`beautifulsoup4` + `lxml`、标准库 `zipfile`。

## 统一管线

```
文件 → converters/<格式>.py → DocResult(markdown, headings, images, warnings)
     → merge.py: 标题对齐 → 图片占位符替换 → 逐文件加 H1 → merged.md
     → feishu.py: 探测链接 → create/overwrite
```

每个转换器只负责「把这份文件变成从 H1 起的 Markdown」，图片统一写成占位符
`@@DMIMG:0001@@`，由 `merge.py` 在知道最终相对路径后才替换成
`![alt](@./assets/01_001.png)`。这样转换器不用关心 cwd，也不用管文件冲突。

## 标题降级的两步走

1. `demote_to_start_at_h1`：把该文件的最浅标题对齐到 H1（例如全篇只有 `####`，先提成 `#`）。
2. `shift_headings(+1)`：整体下沉一级，于是落进飞书时从 H2 起。

删掉与文件名重复的首个 H1 之后，**必须重新跑一遍第 1 步**，否则正文会从 H3 起跳——
这是第一版踩过的坑。

`shift_headings` 会跟踪 ``` / ~~~ 围栏，代码块里以 `#` 开头的注释不会被误改。

### 什么叫「与文件名重复」

`loose_key` 只保留数字/字母/汉字并小写，再去掉开头的序号（`01`、`第2讲`）和结尾的扩展名残留，
然后双向包含匹配。所以 `03-SKILL规范写作.md.pdf` 的 H1 `03 · SKILL.md 规范写作` 能被认出来是同一个。
比对时两边各出一个「去掉所有 `md` 子串」的变体，专门对付 `.md` 导出成 PDF 的那批文件。

H1 文本本身也走 `strip_doc_ext`：`xx.md.pdf` → `xx`，标题里不留 `.md` 尾巴。

## docx

- 顺序靠 `document.element.body.iterchildren()` 拿 `w:p` / `w:tbl`，不用 `document.paragraphs`
  （那个会把表格全部排到最后）。
- 标题：`paragraph.style.name` 匹配 `^(heading|标题)\s*([1-9])$`；`Title` 样式算 H1；
  都没有就看 `w:outlineLvl`（0 基）。
- 列表：`w:numPr` 有就是列表项（层级取 `w:ilvl`）；样式名含 `List Bullet` / `List Number`
  但没有 numPr 的（Word 内置样式）也认，`List Number` 用 `1. `。
- 内联：直接遍历 `w:r` / `w:hyperlink` / `w:br`，run 级 `w:b` / `w:i` 转 `**` / `*`，
  超链接取 `part.rels[rId].target_ref`。
- 图片：`a:blip` 的 `r:embed` → `part.related_parts[rid].blob`，按原扩展名落盘。
  `emf/wmf/tiff/svg` 飞书不收，直接跳过（`ImageSink.save` 里拦）。
- 表格合并单元格会重复返回同一 `tc`，用 `id(cell._tc)` 去重。

## pdf

### 按逻辑行重建，不按 block

PDF 导出的 block 顺序**不等于阅读顺序**：编号列表的序号、被挤到行尾的字常常各自成一个 block。
按 block 拼会得到「正文 → 1. → 标题」这种三行错位。所以流程是：

1. 文本用 `page.get_text("dict", flags=TEXTFLAGS_TEXT)`；图片位置另用 `page.get_image_info(xrefs=True)`
   拿 bbox，字节在需要时才 `doc.extract_image(xref)`（整本压内存会让 20 页扫描件峰值到 600MB+）。
2. 所有 line 按纵向重叠 >50% 分组 → 组内按 x 排序 → 横向间隙超过**三成页宽**就切开（双栏），
   间隙内合并成一条逻辑行；三列以上用 ` | ` 连起来保住表格的列信息。
3. 段落判定用 **基线间距 ÷ 字号**：单倍行距 ≈1.2，1.5 倍 ≈1.8，2 倍 >2.0，阈值取 2.0。
   注意 PDF 的 **y 轴向下增大**，行距是「当前行底 − 上一行底」，写成反的会让整页粘成一段。
   上一行以句末标点结尾时也强制断段。

### 页眉页脚

统计每页顶部 10% / 底部 10% 的文本，**跨页出现 ≥60%（且至少 3 页）**的判为页眉页脚，整行丢弃；
页边区形如 `- 3 -` / `第 3 页` / `3/12` 的纯页码行也丢。这是全文唯一会删内容的地方。

### 标题

1. 有书签就用书签（`doc.get_toc()`），按页匹配逻辑行文本（归一化后相等或前缀匹配）；
2. 没书签按字号：正文字号 = 按字符数加权的众数；字号 ≥ 正文 ×1.12 → 候选。
   字号去重降序，前 3 档映射 H1–H3，更多档次归 H3。
3. **收紧条件**：还要 ≤40 字且不以 `。，！？；、` 结尾。设计稿类 PDF 字号层级密集，
   不加这两条会把长段落整段判成标题（实测 10 页的报告能误判出 100+ 个标题）。
4. `use_bold` 默认关 —— 加粗判标题误判率高。

### 扫描页

一页文字 < 100 字、图片面积 > 页面 50% → 扫描页，交给 OCR（见下）。识别成功就丢掉整页图
（`sink.rollback_to`），识别为空反而保留原图，不凭空丢内容。

- 行拼接用 `smart_join`：中文之间不加空格，英文之间加；行尾 `-` 且下一行小写字母开头时去掉连字符。

## epub

- `META-INF/container.xml` → `rootfile/@full-path` → opf；找不到就扫 zip 里第一个 `.opf`。
- 阅读顺序只认 `spine/itemref`，跳过 `linear="no"`、`properties` 含 `nav`、文件名含
  `nav/toc/cover` 的项。
- 每个 XHTML 交给 `HtmlToMd`，图片 `src` 用 `posixpath.normpath(join(章节目录, src))` 解到 zip 内路径。
- XHTML 用 `BeautifulSoup(html, "lxml")`（HTML 解析器）而不是 XML 解析器：epub 里混着 HTML5 标签，
  XML 模式反而更容易炸。代价是会刷 `XMLParsedAsHTMLWarning`，已在模块顶部过滤。

## html → markdown

`HtmlToMd` 是手写的块级/行内两段式渲染：

- 块级识别靠 `BLOCK_TAGS`；连续的行内节点攒成一个段落，遇到块级标签才 flush。
- 列表递归渲染，嵌套缩进 2 空格；`li` 里既有文字又有子列表时先出文字再出子列表。
- 表格第一行当表头（没有 `thead` 也一样），单元格里的 `|` 转义成 `\|`。
- 行内文本统一走 `escape_md`（转义 `\ * _ ` [ ] $ ~ < >`）和 `tidy_inline`
  （压空白、去掉中文之间的空格）。
- 跳过 `script/style/noscript/svg/head/title`。

## 飞书写入

- `drive +inspect --url` 一步拿到类型和标题，wiki 链接会自动解包并在 `wiki_node` 里给出真实
  `obj_type` / `obj_token`。所以 `folder` 走新建、`docx` 走覆盖，判断只有一个来源。
- 新建：`docs +create --doc-format markdown --content @merged.md --title <标题> --parent-token <folder>`。
- 覆盖：`docs +update --command overwrite --doc <url> --doc-format markdown --content @merged.md`，
  **不会改文档名**。
- `@file` 和图片的 `@./assets/x.png` 都只认**当前工作目录下的相对路径**，所以
  `subprocess.run(..., cwd=session_dir)`。传绝对路径会报 `unsafe file path`。
- 非空判断：`docs +fetch --doc-format markdown` 回来去掉空白后 > 30 字即视为有内容，
  需要 `--force`。空文档回读只有一行 `<title>...</title>`。
- 同名校验：`GET /open-apis/drive/v1/files?folder_token=...` 列 200 条，按 `name` 精确匹配。

## 排障命令

```bash
S=~/.workbuddy/skills/doc-merge-to-feishu/scripts/run.sh
bash "$S" show --json                      # 看最近一次的文件清单、警告、写入记录
cat <session>/session.json | jq .files     # 单个文件的解析结果
bash "$S" prepare <一个文件> --json         # 只试一个文件，看 warnings
bash "$S" push "<链接>" --dry-run           # 只探测链接类型，不写
```

单独跑某个转换器：

```bash
cd ~/.workbuddy/skills/doc-merge-to-feishu/scripts
/Users/spirit/.workbuddy/binaries/python/envs/default/bin/python - <<'PY'
from converters.base import ImageSink
from converters import convert_file
r = convert_file("/path/to/file.pdf", ImageSink("/tmp/imgs", "00"))
print(r.markdown[:2000]); print(r.warnings)
PY
```

## OCR（扫描版 PDF）

### 为什么用 macOS Vision

系统自带、离线、不用下模型，中文准确率比通用开源模型好，速度约 0.3 秒/页（200 dpi）。
调用走 PyObjC（`Vision` / `Quartz` 随 pyobjc 装好，实测可用），不依赖 ocrmac 这类封装。

参数是实测选出来的：

| 参数 | 结论 |
|---|---|
| `setRecognitionLevel_` | **必须 0（accurate）**。1（fast）会输出 `*8*￿$FIll 3 Ai` 这种垃圾 |
| `setUsesLanguageCorrection_` | True |
| `setAutomaticallyDetectsLanguage_` | True —— 关掉时英文标点会被识别成全角 `：`，且"86% in"会粘成 "86%in" |
| `setRecognitionLanguages_` | `["zh-Hans","zh-Hant","en-US"]`，仅作提示 |
| 渲染 dpi | **200 最好**。300 反而把 `45%` 拆成 `4 5 ％`，且慢一倍 |

tesseract 作为非 macOS 兜底：调命令行 `tesseract img - -l chi_sim+eng --psm 3 tsv`，
按 `page/block/par` 三列聚成行（tsv 里 `word` 级别太碎）。没有 `chi_sim` 语言包就退成 `eng`。

飞书 OCR（`/open-apis/optical_char_recognition/v1/image/basic_recognize`）实测**两条路都不通**：
user token 报 `99991668 not support`，bot token 报 `99991672 app_scope_not_applied`
（缺 `optical_char_recognition:image`）。代码里保留这个引擎，用户申请权限后可用，但不作为默认路径。

### 行分组

Vision 会把**同一行切成多个 observation**，而且返回顺序不保证。例：

```
y0=0.888 x0=0.267 | 年度经营复盘
y0=0.891 x0=0.121 | 2 0 2 4
```

按 y 排序直接拼会得到「年度经营复盘2024」。所以 `_group_lines` 先按 y 中心分组（容差 0.6 倍行高），
组内**按 x0 排序**再拼。

### 段落合并

用「基线间距 / 行高」判断，不是用 bbox 间的空隙：

```python
ratio = (last.y0 - line.y0) / max(line.height, last.height)
same_para = ratio < 1.9 and last.text[-1] not in "。！？；：.!?;:"
```

正常行距 1.2–1.8 归同段；1.9 以上（或上一行以句号收尾）断开。阈值定在 1.9 是因为中文扫描件
常见 1.5 倍行距，而段落间距通常是 2 倍以上。

### 数字粘连

Vision 对 CJK 里的数字常插空格（`2 0 24`、`62 ％`）。`fix_spacing` 循环合并 **1–2 位**的数字碎片，
不碰 3 位以上，避免把 `2024 2025` 这种相邻年份粘成 `20242025`。

### 内存

第一版用 `TEXT_PRESERVE_IMAGES` 把所有页的图片字节留在内存里，20 页扫描件峰值 625MB。
现在 `_collect` 只用 `TEXTFLAGS_TEXT` 取文本，图片位置从 `page.get_image_info(xrefs=True)` 补，
真要用图时再 `doc.extract_image(xref)`。20 页降到 510MB，主要成本变成 pymupdf 自己的缓存。
扫描页 OCR 成功后 `sink.rollback_to()` 把刚存的整页图删掉，文档里不出现「文字 + 同一张图」。

## 表格：为什么不用 PyMuPDF 自带的 find_tables 结果直接输出

`page.find_tables()` 在这批「由 Markdown 导出」的 PDF 上会给出两种结果：

- `vertical_strategy="lines"`：有框线的表能找到（列切得对），但常把整页文字吞进同一个
  12 行网格，头几行是正文。
- `vertical_strategy="text"`：无框表也能找到，但单元格跨行回绕时 `extract()` 会把内容摊错行，
  一行的多个单元格被拼成一句长话。

因此流程改成三段：

1. 两种策略都跑，各自清洗：丢全空列、只留「连续多行且每行 ≥2 个非空单元格」的段落；
2. 过滤伪表：两列且左列基本是编号/项目符号（那是列表，不是表）、单元格中位数过长（那是正文）；
3. 认领：用行框/列框的**空间范围**把文字碎片从正文流里剔除。

第 3 步必须按坐标而不是按文字匹配——表格里被折行拆出来的单字（`稿`、`取`、`索`）文字匹配
认不到，它们会漏到正文里变成一行行碎片。

## 列表项折行：断句错位的真凶

编号项 `1. ……很难` 换行后是 `封。`，两行行距 1.71（小于阈值），但当时的代码在判断正文之前
就先 `flush_list()`，折下来的那半句被当成新段落，于是出现：

```
2. ……纯靠灵感、无法言传的东西很难

封。
```

修法：进正文逻辑之前先判「当前是否还在列表里 + 行距是否够紧 + 相对标记是否有缩进」，
满足就 `smart_join` 回 `list_run[-1]`。另外项目符号后常没有空格（`•有结果可验收`），
`LIST_START_RE` 要用 `\s*(?=\S)` 而不是要求空白。

## 双栏：按行读会把两栏粘在一起

双栏页按 (y, x) 排序时，左右两栏逐行交错，标题会被粘成
`误区一：……误区二：……`。判定版心间隙要求同时满足：

- 一条竖带（±6pt）里几乎没碎片跨过；
- 左右两侧各占 20% 以上碎片；
- ≥45% 的行两侧同时有字（排除封面大字、居中标题造成的假间隙）。

满足就先把碎片按间隙切成左右两组，各自成行，再「左栏全部 → 右栏全部」拼接。
混合版面（页面上半是表格、下半双栏）目前识别不出，是已知局限。
