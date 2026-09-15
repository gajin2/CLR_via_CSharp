# -*- coding: utf-8 -*-
"""EPUB3 结构自检：不依赖外部工具，逐项检查 Calibre 会报错的硬性约束。

用法: python validate_epub.py [xxx.epub]
"""
import os, re, sys, zipfile
from xml.etree import ElementTree as ET

ROOT = os.path.dirname(os.path.abspath(__file__))
EPUB = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "CLR_via_CSharp_4th_Edition.epub")

CNT = "{urn:oasis:names:tc:opendocument:xmlns:container}"
OPF = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"
XH = "{http://www.w3.org/1999/xhtml}"

errors, warns, notes = [], [], []


def err(m):
    errors.append(m)


def warn(m):
    warns.append(m)


def note(m):
    notes.append(m)


z = zipfile.ZipFile(EPUB)
names = z.namelist()
infos = {i.filename: i for i in z.infolist()}

# 1. mimetype: 必须是第一个条目且不压缩
if names[0] != "mimetype":
    err(f"mimetype 不是第一个条目（实际: {names[0]}）")
else:
    if infos["mimetype"].compress_type != zipfile.ZIP_STORED:
        err("mimetype 被压缩了，必须 STORED（不压缩）")
    if z.read("mimetype") != b"application/epub+zip":
        err("mimetype 内容不是 application/epub+zip")

# 2. container.xml
if "META-INF/container.xml" not in names:
    err("缺少 META-INF/container.xml")
    print("\n".join(errors))
    sys.exit(1)
croot = ET.fromstring(z.read("META-INF/container.xml"))
rf = croot.find(f"{CNT}rootfiles/{CNT}rootfile")
if rf is None:
    err("container.xml 无 rootfile")
    print("\n".join(errors))
    sys.exit(1)
opf_path = rf.get("full-path")
if opf_path not in names:
    err(f"OPF 不存在: {opf_path}")
    print("\n".join(errors))
    sys.exit(1)
base = os.path.dirname(opf_path)

# 3. OPF
oroot = ET.fromstring(z.read(opf_path))
md = oroot.find(f"{OPF}metadata")
mid = oroot.find(f"{OPF}manifest")
sp = oroot.find(f"{OPF}spine")
if md is None or mid is None or sp is None:
    err("OPF 缺少 metadata / manifest / spine")
    print("\n".join(errors))
    sys.exit(1)

uid = oroot.get("unique-identifier")
if uid and md.find(f"{DC}identifier[@id='{uid}']") is None:
    err(f"spine unique-identifier '{uid}' 在 metadata 中找不到对应 dc:identifier")
for tag in ("title", "language", "identifier"):
    if md.find(f"{DC}{tag}") is None:
        err(f"metadata 缺少 dc:{tag}")

items = {}
for it in mid.findall(f"{OPF}item"):
    iid, href = it.get("id"), it.get("href")
    media = it.get("media-type")
    props = (it.get("properties") or "").split()
    if iid in items:
        err(f"manifest id 重复: {iid}")
    items[iid] = {"href": href, "media": media, "props": props}
    full = os.path.normpath(os.path.join(base, href)).replace("\\", "/")
    if full not in names:
        err(f"manifest 指向的文件不存在: {href} -> {full}")

# 反向：zip 里的资源是否都在 manifest（EPUB3 要求）
manifest_full = {
    os.path.normpath(os.path.join(base, v["href"])).replace("\\", "/")
    for v in items.values()
}
for n in names:
    if n == "mimetype" or n.startswith("META-INF/"):
        continue
    if n == opf_path:            # OPF 自身不登记在自己里面
        continue
    if n not in manifest_full:
        err(f"zip 内文件未登记进 manifest: {n}")

# 4. spine
idrefs = [ir.get("idref") for ir in sp.findall(f"{OPF}itemref")]
if not idrefs:
    err("spine 为空")
for i in idrefs:
    if i not in items:
        err(f"spine idref 未在 manifest 中定义: {i}")
if len(idrefs) != len(set(idrefs)):
    warn("spine 中存在重复 idref")
navprop = [k for k, v in items.items() if "nav" in v["props"]]
if not navprop:
    err("没有任何 manifest item 带 properties=\"nav\"（EPUB3 必需）")
else:
    note(f"nav 文档: {[items[k]['href'] for k in navprop]}")

# 5. 每个 XHTML: XML 良构 + 内部引用可解析
img_refs = {}
for iid, v in items.items():
    if v["media"] not in ("application/xhtml+xml", "text/html"):
        continue
    full = os.path.normpath(os.path.join(base, v["href"])).replace("\\", "/")
    data = z.read(full)
    try:
        root = ET.fromstring(data)
    except ET.ParseError as e:
        err(f"[{full}] XML 不合法: {e}")
        continue
    # XML 声明必须存在（EPUB3 要求）
    if not data.lstrip()[:5].lower().startswith(b"<?xml"):
        warn(f"[{full}] 缺少 XML 声明")
    d = os.path.dirname(full)
    for el in root.iter():
        attr = None
        if el.tag == f"{XH}img":
            attr = "src"
        elif el.tag == f"{XH}a":
            attr = "href"
        elif el.tag == f"{XH}link":
            attr = "href"
        if not attr:
            continue
        tgt = el.get(attr)
        if not tgt or tgt.startswith(("http:", "https:", "mailto:", "#", "data:")):
            continue
        path, _, frag = tgt.partition("#")
        if not path:
            continue
        tfull = os.path.normpath(os.path.join(d, path)).replace("\\", "/")
        if tfull not in names:
            err(f"[{full}] {attr}=\"{tgt}\" 指向不存在的文件")
        else:
            if tfull not in manifest_full:
                err(f"[{full}] {attr}=\"{tgt}\" 目标未登记 manifest")
            if el.tag == f"{XH}img":
                img_refs.setdefault(tfull, 0)
                img_refs[tfull] += 1
            if frag and tfull.endswith(".xhtml"):
                try:
                    troot = ET.fromstring(z.read(tfull))
                except ET.ParseError:
                    continue
                ids = {e.get("id") for e in troot.iter() if e.get("id")}
                ids |= {e.get("name") for e in troot.iter() if e.get("name")}
                if frag not in ids:
                    warn(f"[{full}] 锚点 #{frag} 在 {os.path.basename(tfull)} 中找不到 id")

# 6. NCX
ncx = [v["href"] for v in items.values() if v["media"] == "application/x-dtbncx+xml"]
if ncx:
    nfull = os.path.normpath(os.path.join(base, ncx[0])).replace("\\", "/")
    try:
        nroot = ET.fromstring(z.read(nfull))
        pts = list(nroot.iter("{http://www.daisy.org/z3986/2005/ncx/}content"))
        note(f"NCX navPoint: {len(pts)} 条")
    except ET.ParseError as e:
        err(f"NCX XML 不合法: {e}")
    toc_attr = sp.get("toc")
    if not toc_attr or toc_attr not in items:
        warn("spine 未通过 toc 属性引用 NCX（不影响 EPUB3 阅读，但影响 EPUB2 兼容）")

# 7. 图片体积
total_img = sum(infos[n].file_size for n in names if n.startswith(os.path.join(base, "images")) or "/images/" in n)
note(f"内嵌图片 {len(img_refs)} 个被正文引用, 合计 {total_img/1024/1024:.1f} MB")

print(f"文件: {EPUB}")
print(f"大小: {os.path.getsize(EPUB)/1024/1024:.2f} MB, 条目 {len(names)}")
print(f"正文文档 {len(idrefs)} 篇 (spine), manifest {len(items)} 项\n")
for t, lst in (("错误", errors), ("警告", warns)):
    if lst:
        print(f"--- {t} ({len(lst)}) ---")
        for m in lst[:40]:
            print("  " + m)
        if len(lst) > 40:
            print(f"  ... 另有 {len(lst)-40} 条")
print("--- 信息 ---")
for m in notes:
    print("  " + m)
print()
print("结果:", "通过（无结构性错误）" if not errors else f"不通过：{len(errors)} 个错误")
sys.exit(0 if not errors else 1)
