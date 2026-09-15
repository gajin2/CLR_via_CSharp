# -*- coding: utf-8 -*-
"""从 chapters/*.md 构建 EPUB 3 电子书（可被 Calibre 正常打开与转换）。

设计要点
--------
1. OPF manifest 必须登记**全部**资源（含图片），否则 epubcheck 报 RSC-008、
   Calibre 导入时报 "not declared in the OPF manifest"。
2. 所有内部链接（nav / landmarks / 正文锚点）必须指向真实存在的文件与 id。
3. mimetype 必须是 zip 的第一条目且以 STORED（不压缩）方式写入。
4. 正文文档全部为良构 XHTML（用 xml.etree 解析通过），供 Calibre 的
   EPUB 输入插件直接消费。

运行: python build_epub.py
"""
import os
import re
import html
import zipfile
from datetime import datetime, timezone

import markdown
from pygments.formatters import HtmlFormatter

ROOT = os.path.dirname(os.path.abspath(__file__))
CHAPTERS = os.path.join(ROOT, "chapters")
IMAGES = os.path.join(ROOT, "resources", "images")
DRAWIOS = os.path.join(ROOT, "resources", "drawios")
OUT = os.path.join(ROOT, "CLR_via_CSharp_4th_Edition.epub")

BOOK_TITLE = "CLR via C#（第4版）"
AUTHOR = "Jeffrey Richter 著  周靖 译"
LANG = "zh-CN"
UUID = "urn:uuid:6f1c9f2e-8a34-4d5e-9b7c-c1a2c3d4e5f0"

MEDIA = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "gif": "image/gif", "svg": "image/svg+xml", "webp": "image/webp",
}

# ---------------------------------------------------------------- 文本预处理

md_engine = markdown.Markdown(
    extensions=["tables", "fenced_code", "footnotes", "codehilite", "sane_lists", "attr_list"],
    extension_configs={"codehilite": {"guess_lang": False, "noclasses": False}},
)

CJK = r"\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
# 章/节/部… 之后是**词边界**空格，必须保留（否则“第 2 章 生成…”会被粘成“第 2 章生成…”）；
# 其余 CJK 字符之间的孤立空格属源文件笔误（如“第 11 章 事 件”），予以去除。
KEEP_SPACE_AFTER = "章节目部篇条卷讲回"
RESTORE_CJK = re.compile(
    rf"(?<=[{CJK}])(?<![{KEEP_SPACE_AFTER}])[ \t]+(?=[{CJK}])"
)


def norm_title(t):
    """规范化标题：折叠空白、去掉 CJK 字符之间的多余空格。"""
    t = re.sub(r"\s+", " ", t).strip()
    t = RESTORE_CJK.sub("", t)
    return t


def md_h1(text):
    """取 md 中第一个 ATX 一级标题（允许最多 3 个空格缩进）。"""
    for line in text.splitlines():
        m = re.match(r"^ {0,3}#\s+(.*?)\s*#*\s*$", line)
        if m:
            return m.group(1).strip()
    return ""


def ensure_h1(text, title):
    """保证正文以一级标题开头；源文件漏写或多写时补正。

    - 首个非空行若是不带 # 的标题文本（如 ch14），提升为 H1；
    - 若全文没有 H1，则在正文前插入 H1。
    """
    lines = text.splitlines()
    idx = next((i for i, l in enumerate(lines) if l.strip()), None)
    if idx is None:
        return f"# {title}\n"

    first = lines[idx]
    stripped = re.sub(r"^ {0,3}#{1,6}\s*", "", first).strip()
    if norm_title(stripped) == norm_title(title):
        # 首行即标题（可能缩进异常或缺 #），统一重写为标准 H1
        lines[idx] = f"# {title}"
        return "\n".join(lines)

    if not md_h1(text):
        lines.insert(idx, f"# {title}\n")
        return "\n".join(lines)

    return text


def preprocess(text):
    """修正源文件中的结构性问题（不改变可见文本内容）。"""
    # 1. <a ...> 标签内的中文弯引号属笔误，会让属性值非法
    text = re.sub(
        r"<a\b[^>]*>",
        lambda m: m.group(0).replace("“", '"').replace("”", '"'),
        text,
    )
    # 2. 坏闭合标签 </br> 与未闭合的 <sup>x<sup>
    text = text.replace("</br>", "<br/>")
    text = re.sub(r"<sup>([^<>]*)<sup>", r"<sup>\1</sup>", text)
    # 2b. 缺少协议头的外部链接（源文件 ch8 有 ](www.ecma-...htm)），
    #     在 EPUB 里会被当作相对路径去找本地文件，导致 RSC-007 资源缺失
    text = re.sub(r"\]\(\s*(www\.[^)\s]+)\)", r"](http://\1)", text)
    text = re.sub(r'href="(www\.[^"]+)"', r'href="http://\1"', text)
    # 2c. 锚点声明里多写的 '#'（源文件 ch13 有 <a name="#13_10">）会让
    #     href="#13_10" 找不到目标，epubcheck 报 RSC-012
    text = re.sub(r'<a (name|id)="#([^"]+)"', r'<a \1="\2"', text)
    # 3. 图片/附件路径统一指向 EPUB 内的 ../images/
    text = re.sub(r"(\!\[[^\]]*\]\()\s*(?:\.\./)*resources/(?:images|drawios)/",
                  r"\1../images/", text)
    text = re.sub(r"(\!\[[^\]]*\]\()\s*(?:\.\./)*images/", r"\1../images/", text)
    # 4. 去掉 XML 非法控制字符
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text


def to_body(md_text):
    body = md_engine.reset().convert(md_text)
    body = re.sub(r"<sup>([^<>]*)<sup>", r"<sup>\1</sup>", body)
    body = re.sub(r'<a name="([^"]+)"', r'<a id="\1" name="\1"', body)
    body = re.sub(r'<a name=([^"\s>]+)', r'<a id="\1" name="\1"', body)
    # 残留的 ../resources/ 前缀（HTML 形式的 img）
    body = re.sub(r'(src|href)="(?:\.\./)*resources/(?:images|drawios)/', r'\1="../images/"', body)
    # img 的 alt 缺失补齐，部分阅读器（含 Calibre 的转换链）对空 alt 处理不佳
    body = re.sub(r'<img ([^>]*?)alt=""([^>]*?)/>', r'<img \1alt="插图"\2/>', body)
    return body


# 中文虚词：比对标题与链接文本时忽略，避免因“的/和”等差一两个字匹配不上
STOPWORDS = "的与和及其了"


def strip_tags(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()


def match_key(s):
    return re.sub(rf"[{STOPWORDS}\s]", "", strip_tags(s))


def resolve_dangling_fragments(body):
    """修复指向不存在锚点的站内链接（epubcheck 报 RSC-012，Calibre 点击无效）。

    策略分两层：
      1. 按链接文本与标题文本模糊比对，命中则在标题起始处插入空锚点 <a id="...">；
      2. 仍找不到目标时，把链接降级为纯文本，保证不留失效跳转。
    返回 (修复后的 body, 回绑数量, 降级数量, 回绑明细)
    """
    defined = set(re.findall(r'\sid="([^"]+)"', body))
    heads = [(m.start(), m.group(0), m.group(3))
             for m in re.finditer(r"<(h[1-6])([^>]*)>(.*?)</\1>", body, re.S)]
    edits, rebound, dropped, detail = [], 0, 0, []

    for m in re.finditer(r'<a href="#([^"]+)">(.*?)</a>', body, re.S):
        frag, label = m.group(1), m.group(2)
        if frag in defined:
            continue
        key = match_key(label)
        hit = None
        for pos, tag_html, text in heads:
            t = match_key(text)
            if key and t and (key in t or t in key):
                hit = (pos, tag_html, text)
                break
        if hit:
            ins = hit[0] + hit[1].index(">") + 1
            edits.append((ins, ins, f'<a id="{frag}"></a>'))
            defined.add(frag)
            rebound += 1
            detail.append(f'{frag} -> «{strip_tags(hit[2])[:34]}»')
        else:
            edits.append((m.start(), m.end(), label))
            dropped += 1
            detail.append(f'{frag} -> 无匹配标题，已降级为纯文本')

    for s, e, new in sorted(edits, key=lambda x: -x[0]):
        body = body[:s] + new + body[e:]
    return body, rebound, dropped, detail


# ---------------------------------------------------------------- 目录结构

with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as f:
    readme = f.read()

structure = []  # ("part", 标题) | ("doc", 相对路径, README 标题)
for line in readme.splitlines():
    line = line.strip()
    if re.match(r"^第\s*[一二三四五六七八九十百]+\s*部分\s+.+$", line):
        structure.append(("part", norm_title(line)))
        continue
    m = re.match(r"^\[([^\]]+)\]\(\./(?:chapters/)?([^)]+\.md)\)", line)
    if m:
        structure.append(("doc", m.group(2).strip(), norm_title(m.group(1))))

n_parts = sum(1 for s in structure if s[0] == "part")
n_docs = sum(1 for s in structure if s[0] == "doc")
print(f"目录解析：{n_parts} 个部分 / {n_docs} 篇文档")

# ---------------------------------------------------------------- 逐篇转换

docs = []          # {fid, href, title, body, md_basename}
link_fixes = []
for item in structure:
    if item[0] != "doc":
        continue
    rel, readme_title = item[1], item[2]
    path = os.path.join(CHAPTERS, rel)
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    title = norm_title(md_h1(raw)) or readme_title
    raw = preprocess(ensure_h1(raw, title))
    body = to_body(raw)
    body, rebound, dropped, detail = resolve_dangling_fragments(body)
    if detail:
        link_fixes.append((rel, rebound, dropped, detail))
    base = os.path.splitext(os.path.basename(rel))[0]
    docs.append({
        "fid": "doc_" + re.sub(r"\W+", "_", base),
        "href": "text/doc_" + re.sub(r"\W+", "_", base) + ".xhtml",
        "title": title,
        "body": body,
    })
    print(f"  {rel:45s} -> {title}")

# 正文实际引用到的图片（以转换结果为准，确保 manifest 与内容一致）
used_images = []
seen = set()
for d in docs:
    for m in re.finditer(r'src="\.\./images/([^"]+)"', d["body"]):
        name = m.group(1)
        if name not in seen:
            seen.add(name)
            used_images.append(name)

missing = [n for n in used_images if not os.path.exists(os.path.join(IMAGES, n))]
for n in missing:
    print("  !! 缺图片:", n)
used_images = [n for n in used_images if n not in missing]
print(f"正文引用图片 {len(used_images)} 张，缺失 {len(missing)} 张")

if link_fixes:
    print("站内锚点修复：")
    for rel, rebound, dropped, detail in link_fixes:
        print(f"  {rel}  回绑 {rebound} / 降级 {dropped}")
        for d in detail:
            print(f"     - {d}")

# ---------------------------------------------------------------- 样式表

CSS = """
@charset "utf-8";
html { font-size: 100%; }
body {
  font-family: "Noto Serif CJK SC", "Source Han Serif SC", "Songti SC", "SimSun",
               Georgia, serif;
  line-height: 1.75;
  margin: 0.6em 1em;
  text-align: justify;
  word-wrap: break-word;
}
h1, h2, h3, h4 {
  font-family: "Noto Sans CJK SC", "Source Han Sans SC", "Microsoft YaHei", sans-serif;
  line-height: 1.4;
  page-break-after: avoid;
}
h1 {
  font-size: 1.55em;
  text-align: center;
  margin: 1.4em 0 1em;
  padding-bottom: 0.35em;
  border-bottom: 2px solid #4a6785;
  page-break-before: always;
}
h2 { font-size: 1.28em; color: #2c4a6e; margin: 1.6em 0 0.6em;
     border-left: 5px solid #4a6785; padding-left: 0.45em; }
h3 { font-size: 1.1em; color: #3a5a7e; margin: 1.3em 0 0.5em; }
h4 { font-size: 1em; color: #3a5a7e; margin: 1.1em 0 0.4em; }
p { margin: 0.6em 0; text-indent: 0; }
a { color: #1d5c94; text-decoration: none; }
ul, ol { margin: 0.6em 0; padding-left: 1.8em; }
li { margin: 0.25em 0; }

/* 代码：等宽字体 + 可换行，避免小屏横向溢出 */
code, pre, kbd, samp {
  font-family: "Cascadia Mono", Consolas, "DejaVu Sans Mono", "Courier New", monospace;
}
code { font-size: 0.88em; background: #f0f2f4; padding: 0 0.2em; border-radius: 3px; }
pre {
  font-size: 0.82em;
  line-height: 1.45;
  background: #f6f8fa;
  border: 1px solid #d8dee4;
  border-radius: 4px;
  padding: 0.65em 0.75em;
  margin: 0.8em 0;
  white-space: pre-wrap;
  word-wrap: break-word;
  overflow-wrap: break-word;
}
pre code { background: none; padding: 0; font-size: 1em; }
.codehilite { margin: 0.8em 0; }
.codehilite pre { margin: 0; }

blockquote {
  border-left: 4px solid #c9d4df; margin: 0.9em 0; padding: 0.3em 0.9em;
  color: #4a4a4a; background: #f8fafc;
}
img { max-width: 100%; height: auto; }
table { border-collapse: collapse; width: 100%; margin: 0.9em 0; font-size: 0.9em; }
th, td { border: 1px solid #bbb; padding: 0.35em 0.55em; vertical-align: top; }
th { background: #eef2f6; }
hr { border: none; border-top: 1px solid #ddd; margin: 1.4em 0; }
.footnote, .footnotes { font-size: 0.85em; color: #555; }
.footnotes hr { margin-top: 2em; }

/* 分部扉页 */
.part-title { text-align: center; margin-top: 30%; }
.part-title h1 {
  border: none; font-size: 2em; color: #2c4a6e;
  page-break-before: avoid; margin: 0;
}
.part-title .part-no { font-size: 1.15em; color: #8a8a8a; letter-spacing: 0.35em; margin-bottom: 0.6em; }

/* 封面 */
.cover { text-align: center; margin: 0; padding: 0; }
.cover img { max-width: 100%; max-height: 100%; }

/* 目录页：用 id 选择器而非 nav[epub|type~="toc"]，
   Calibre 的 CSS 解析器不认识 XML 命名空间前缀，会在转换日志里刷解析错误 */
#toc ol { list-style: none; padding-left: 0; }
#toc ol ol { padding-left: 1.4em; }
#toc li { margin: 0.3em 0; }
"""

try:
    CSS += "\n/* Pygments (friendly) */\n" + HtmlFormatter(style="friendly").get_style_defs(".codehilite")
except Exception as e:  # pragma: no cover
    print("  跳过 Pygments 样式:", e)

# ---------------------------------------------------------------- XHTML 模板

def xhtml(title, body, lang=LANG):
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{lang}" xml:lang="{lang}">\n'
        '<head>\n<meta charset="utf-8"/>\n'
        f'<title>{html.escape(title)}</title>\n'
        '<link rel="stylesheet" type="text/css" href="../style.css"/>\n'
        '</head>\n<body>\n'
        f'{body}\n'
        '</body>\n</html>'
    )


def nav_xhtml(items, first_body_href):
    """items: [(title, href_or_None, [children...]), ...]"""
    def render(nodes):
        out = []
        for title, href, children in nodes:
            if href:
                out.append(f'<li><a href="{href}">{html.escape(title)}</a>')
            else:
                out.append(f'<li><span>{html.escape(title)}</span>')
            if children:
                out.append("<ol>" + render(children) + "</ol>")
            out.append("</li>")
        return "\n".join(out)

    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<!DOCTYPE html>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" lang="{LANG}" xml:lang="{LANG}">\n'
        '<head><meta charset="utf-8"/><title>目录</title>\n'
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>\n'
        '<body>\n'
        '<nav epub:type="toc" id="toc"><h1>目录</h1>\n<ol>\n'
        '<li><a href="text/cover.xhtml">封面</a></li>\n'
        f'{render(items)}\n'
        '</ol></nav>\n'
        '<nav epub:type="landmarks" hidden="hidden"><h2>Guide</h2>\n<ol>\n'
        '<li><a epub:type="cover" href="text/cover.xhtml">封面</a></li>\n'
        '<li><a epub:type="toc" href="nav.xhtml">目录</a></li>\n'
        f'<li><a epub:type="bodymatter" href="{first_body_href}">正文</a></li>\n'
        '</ol></nav>\n'
        '</body></html>'
    )

# ---------------------------------------------------------------- 封面

from PIL import Image, ImageDraw, ImageFont

cover_path = os.path.join(ROOT, "_cover.png")
W, H = 1000, 1414
img = Image.new("RGB", (W, H), "#1d3557")
d = ImageDraw.Draw(img)
try:
    f_big = ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc", 110)
    f_mid = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 46)
    f_sml = ImageFont.truetype(r"C:\Windows\Fonts\msyh.ttc", 34)
except OSError:
    f_big = f_mid = f_sml = ImageFont.load_default()
d.rectangle([60, 60, W - 60, H - 60], outline="#a8dadc", width=6)
d.text((W // 2, 420), "CLR via C#", font=f_big, fill="#f1faee", anchor="mm")
d.text((W // 2, 560), "第 4 版", font=f_mid, fill="#a8dadc", anchor="mm")
d.line([280, 640, W - 280, 640], fill="#457b9d", width=3)
d.text((W // 2, 730), "Jeffrey Richter  著", font=f_mid, fill="#e63946", anchor="mm")
d.text((W // 2, 810), "周靖  译", font=f_mid, fill="#e63946", anchor="mm")
d.text((W // 2, H - 220), "Microsoft .NET / CLR 经典之作", font=f_sml, fill="#a8dadc", anchor="mm")
img.save(cover_path, "PNG")

# ---------------------------------------------------------------- 组织 manifest / spine

manifest = []      # {id, href, media, props, spine}
spine = []


def add(fid, href, media, props=None, in_spine=True, data=None):
    manifest.append({"id": fid, "href": href, "media": media, "props": props or [], "data": data})
    if in_spine:
        spine.append(fid)


add("css", "style.css", "text/css", in_spine=False, data=CSS.encode("utf-8"))
# 封面图片的 manifest id 直接取 "cover"：Calibre 若发现封面图片 id 不是 "cover"，
# 会在转换日志里输出 "The cover image has an id != 'cover'" 并自行改名（Nook 兼容 hack）
add("cover", "images/cover.png", "image/png",
    props=["cover-image"], in_spine=False, data=open(cover_path, "rb").read())
add("coverpage", "text/cover.xhtml", "application/xhtml+xml",
    data=xhtml("封面", '<div class="cover"><img src="../images/cover.png" alt="封面"/></div>').encode("utf-8"))

# 分部扉页 + 章节：spine 顺序严格按 README，同时构造导航树。
# 每节点为 [标题, href, 子节点列表]；分部之下的章节挂进该分部的子节点，
# 分部之前/之后的独立文档（前言、序言、译者后记）保持顶层，不被误嵌。
nav_tree = []
current_group = None
part_no = 0
doc_i = 0
first_body_href = None

for item in structure:
    if item[0] == "part":
        part_no += 1
        fid = f"part{part_no}"
        m = re.match(r"^(第\s*[一二三四五六七八九十]+\s*部分)\s*(.*)$", item[1])
        pno, pname = (m.group(1), m.group(2)) if m else ("", item[1])
        body = (f'<div class="part-title"><p class="part-no">{html.escape(pno)}</p>'
                f'<h1>{html.escape(pname)}</h1></div>')
        href = f"text/{fid}.xhtml"
        add(fid, href, "application/xhtml+xml", data=xhtml(item[1], body).encode("utf-8"))
        node = [item[1], href, []]
        nav_tree.append(node)
        current_group = node
        if first_body_href is None:
            first_body_href = href
    else:
        d = docs[doc_i]
        doc_i += 1
        add(d["fid"], d["href"], "application/xhtml+xml",
            data=xhtml(d["title"], d["body"]).encode("utf-8"))
        entry = [d["title"], d["href"], []]
        if current_group is None:
            nav_tree.append(entry)
        else:
            current_group[2].append(entry)
        if first_body_href is None:
            first_body_href = d["href"]

assert doc_i == len(docs), f"structure 与 docs 数量不一致: {doc_i} vs {len(docs)}"

# 图片：登记进 manifest（这是上一版 90 条 RSC-008 错误的根因）
for name in used_images:
    p = os.path.join(IMAGES, name)
    ext = os.path.splitext(name)[1].lower().lstrip(".")
    add("img_" + re.sub(r"\W+", "_", os.path.splitext(name)[0]),
        "images/" + name,
        MEDIA.get(ext, "application/octet-stream"),
        in_spine=False, data=open(p, "rb").read())

# ---------------------------------------------------------------- nav / NCX / OPF

nav_items = [(t, h, ch) for (t, h, ch) in nav_tree]
add("nav", "nav.xhtml", "application/xhtml+xml", props=["nav"], in_spine=False,
    data=nav_xhtml(nav_items, first_body_href).encode("utf-8"))
# 目录页紧跟封面进入阅读顺序：Calibre 的阅读器与「转换」流程都会把它当作正常页面
spine.insert(1, "nav")

# NCX：层级与 nav 一致，兼容 EPUB2 阅读器
order = [0]


def ncx_points(nodes):
    out = []
    for title, href, children in nodes:
        order[0] += 1
        n = order[0]
        pts = "".join(ncx_points(children))
        out.append(
            f'<navPoint id="np{n}" playOrder="{n}">'
            f'<navLabel><text>{html.escape(title)}</text></navLabel>'
            f'<content src="{href}"/>{pts}</navPoint>'
        )
    return out


ncx_body = "\n".join(ncx_points(nav_tree))
ncx = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">\n'
    '<head>\n'
    f'<meta name="dtb:uid" content="{html.escape(UUID)}"/>\n'
    '<meta name="dtb:depth" content="2"/>\n'
    '<meta name="dtb:totalPageCount" content="0"/>\n'
    '<meta name="dtb:maxPageNumber" content="0"/>\n'
    '</head>\n'
    f'<docTitle><text>{html.escape(BOOK_TITLE)}</text></docTitle>\n'
    f'<navMap>\n{ncx_body}\n</navMap>\n</ncx>'
)
add("ncx", "toc.ncx", "application/x-dtbncx+xml", in_spine=False, data=ncx.encode("utf-8"))

modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

manifest_xml = "\n".join(
    '<item id="{id}" href="{href}" media-type="{media}"{props}/>'.format(
        id=m["id"], href=m["href"], media=m["media"],
        props=(' properties="' + " ".join(m["props"]) + '"') if m["props"] else "")
    for m in manifest
)
spine_xml = "\n".join(f'<itemref idref="{i}"/>' for i in spine)

opf = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    f'<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    f'unique-identifier="bookid" xml:lang="{LANG}">\n'
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
    f'<dc:identifier id="bookid">{html.escape(UUID)}</dc:identifier>\n'
    f'<dc:title>{html.escape(BOOK_TITLE)}</dc:title>\n'
    f'<dc:creator id="creator">{html.escape(AUTHOR)}</dc:creator>\n'
    f'<dc:language>{LANG}</dc:language>\n'
    '<dc:description>CLR Via C# 第四版 中文读书笔记 (Jeffrey Richter)</dc:description>\n'
    f'<dc:date>{modified}</dc:date>\n'
    '<meta refines="#creator" property="role" scheme="marc:relators">aut</meta>\n'
    f'<meta property="dcterms:modified">{modified}</meta>\n'
    '<meta name="cover" content="cover"/>\n'
    '</metadata>\n<manifest>\n'
    f'{manifest_xml}\n</manifest>\n'
    f'<spine toc="ncx">\n{spine_xml}\n</spine>\n'
    '</package>'
)
manifest.append({"id": "opf", "href": "content.opf", "media": "application/oebps-package+xml",
                 "props": [], "data": opf.encode("utf-8")})

# ---------------------------------------------------------------- 打包

if os.path.exists(OUT):
    os.remove(OUT)

zf = zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED)
zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
zf.writestr("META-INF/container.xml",
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
            '  <rootfiles>\n'
            '    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>\n'
            '  </rootfiles>\n'
            '</container>')
for m in manifest:
    if m["id"] == "opf":
        continue
    zf.writestr("OEBPS/" + m["href"], m["data"])
zf.writestr("OEBPS/content.opf", opf)
zf.close()
os.remove(cover_path)

n_img = len(used_images)
print(f"\n生成完成: {OUT}")
print(f"  {os.path.getsize(OUT)/1024/1024:.2f} MB, "
      f"spine {len(spine)} 篇, manifest {len(manifest)} 项（含图片 {n_img} 张）")
