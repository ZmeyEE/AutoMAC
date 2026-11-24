"""直接讀 docx 的 XML 做結構檢查（不需要 Word）：字體、七層編號是否連到標題樣式、TOC 欄位、全形空白、空段落、錯字。
用法：python tools/verify_docx_struct.py 文件/AutoMAC開發文件_2.docx
退出碼 0 = 全部通過；有問題會逐項列出。這裡看的是檔案結構，實際排版（跑版、分頁）仍要用 Word 開啟或 verify_docx.ps1 的 PDF 目視確認。"""
import re
import sys
import zipfile

NS_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def main(path: str) -> int:
    z = zipfile.ZipFile(path)
    doc = z.read("word/document.xml").decode("utf-8")
    sty = z.read("word/styles.xml").decode("utf-8")
    num = z.read("word/numbering.xml").decode("utf-8")
    problems: list[str] = []
    ok = lambda msg: print("通過：" + msg)   # noqa: E731

    # 1. 字體：docDefaults 與每個內文樣式
    defaults = re.search(r"<w:docDefaults>.*?</w:docDefaults>", sty, re.S)
    fonts = re.findall(r'<w:rFonts[^>]*>', defaults.group(0)) if defaults else []
    if fonts and 'w:eastAsia="標楷體"' in fonts[0] and 'w:ascii="Times New Roman"' in fonts[0]:
        ok("docDefaults 字體 = 標楷體 + Times New Roman")
    else:
        problems.append(f"docDefaults 字體不對：{fonts[:1]}")
    used = set(re.findall(r'<w:(?:pStyle|rStyle|tblStyle) w:val="([^"]+)"', doc)) | {f"Heading{i}" for i in range(1, 8)}
    bad = [m for m in re.findall(r'<w:style [^>]*w:styleId="([^"]+)"[^>]*>(.*?)</w:style>', sty, re.S)
           if m[0] in used and "<w:rFonts" in m[1] and 'w:eastAsia="標楷體"' not in m[1] and not re.search(r"Code|Consolas|程式", m[0])]
    if bad:
        problems.append("這些樣式的東亞字體不是標楷體：" + ", ".join(b[0] for b in bad[:8]))
    else:
        ok("所有指定字體的樣式，中文都是標楷體（程式碼樣式除外）")

    # 2. 七層編號：標題 1~7 都連到同一個多層次清單
    ids = set()
    for lvl in range(1, 8):
        m = re.search(rf'<w:style [^>]*w:styleId="Heading{lvl}"[^>]*>(.*?)</w:style>', sty, re.S)
        n = re.search(r'<w:numId w:val="(\d+)"', m.group(1)) if m else None
        if not n:
            problems.append(f"標題 {lvl} 沒有連到多層次清單")
        else:
            ids.add(n.group(1))
    if len(ids) == 1:
        ok(f"標題 1 到 7 都連到同一個多層次清單（numId {next(iter(ids))}）")
    elif ids:
        problems.append(f"標題連到不同清單：{ids}")
    nid = next(iter(ids), None) if ids else None
    am = re.search(rf'<w:num w:numId="{nid}"[^>]*>.*?<w:abstractNumId w:val="(\d+)"', num, re.S)
    abs_xml = re.search(rf'<w:abstractNum [^>]*w:abstractNumId="{am.group(1)}".*?</w:abstractNum>', num, re.S).group(0) if am else num
    fmts = re.findall(r'<w:lvl w:ilvl="(\d)".*?<w:numFmt w:val="([^"]+)"/>.*?<w:lvlText w:val="([^"]*)"/>', abs_xml, re.S)
    want = {"0": ("ideographLegalTraditional", "%1、"), "1": ("taiwaneseCountingThousand", "%2、"),
            "2": ("taiwaneseCountingThousand", "（%3）"), "3": ("decimal", "%4."), "4": ("decimal", "(%5)"),
            "5": ("lowerLetter", "%6."), "6": ("lowerLetter", "(%7)")}
    got = {}
    for lv, f, t in fmts:
        got.setdefault(lv, (f, t))
    miss = [f"第 {int(k) + 1} 層 {got.get(k)} ≠ {v}" for k, v in want.items() if got.get(k) != v]
    problems += miss
    if not miss:
        ok("七層編號格式 壹、/一、/（一）/1./(1)/a./(a) 正確")

    # 3. TOC、SEQ 欄位、updateFields
    if re.search(r"TOC \\o &quot;1-3&quot; \\h \\z \\u|TOC \\o \"1-3\" \\h \\z \\u", doc):
        ok("目錄是自動 TOC 欄位")
    else:
        problems.append("找不到 TOC 欄位")
    seq = len(re.findall(r"SEQ (圖|表|Figure|Table)", doc))
    ok(f"SEQ 圖表標號欄位 {seq} 個") if seq else problems.append("沒有 SEQ 圖表標號")
    if "updateFields" not in z.read("word/settings.xml").decode("utf-8"):
        problems.append("settings 沒有 updateFields，開啟時目錄不會自動更新")
    else:
        ok("設定了開啟時更新欄位")

    # 4. 文字內容：全形空白、空段落、錯字
    paras = re.findall(r"<w:p[ >].*?</w:p>", doc, re.S)
    text_of = lambda p: "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", p))   # noqa: E731
    full = "\n".join(text_of(p) for p in paras)
    n_full = full.count("　")
    ok("沒有全形空白") if not n_full else problems.append(f"含全形空白 {n_full} 個（會顯示成方框）")
    body_no_tbl = re.sub(r"<w:tbl>.*?</w:tbl>", "", doc, flags=re.S)
    paras_out = re.findall(r"<w:p[ >].*?</w:p>", body_no_tbl, re.S)
    empty = sum(1 for p in paras_out if not text_of(p).strip() and "<w:drawing" not in p and "<w:br" not in p
                and "fldChar" not in p and "<w:sectPr" not in p)
    ok("表格外沒有空段落") if empty == 0 else problems.append(f"表格外有 {empty} 個空段落（不可用空白段落撐版面）")
    for typo in ("輪鼓", "輪穀"):
        if typo in full:
            problems.append(f"錯字：{typo}")
    ok("沒有「輪鼓」「輪穀」錯字") if not any(t in full for t in ("輪鼓", "輪穀")) else None
    ok(f"內容 {len(full)} 字、{len(paras)} 段、圖 {doc.count('<w:drawing')} 張")

    print()
    for p in problems:
        print("問題：" + p)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
