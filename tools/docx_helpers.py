"""產生 Word 文件用的底層工具：字體、七層標題編號、目錄欄位、圖表標號（SEQ）、表格、備註框、頁碼。
這些都是依「文件格式規則」寫成的可重用函式，之後別的專案的報告也可以直接拿去用。

規則重點
- 中文標楷體、英文與數字 Times New Roman：寫在 docDefaults 與每個樣式（程式碼樣式除外，用等寬字體）。
- 標題是 Word 真正的「多層次清單編號」並連結到標題 1～7，不是手打文字：
  壹、 / 一、 / （一） / 1. / (1) / a. / (a)
- 目錄用 TOC 欄位，圖表標號用 SEQ 欄位；不用空白段落撐版面；備註框用段落底色與邊框。
- 內文引用章節用章節名稱，不寫會變動的編號。
"""
from __future__ import annotations

from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement, parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Pt, RGBColor

EAST, LATIN, MONO = "標楷體", "Times New Roman", "Consolas"
NUM_ID = 90          # 自訂多層次清單的 numId / abstractNumId

# (numFmt, lvlText) 依使用者規定的七層格式
LEVELS = [("ideographLegalTraditional", "%1、"), ("taiwaneseCountingThousand", "%2、"),
          ("taiwaneseCountingThousand", "（%3）"), ("decimal", "%4."), ("decimal", "(%5)"),
          ("lowerLetter", "%6."), ("lowerLetter", "(%7)")]


# --------------------------------------------------------------------------- 字體
def set_fonts(rpr, east: str = EAST, latin: str = LATIN) -> None:
    """在 rPr 設定 rFonts：英數用 latin，中文用 east，並移除主題字體屬性（否則會蓋掉我們的設定）。"""
    rf = rpr.find(qn("w:rFonts"))
    if rf is None:
        rf = OxmlElement("w:rFonts")
        rpr.insert(0, rf)
    for a in ("asciiTheme", "hAnsiTheme", "eastAsiaTheme", "cstheme"):
        if rf.get(qn(f"w:{a}")) is not None:
            del rf.attrib[qn(f"w:{a}")]
    rf.set(qn("w:ascii"), latin)
    rf.set(qn("w:hAnsi"), latin)
    rf.set(qn("w:cs"), latin)
    rf.set(qn("w:eastAsia"), east)


def style_fonts(style, size: float | None = None, bold: bool | None = None, color: str | None = "000000",
                mono: bool = False) -> None:
    rpr = style.element.get_or_add_rPr()
    set_fonts(rpr, east=EAST, latin=MONO if mono else LATIN)
    if size:
        style.font.size = Pt(size)
    if bold is not None:
        style.font.bold = bold
    if color:
        style.font.color.rgb = RGBColor.from_string(color)


def set_doc_defaults(doc) -> None:
    styles = doc.styles.element
    dd = styles.find(qn("w:docDefaults"))
    rpr = dd.find(qn("w:rPrDefault")).find(qn("w:rPr"))
    set_fonts(rpr)
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.append(lang)
    lang.set(qn("w:val"), "en-US")
    lang.set(qn("w:eastAsia"), "zh-TW")


# --------------------------------------------------------------------------- 標題編號
def install_heading_numbering(doc) -> None:
    """建立 7 層多層次清單，並連結到標題 1～7 樣式（樣式內帶 numPr，段落就自動編號）。"""
    numbering = doc.part.numbering_part.numbering_definitions._numbering
    lvls = []
    for i, (fmt, text) in enumerate(LEVELS):
        lvls.append(
            f'<w:lvl w:ilvl="{i}"><w:start w:val="1"/><w:numFmt w:val="{fmt}"/>'
            f'<w:pStyle w:val="Heading{i + 1}"/><w:suff w:val="space"/><w:lvlText w:val="{text}"/>'
            f'<w:lvlJc w:val="left"/><w:pPr><w:ind w:left="0" w:firstLine="0"/></w:pPr></w:lvl>')
    absn = parse_xml(f'<w:abstractNum {nsdecls("w")} w:abstractNumId="{NUM_ID}">'
                     f'<w:multiLevelType w:val="multilevel"/>{"".join(lvls)}</w:abstractNum>')
    first_num = numbering.find(qn("w:num"))
    if first_num is not None:
        first_num.addprevious(absn)          # abstractNum 必須在所有 num 之前
    else:
        numbering.append(absn)
    numbering.append(parse_xml(f'<w:num {nsdecls("w")} w:numId="{NUM_ID}"><w:abstractNumId w:val="{NUM_ID}"/></w:num>'))
    sizes = [18, 16, 14, 13, 12, 12, 12]
    for i in range(7):
        st = doc.styles[f"Heading {i + 1}"]
        style_fonts(st, size=sizes[i], bold=True)
        ppr = st.element.get_or_add_pPr()
        num = ppr.get_or_add_numPr()
        num.get_or_add_ilvl().val = i
        num.get_or_add_numId().val = NUM_ID
        st.paragraph_format.space_before = Pt(18 if i == 0 else 12)
        st.paragraph_format.space_after = Pt(6)
        st.paragraph_format.keep_with_next = True
        st.paragraph_format.left_indent = Pt(0)
        st.paragraph_format.first_line_indent = Pt(0)
    doc.styles["Heading 1"].paragraph_format.page_break_before = True


def unnumbered_heading(doc, text: str, level: int = 1):
    """附錄等不編號的標題：用標題樣式但把這個段落的 numId 設為 0。"""
    p = doc.add_paragraph(text, style=f"Heading {level}")
    p._p.get_or_add_pPr().get_or_add_numPr().get_or_add_numId().val = 0
    return p


# --------------------------------------------------------------------------- 欄位
def _fld(run, kind: str) -> None:
    c = OxmlElement("w:fldChar")
    c.set(qn("w:fldCharType"), kind)
    run._r.append(c)


def add_field(paragraph, instr: str, placeholder: str = "1") -> None:
    """插入複雜欄位（begin / instr / separate / 結果 / end），Word 開啟後可更新。"""
    r1 = paragraph.add_run()
    _fld(r1, "begin")
    r2 = paragraph.add_run()
    it = OxmlElement("w:instrText")
    it.set(qn("xml:space"), "preserve")
    it.text = f" {instr} "
    r2._r.append(it)
    r3 = paragraph.add_run()
    _fld(r3, "separate")
    paragraph.add_run(placeholder)
    r5 = paragraph.add_run()
    _fld(r5, "end")


def add_toc(doc, title: str = "目錄") -> None:
    p = doc.add_paragraph(title, style="TOC Heading") if "TOC Heading" in [s.name for s in doc.styles] else doc.add_paragraph(title)
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.page_break_before = True
    for r in p.runs:
        r.font.size = Pt(18)
        r.font.bold = True
        set_fonts(r._r.get_or_add_rPr())
    tp = doc.add_paragraph()
    add_field(tp, 'TOC \\o "1-3" \\h \\z \\u', "（請在 Word 按 F9 更新目錄）")


def set_update_fields(doc) -> None:
    settings = doc.settings.element
    if settings.find(qn("w:updateFields")) is None:
        uf = OxmlElement("w:updateFields")
        uf.set(qn("w:val"), "true")
        settings.append(uf)


def add_page_number_footer(section, prefix: str = "") -> None:
    p = section.footer.paragraphs[0]
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if prefix:
        p.add_run(prefix)
    add_field(p, "PAGE", "1")


# --------------------------------------------------------------------------- 區塊
def body_paragraph(doc, text: str, indent: bool = True):
    p = doc.add_paragraph()
    _runs(p, text)
    pf = p.paragraph_format
    pf.space_after = Pt(6)
    pf.line_spacing = 1.3
    if indent:
        pf.first_line_indent = Pt(24)
    return p


def _runs(p, text: str) -> None:
    """支援 `等寬` 行內程式碼標記（反引號包起來的片段用等寬字體）。"""
    parts = text.split("`")
    for i, part in enumerate(parts):
        if not part:
            continue
        r = p.add_run(part)
        if i % 2 == 1:
            set_fonts(r._r.get_or_add_rPr(), east=EAST, latin=MONO)
            r.font.size = Pt(10.5)


def bullets(doc, items: list[str]) -> None:
    for it in items:
        p = doc.add_paragraph(style="List Bullet")
        _runs(p, it)
        p.paragraph_format.space_after = Pt(3)
        p.paragraph_format.line_spacing = 1.25


def note_box(doc, text: str, label: str = "注意") -> None:
    """備註框：用段落底色與左邊框，不用單格表格。"""
    p = doc.add_paragraph()
    r = p.add_run(f"{label}：")
    r.bold = True
    _runs(p, text)
    ppr = p._p.get_or_add_pPr()
    ppr.append(parse_xml(f'<w:pBdr {nsdecls("w")}><w:left w:val="single" w:sz="24" w:space="6" w:color="7F7F7F"/></w:pBdr>'))
    ppr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="F2F2F2"/>'))
    p.paragraph_format.left_indent = Cm(0.4)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(8)
    p.paragraph_format.line_spacing = 1.25


def code_block(doc, text: str) -> None:
    for line in text.strip("\n").split("\n"):
        p = doc.add_paragraph()
        r = p.add_run(line if line else " ")
        set_fonts(r._r.get_or_add_rPr(), east=EAST, latin=MONO)
        r.font.size = Pt(9.5)
        ppr = p._p.get_or_add_pPr()
        ppr.append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="F7F7F7"/>'))
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.left_indent = Cm(0.5)
        p.paragraph_format.line_spacing = 1.1


class Counter:
    figure = 0
    table = 0


def caption(doc, kind: str, text: str, above: bool = False):
    """圖／表標號：「圖 N」的 N 是 SEQ 欄位，Word 重排或插入新圖時會自動更新。"""
    p = doc.add_paragraph(style="Caption")
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"{kind} ")
    n = (Counter.figure + 1, "圖") if kind == "圖" else (Counter.table + 1, "表")
    add_field(p, f"SEQ {kind} \\* ARABIC", str(n[0]))
    if kind == "圖":
        Counter.figure += 1
    else:
        Counter.table += 1
    p.add_run(" " + text)                              # 不用全形空白（會顯示成方框），一律半形
    p.paragraph_format.keep_with_next = above
    p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.space_before = Pt(4)
    return p


def figure(doc, path: Path, text: str, max_w: float = 15.5, max_h: float = 17.0) -> None:
    from PIL import Image
    with Image.open(path) as im:
        w, h = im.size
    width = min(max_w, max_h * w / h)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.keep_with_next = True
    p.add_run().add_picture(str(path), width=Cm(width))
    caption(doc, "圖", text)


def table(doc, text: str, header: list[str], rows: list[list[str]], widths: list[float] | None = None,
          font: float = 10.5) -> None:
    caption(doc, "表", text, above=True)
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        r = c.paragraphs[0].add_run(h)
        r.bold = True
        r.font.size = Pt(font)
        c._tc.get_or_add_tcPr().append(parse_xml(f'<w:shd {nsdecls("w")} w:val="clear" w:color="auto" w:fill="E7E6E6"/>'))
    trpr = t.rows[0]._tr.get_or_add_trPr()
    trpr.append(OxmlElement("w:tblHeader"))                       # 跨頁時表頭重複
    for row in rows:
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ""
            para = cells[i].paragraphs[0]
            _runs(para, str(v))
            for r in para.runs:
                r.font.size = Pt(font)
            para.paragraph_format.space_after = Pt(0)
            para.paragraph_format.line_spacing = 1.15
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    for row in t.rows:                                             # 列不跨頁斷開
        row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))


def setup_document(title: str) -> Document:
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Cm(21.0), Cm(29.7)
    sec.left_margin = sec.right_margin = Cm(2.5)
    sec.top_margin = sec.bottom_margin = Cm(2.5)
    set_doc_defaults(doc)
    st = doc.styles
    style_fonts(st["Normal"], size=12)
    st["Normal"].paragraph_format.space_after = Pt(6)
    for name, size, bold in (("Caption", 10.5, False), ("List Bullet", 12, None), ("Title", 28, True)):
        style_fonts(st[name], size=size, bold=bold)
    style_fonts(st["Table Grid"], size=10.5, color=None) if "Table Grid" in [s.name for s in st] else None
    install_heading_numbering(doc)
    doc.core_properties.title = title
    doc.core_properties.author = "AutoMAC 專案"
    doc.core_properties.comments = ""
    return doc


__all__ = [n for n in dir() if not n.startswith("_")] + ["WD_SECTION"]


def space_after_tables(doc, before_pt: float = 6.0) -> int:
    """表格後面的段落加「段落前間距」，取代用空白段落撐距離（使用者規則：不用空白段落撐版面）。
    回傳處理幾個表格。表格後面不是段落（接另一個表格或文件結尾）時丟例外，因為 Word 會把相鄰表格合併。"""
    from docx.oxml.ns import qn
    n = 0
    for tbl in doc.element.body.iter(qn("w:tbl")):
        if tbl.getparent() is not doc.element.body:
            continue                                               # 巢狀表格不處理
        nxt = tbl.getnext()
        if nxt is None or nxt.tag != qn("w:p"):
            raise ValueError(f"表格後面必須接段落（目前接的是 {None if nxt is None else nxt.tag.split('}')[1]}），否則相鄰表格會被 Word 合併")
        ppr = nxt.get_or_add_pPr()
        sp = ppr.find(qn("w:spacing"))
        if sp is None:
            sp = OxmlElement("w:spacing")
            ppr.append(sp)
        sp.set(qn("w:before"), str(int(before_pt * 20)))
        n += 1
    return n
