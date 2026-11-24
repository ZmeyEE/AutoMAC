"""產生「AutoMAC 開發文件」Word 檔（用系統 Python 的 python-docx，不需額外安裝）。

內容來源
- 手寫的章節文字（背景、實測、設計取捨、驗證與反省）：寫在本檔，數字都來自實測紀錄。
- 模組與函式說明：用 ast 讀 src/automac 的原始碼與 docstring 自動產生，所以和程式永遠一致。
- 設定欄位一覽：從 config.py 的 Settings 欄位與行尾註解自動產生。
- 測試項數：現場載入測試並計數。
- 圖：docs/diagrams/*.png（由 tools/make_diagrams.py 產生，同時有可編輯的 .drawio）。

用法：python tools/make_report.py        輸出 文件/AutoMAC開發文件.docx
"""
from __future__ import annotations

import ast
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
from docx.enum.text import WD_ALIGN_PARAGRAPH  # noqa: E402
from docx.shared import Cm, Pt, RGBColor  # noqa: E402

import docx_helpers as H  # noqa: E402

OUT = ROOT / "文件" / "AutoMAC開發文件_3.docx"   # 不覆蓋使用者可能已手改的第一版
DIAG = ROOT / "docs" / "diagrams"
SRC = ROOT / "src" / "automac"


# ----------------------------------------------------------------------------- 自動產生的資料
def first_line(doc: str | None, n: int = 70) -> str:
    if not doc:
        return "（無說明）"
    line = next((ln.strip() for ln in doc.strip().splitlines() if ln.strip()), "")
    line = re.sub(r"\s+", " ", line)
    return line if len(line) <= n else line[:n] + "…"


def module_rows(path: Path) -> tuple[str, list[list[str]]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    summary = first_line(ast.get_docstring(tree), 90)
    rows: list[list[str]] = []
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            rows.append([f"`{node.name}`", "類別", first_line(ast.get_docstring(node))])
            for m in node.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)) and not m.name.startswith("_"):
                    rows.append([f"`{node.name}.{m.name}`", "方法", first_line(ast.get_docstring(m))])
        elif isinstance(node, ast.FunctionDef) and not node.name.startswith("_"):
            rows.append([f"`{node.name}`", "函式", first_line(ast.get_docstring(node))])
    return summary, rows


def settings_rows() -> list[list[str]]:
    lines = (SRC / "config.py").read_text(encoding="utf-8").splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.startswith("class Settings"))
    end = next(i for i, ln in enumerate(lines) if "# ---- 存取工具" in ln)
    rows = []
    for ln in lines[start:end]:
        m = re.match(r"\s{4}(\w+): ([^=]+?)(?: = (.+?))?(?:\s+#\s*(.*))?$", ln)
        if m and not ln.strip().startswith("#") and m.group(1) not in ("mac",):
            name, typ, default, comment = m.groups()
            if default is None:
                continue
            default = default.split("  #")[0].strip()
            rows.append([f"`{name}`", default.replace("field(default_factory=lambda: ", "").rstrip(")") if "field(" in default else default,
                         comment or ""])
    return rows


def test_counts() -> tuple[int, list[list[str]]]:
    loader = unittest.TestLoader()
    suite = loader.discover(str(ROOT / "tests"), top_level_dir=str(ROOT))
    counts: dict[str, int] = {}

    def walk(s):
        for t in s:
            if isinstance(t, unittest.TestSuite):
                walk(t)
            else:
                mod = type(t).__module__.split(".")[-1]
                counts[mod] = counts.get(mod, 0) + 1
    walk(suite)
    names = {"test_p0_basics": "P0 MAC 工具、統計、設定與狀態檔", "test_p1_detector": "P1 流量取樣與偵測狀態機（含真實樣本重播）",
             "test_p2_probe": "P2 探針與有效性判斷", "test_p3_scan_presence": "P3 鄰居掃描與佔用協定",
             "test_p4_switch": "P4 選擇策略與切換流程（含失敗回滾）", "test_p5_service": "P5 服務層（自動切換各種保護）",
             "test_p6_gui": "P6 視窗操作", "test_p8_hub": "P8 伺服器登記處、組員端、加密、入網配發與用量帳本（含真實 HTTPS）"}
    rows = [[names.get(k, k), str(v)] for k, v in sorted(counts.items())]
    return sum(counts.values()), rows


# ----------------------------------------------------------------------------- 文件組裝
def build() -> None:
    doc = H.setup_document("AutoMAC 開發文件")
    total_tests, test_rows = test_counts()

    # ---- 封面：白底、置中圖、一行黑色粗體 28 pt 標題、兩行灰色小字（不放色塊、日期、版本號）
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(60)
    p.add_run().add_picture(str(DIAG / "01_架構分層圖.png"), width=Cm(13.5))
    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t.paragraph_format.space_before = Pt(30)
    r = t.add_run("AutoMAC 開發文件")
    r.bold = True
    r.font.size = Pt(28)
    r.font.color.rgb = RGBColor(0, 0, 0)
    for line in ("校園網 MAC 限速偵測與自動切換", "實驗室"):
        g = doc.add_paragraph()
        g.alignment = WD_ALIGN_PARAGRAPH.CENTER
        g.paragraph_format.space_after = Pt(2)
        rr = g.add_run(line)
        rr.font.size = Pt(12)
        rr.font.color.rgb = RGBColor(0x7F, 0x7F, 0x7F)
    sec = doc.sections[0]
    sec.different_first_page_header_footer = True
    H.add_page_number_footer(sec)
    H.add_toc(doc)
    H.set_update_fields(doc)

    h = lambda lv, text: doc.add_paragraph(text, style=f"Heading {lv}")
    P = lambda text: H.body_paragraph(doc, text)

    # ========================================================================= 壹
    h(1, "前言")
    h(2, "背景與問題")
    P("學校科技大學校園網路對每個登記的 MAC 位址設有流量上限。依使用者說明，每個 MAC 每日約 10 GB，超過後速度會被壓到約 1 Mbps；"
      "這條規則未見於校方公開頁面，本文件視為使用者描述的現象，並以實測驗證其表現，而不是引用官方規定。")
    P("實驗室有多位成員共用一池已登記的 MAC，所有裝置連到同一台不可管理的交換機。交換機沒有管理介面，校方也無法查詢各 MAC 的用量，"
      "因此無法事先知道哪個 MAC 還有額度、也無法知道別人正在用哪個 MAC。")
    P("舊版工具（`mac_switcher.py`）的做法是每 15 秒下載 1 MB 測速，低於門檻就切換下一個 MAC。實測與分析發現三個問題：")
    H.bullets(doc, ["使用者自己正在下載時，探針與下載搶頻寬，讀值偏低，舊版 8 Mbps 的門檻會被誤觸發而白白切換。",
                    "被限速時每次探針要花約 8 秒，占掉唯一的 1 Mbps，使用者會感覺更卡。",
                    "沒有任何機制知道其他成員正在用哪個 MAC，兩人用到同一個 MAC 會造成 IP 衝突與雙方斷線。"])
    h(2, "目標與範圍")
    H.bullets(doc, ["偵測限速要快且極低誤判，並且在使用者下載或看影片時不探測、不造成卡頓。",
                    "被限速時能一鍵或自動切換到池內沒人用的 MAC，換完驗證網路真的通了，失敗要換回原 MAC。",
                    "切換前確認沒有其他人正在用同一個 MAC（鄰居掃描加 mDNS 互相宣告）。",
                    "提供視窗讓使用者新增、刪除、標記、切換 MAC；同一套程式也能在實驗室伺服器上無人值守執行。",
                    "伺服器換 MAC 後，監視器（Pi）的影像傳輸要能自動接回，不需要人工處理。",
                    "整理成函式庫並附測試與文件，方便之後維護與上傳 GitHub。"])
    h(2, "使用環境")
    H.table(doc, "實驗室裝置與角色", ["裝置", "系統", "角色"],
            [["實驗室電腦", "Windows 10", "開發與日常使用，執行 AutoMAC 視窗或服務"],
             ["伺服器 LAB-SERVER", "Windows 11", "共用資料夾、其他服務 伺服器；執行 AutoMAC 背景服務"],
             ["監視器 pi-monitor", "Raspberry Pi 4（Debian 13）", "USB 攝影機加 YOLOv8n 偵測，影像經 SMB 傳到伺服器"]],
            [4.2, 4.2, 7.6])
    H.note_box(doc, "本文件含實驗室網段位址與裝置角色，僅供維護使用；對外分享或上傳前請先檢查。程式碼與範例設定檔不含真實 MAC。")

    # ========================================================================= 貳
    h(1, "需求與設計原則")
    h(2, "使用者需求")
    H.bullets(doc, ["實驗室內可能有多人共用 MAC，需要知道區網內其他裝置在用哪個 MAC。",
                    "要求「快速切換」，並明確表示不在意記錄下載量，因此不做流量帳本。",
                    "測速不可讓使用者感覺卡頓；使用者自己在下載時，速度會和被限速時相近，必須能分辨。",
                    "要有視窗新增與刪除 MAC；伺服器也需要依流量自動換 MAC。",
                    "要查開源專案與期刊論文，不要重複造輪子；研討會論文品質參差，優先看期刊。"])
    h(2, "設計原則")
    H.bullets(doc, ["先查再寫：每個技術先找現成的標準、論文或開源專案，能套用就套用，不採用要記錄原因。",
                    "判斷邏輯與系統邊界分離：偵測與選擇策略是純邏輯，時間與外部結果由外部傳入，才能用真實樣本重播驗證。",
                    "安全優先：自動切換預設關閉；寧可少切不要誤切；任何失敗都換回原 MAC，不留下斷線狀態。",
                    "少依賴：只依賴 psutil 與 zeroconf，視窗用標準函式庫 tkinter，網路用標準函式庫 urllib。",
                    "誠實：沒驗證的就寫沒驗證，推算的數字要標明是推算。"])
    h(2, "開發流程")
    P("每個階段都依序做：實作、驗證、反省、抉擇最佳解、優化，然後才進下一階段。反省與抉擇的完整紀錄見「驗證與反省」一章。"
      "每個技術都附可編輯的圖（draw.io 格式）與函式說明，完成後整理成本文件。")

    # ========================================================================= 參
    h(1, "文獻與現有技術調查")
    h(2, "論文與標準")
    P("文獻以 Crossref 查書目，確認題名、出處與 DOI；標示「已讀」的是讀過全文，其餘只確認題名與出處，並未取用其中的具體數據。")
    H.table(doc, "文獻調查結果", ["文獻", "出處", "用途與結論", "讀過"],
            [["ShaperProbe（Kanuparthi 與 Dovrolis）", "ACM IMC 2011\ndoi:10.1145/2068816.2068860",
              "以持續約 60 秒的飽和探測偵測整形（shaping）。需塞滿線路且需對端配合，不適合平常使用；只借用概念", "已讀"],
             ["An Internet-Wide Analysis of Traffic Policing（Flach 等）", "ACM SIGCOMM 2016\ndoi:10.1145/2934872.2934873",
              "從伺服器端封包記錄偵測丟包型限速。本專案在客戶端，無法取得伺服器端資料；只借用整形與丟包的區分概念", "已讀"],
             ["Inferring traffic shaping and policy parameters using end host measurements", "IEEE INFOCOM 2011\ndoi:10.1109/infcom.2011.5934941",
              "推論限速參數。我們的限速型態已用實測量到，不需要再推論", "否"],
             ["Identifying Traffic Differentiation in Mobile Networks（Kakhki 等）", "ACM IMC 2015\ndoi:10.1145/2815675.2815691",
              "偵測依應用程式的差別待遇。本專案的限速是以 MAC 為單位，不依應用程式", "否"],
             ["RFC 5227 IPv4 Address Conflict Detection", "IETF 2008\ndoi:10.17487/rfc5227",
              "佔用協定借用其「探測、宣告、衝突時退讓」的概念", "已讀"],
             ["RFC 6762 Multicast DNS；RFC 6763 DNS-Based Service Discovery", "IETF 2013\ndoi:10.17487/rfc6762、rfc6763",
              "採用 mDNS 與 DNS-SD 做區網宣告與發現，內建名稱衝突探測、到期與離線告別封包", "否"],
             ["Leases: An Efficient Fault-Tolerant Mechanism for Distributed File Cache Consistency（Gray 與 Cheriton）", "ACM SOSP 1989\ndoi:10.1145/74850.74870",
              "租約概念：宣告有存活時間，程式當掉時自動失效", "否"],
             ["Zero Configuration Networking: Implementation, performance, and security", "Computers and Electrical Engineering 2012\ndoi:10.1016/j.compeleceng.2012.02.011",
              "支持 mDNS 這類零設定協定在區網可用", "否"],
             ["Stochastic analysis of packet-pair probing for network bandwidth estimation", "Computer Networks 2006\ndoi:10.1016/j.comnet.2005.09.037",
              "探針設計的背景：小探針的讀值偏低是已知現象", "否"]],
            [4.6, 3.6, 6.2, 1.2], font=9.5)
    P("結論：文獻中的限速偵測技術都需要主動飽和探測或伺服器端資料，不適合「使用者平常在用、不能卡頓」的情境。"
      "本專案的限速特徵已用實測量到，因此採用「網卡流量門檻加小探針」，比文獻方法更輕量。")
    h(2, "GitHub 開源專案")
    H.table(doc, "開源專案調查結果", ["需求", "候選專案", "授權與狀態", "結論"],
            [["換 MAC", "MAC-SPOOFER、MacForger、change-mac 等", "授權不一（GPL-3.0、未明示等），多為改登錄檔", "不採用：現有 `Set-NetAdapter -MacAddress` 是官方指令且已可用"],
             ["限速偵測", "ShaperProbe 的多個 fork", "未標授權，2012 到 2016 停更，需 M-Lab 伺服器", "不採用：只借概念"],
             ["限速偵測（其他）", "搜尋 throttling detection", "只找到通用測速工具", "無匹配，自行實作"],
             ["區網宣告與發現", "python-zeroconf（743 顆星，2026-11 仍在更新）", "LGPL-2.1 以上，Python 3.11 以上", "採用：實測可用"],
             ["區網宣告（自寫）", "搜尋 UDP 廣播探索", "只有一星的聊天室類專案", "無合用專案；mDNS 已足夠"]],
            [2.6, 4.6, 4.4, 5.0], font=9.5)

    # ========================================================================= 肆
    h(1, "實測資料與分析")
    P("所有決策都以 2026-11-23 在實驗室的實測為依據。測試工具為 `samples/probe.py`、`recorder.py` 與專案內的 `tools/` 腳本，"
      "測試期間總下載量約數百 MB，遠低於每日額度。")
    h(2, "限速與健康狀態的差異")
    H.table(doc, "健康與限速狀態的實測比較", ["項目", "未被限速", "被限速"],
            [["單串流下載", "中位 88 Mbps，最大 92.5 Mbps", "約 1.0 Mbps，變動只有 0.9 到 1.1"],
             ["四串流下載", "94 到 96 Mbps，曲線平", "總和同樣約 1.0 Mbps（以 MAC 為單位，不分連線）"],
             ["閘道 ping", "下載時 1 ms 升到 8 到 10 ms", "1 ms，不受影響（限速點在閘道之後）"],
             ["TCP 重傳率", "0.03%", "0.2% 到 1.9%"],
             ["下載中的外部連線時間", "約 3 ms", "中位 3 到 58 ms，最大 1.3 到 2.3 秒（有排隊）"]],
            [4.0, 5.8, 6.2])
    P("被限速時的下載曲線是一條貼在約 1 Mbps 的平線，這是偵測的主要特徵。另在一次四串流測試開頭觀察到約 1 秒、約 60 Mbps 的突發（約 8 MB），"
      "之後掉回 1 Mbps；休息 60 秒後單串流重測沒有再出現，原因未明。因此偵測用中位數而非平均，避免被突發影響。")
    h(2, "使用者自己下載時")
    P("在使用者自己下載（健康 MAC）的 70 秒樣本中，網卡下載是 98.4 Mbps 的平線（標準差 0.54）。同時 256 KB 探針只讀到 3.5 Mbps，"
      "1 MB 探針讀到 4.9 Mbps，外部連線時間 P90 約 1 秒。這證明下載中探針讀值嚴重偏低，舊版 8 Mbps 的門檻會誤觸發；"
      "也證明用網卡流量當第一道判斷最可靠：98 Mbps 與約 1 Mbps 差約 100 倍。")
    h(2, "探針大小與讀值")
    H.table(doc, "探針大小對讀值的影響（新連線）", ["探針大小", "讀值", "說明"],
            [["128 KB", "3.8 Mbps", "握手與慢啟動吃掉大部分時間，健康時就接近門檻，不採用"],
             ["256 KB", "7 到 29 Mbps（依時段）", "限速時約 2 到 3 秒、讀值約 0.9 Mbps，兩者差 8 倍以上，採用"],
             ["1 MB", "約 23.5 Mbps", "舊版做法；限速時需約 8 秒，占掉可用頻寬"],
             ["10 MB", "約 68.6 Mbps", "太大，限速時要 80 秒"]],
            [3.0, 4.2, 8.8])
    P("健康讀值是範圍而不是常數（早期用 requests 測得 7.3 Mbps，之後用標準函式庫 urllib 測得 21 到 29 Mbps），"
      "所以門檻以限速值（約 0.9 Mbps）為基準，固定 2 Mbps，不貼近健康值。限速狀態下探針本身的讀值是由連續下載曲線的前 2 到 3 秒推算，"
      "尚未在被限速時直接量測探針，這是已知的缺口。")
    h(2, "區網與伺服器")
    H.bullets(doc, ["鄰居掃描：網段 /27 找到 7 台（含閘道）；其中 3 台不回應 ping，只有靠 ARP 才找得到，ICMP 約漏掉一半。",
                    "池內有兩個 MAC 當時已被別台使用（一台 Linux 監視器、一台 Windows 伺服器），證明重疊風險與掃描可行。",
                    "二層網路比 /27 大：mDNS 發現了另一個子網（10.13.200.x）的印表機，所以 ARP 掃描看不到全部，需要 mDNS 補強。",
                    "mDNS 雙向測試：Windows 10 與 Windows 11 互相宣告與發現成功；伺服器的防火牆預設擋掉 Python 的 UDP 5353 進站封包，需新增規則才通。",
                    "伺服器網卡 90 秒量測：下載與上傳幾乎為 0；Pi 每 10 分鐘推送約 13 MB（約 1 秒的突發），不會讓流量門檻永遠不通過，因此不需要額外的伺服器模式。"])

    # ========================================================================= 伍
    h(1, "系統設計與技術說明")
    h(2, "整體架構")
    P("程式分五層，上層只依賴下層：呈現層只做顯示，協調層串接並排程，純邏輯層不碰系統與網路，系統與網路邊界都有小介面，基礎層放格式與設定。"
      "這個切法讓偵測與選擇策略能用真實樣本重播驗證，也讓測試能把網卡、掃描、宣告換成假物件。")
    H.figure(doc, DIAG / "01_架構分層圖.png", "AutoMAC 架構分層（可編輯檔：01_架構分層圖.drawio）", max_h=14.0)
    h(2, "限速偵測技術")
    h(3, "判斷規則")
    P("規則採使用者提出的邏輯：主機沒有在輸出大流量，而且探針偏慢，才算被限速。流量大時線路明顯是通的，就不必也不該探測。")
    H.table(doc, "偵測規則", ["網卡目前流量", "動作"],
            [["大（10 秒中位數不小於 3 Mbps）", "一定不是限速：不探測、不打擾"],
             ["小", "發 256 KB 探針；探針期間若有別的流量，這次讀值無效、不計次"],
             ["連續兩次有效且低於 2 Mbps", "判定限速；探針連不上則視為斷網，不計次"]],
            [6.0, 10.0])
    h(3, "流程")
    H.figure(doc, DIAG / "02_偵測流程圖.png", "限速偵測流程（可編輯檔：02_偵測流程圖.drawio）", max_h=17.5)
    h(3, "安靜判斷與探測時機")
    H.bullets(doc, ["安靜：10 秒視窗的中位數低於門檻（抗突發），且最近 3 秒的平均也低於門檻（抓住剛開始的下載）。",
                    "探測時機有四種：確認（第一次偏慢後 5 秒）、換 MAC 後驗證、平線指紋（中位數落在 0.5 到 1.8 Mbps 且起伏很小）、閒置間隔（每 180 秒）。",
                    "平線來源慢但線路健康時，探針會正常，之後 180 秒內不再因平線重探，避免一直打擾。",
                    "第一次偏慢若 60 秒內沒被確認就作廢，回到閒置排程，不會因為一次瞬間壅塞而判限速。"])
    h(3, "設計取捨與反省")
    P("取捨一：用中位數還是平均？限速時曾有 1 秒突發，平均會被拉高而誤判成有流量；但中位數在一次短下載結束時會讓探針在下載尾端啟動。"
      "最終長視窗用中位數、短視窗用平均，兩者都滿足才算安靜。")
    P("取捨二：誤判比漏判更糟。誤判會白白換 MAC 造成斷線，漏判只是晚幾分鐘發現。因此要求連續兩次、探針必須有效、斷網不計次。"
      "用隨機忙碌流量模擬 2 小時，偵測器一個探針都沒發。")
    h(2, "探針與有效性")
    P("探針是 256 KB 單串流新連線，用標準函式庫 urllib，自訂 User-Agent（Cloudflare 會對預設的 Python-urllib 回 403，這是實測踩到的）。"
      "讀值是酬載位元數除以耗時（含 DNS 與握手）。")
    P("有效性判斷：探針前後各讀一次網卡累計位元組，扣掉探針自己的位元組（酬載乘 1.05 再加 12 KB，估算標頭與握手），剩下的就是探測期間的其他流量，"
      "低於門檻才算有效。真實網路驗證：閒置時其他流量約 0；背景下載時其他流量 80 Mbps 以上，被標為無效。")
    h(2, "防重疊技術")
    h(3, "鄰居掃描")
    P("對網段內每個位址送 1 位元組的 UDP 到 9 號埠，逼作業系統先做 ARP，再讀 Windows 鄰居表。不需要 Npcap 等封包擷取驅動。"
      "管理員身分會先清空鄰居表，結果才是現在真的在線；非管理員無法清空，結果偏保守（可能把已離線的 MAC 當成使用中，只會少選，不會撞）。")
    h(3, "mDNS 宣告")
    P("每台跑本工具的機器用 python-zeroconf 宣告服務 `_automac._tcp.local.`，TXT 記錄放目前使用的 MAC、狀態、時間戳與角色。"
      "宣告的存活時間設為 120 秒，程式當掉沒發告別封包時，最久 120 秒後別人就看不到它（預設 4500 秒太久）。")
    h(3, "佔用協定")
    P("兩個人同時挑到同一個 MAC 是最危險的情況，所以借用 RFC 5227 的探測與宣告概念：先宣告 claiming 並附時間戳，等待後檢查有沒有人 holding 或時間戳更早的 claiming，"
      "有就讓位，平手比實例名稱。每一方獨立套用同一規則，恰好一方勝出（單元測試涵蓋三方同時搶佔；同機實測兩個實例同時搶，結果 1 勝 1 負）。")
    H.figure(doc, DIAG / "04_佔用協定流程圖.png", "MAC 佔用協定（可編輯檔：04_佔用協定流程圖.drawio）", max_h=11.0)
    H.note_box(doc, "這個設計把空窗縮到極小，但不是數學保證（網路掉封包時仍可能漏看），所以切換後還有驗證，且鄰居掃描是第二道防線。")
    h(2, "伺服器登記處與組員端")
    P("實驗室的實際情況是「伺服器是登記處」：MAC 池只存在伺服器，組員電腦不該各存一份。因此分成三種角色：單機（自己管清單，原行為）、伺服器（hub，唯一能改清單）、"
      "組員端（client，清單從伺服器同步，視窗與命令列都不提供新增、刪除、改標籤、固定占用、匯入匯出）。")
    P("組員端每 30 秒向伺服器同步一次清單（含刪除與標籤），並回報目前持有的 MAC 以刷新租約；需要換 MAC 時向伺服器「要」一個，由伺服器掃描區網並看自己登記的租約，"
      "在同一把鎖內挑選並預約，所以兩台同時要不會拿到同一個。某台發現某 MAC 被限速，回報後全員都避開。伺服器自己也是一台會換 MAC 的電腦，直接呼叫登記處，不經網路。")
    H.figure(doc, DIAG / "07_伺服器與組員端分配流程圖.png", "伺服器與組員端（可編輯檔：07_伺服器與組員端分配流程圖.drawio）", max_h=12.0)
    h(3, "連不上伺服器時")
    P("伺服器換自己的 MAC 時 IP 會變，網路也可能中斷，所以組員端保留上次同步的清單當快取，連不上時退回原本的做法（本機清單、mDNS 宣告與鄰居掃描）繼續運作，並只在狀態變化時警告一次。"
      "伺服器的網址若沒設定，組員端從 mDNS 找 role=hub 的宣告，伺服器換 IP 也找得到。")
    h(3, "通訊加密")
    P("校園網上其他人也連得到伺服器的埠，所以通訊必須加密並確認對方身分。先查文獻與現成專案（見「文獻調查結果」）再決定：傳輸層用 TLS 1.3"
      "（Dowling 等人在 Journal of Cryptology 2021 對其握手做了形式化安全分析；本文只讀了摘要，未讀全文），"
      "伺服器用自簽的 ECDSA P-384 憑證，組員設定檔記下憑證指紋，組員端握手後先比對指紋才送任何資料（憑證釘選，IoT 與封閉群體的常見做法）。"
      "不採用雙向 TLS（用戶端憑證管理與可用性問題多）與 Noise 協定（需新增相依套件、Python 實作停更），改在 TLS 內帶 256 位元 token 驗證組員。")
    H.table(doc, "威脅與對策", ["威脅", "對策", "驗證方式"],
            [["同網段竊聽", "TLS 1.3、臨時 ECDHE（前向保密）、AEAD 資料加密；只允許 1.3", "中繼器記錄全部位元組，搜尋 token、MAC、名稱、路徑皆找不到；TLS 1.2 與明文 HTTP 連線被拒；實機協商 AES-256-GCM"],
             ["假冒伺服器、中間人", "憑證釘選，握手後送資料前比對 SHA-256 指紋", "另一張憑證的假伺服器：組員端中止，假伺服器沒收到任何請求；錯誤指紋被拒"],
             ["冒充組員", "256 位元 token，TLS 內傳送，常數時間比對", "錯 token 被拒（401）"],
             ["暴力猜測、濫用", "同來源失敗 5 次鎖 60 秒；請求大小上限；握手在處理執行緒（10 秒逾時）", "第 6 次連正確 token 也被鎖，時間到恢復；超大請求回 413；明文連線後服務仍正常"]],
            [3.4, 6.2, 6.4], font=9.5)
    P("成本：每次新連線含握手與釘選，本機量測比明文多約 3 ms（P-384 憑證、947 位元組回應、各 100 次）；對真實伺服器的區網單次同步約 7 ms。組員端每 30 秒同步一次，影響可忽略。")
    h(3, "安全與限制")
    H.bullets(doc, ["沒設 token 或 token 太短（少於 16 字元）伺服器不開 API；防火牆規則限制在本機子網路。",
                    "憑證沒有撤銷機制，伺服器換憑證後要重新發組員設定檔；組員設定檔含 token 與指紋，外流可被拿去冒充組員（仍無法假冒伺服器），懷疑外流就換 token。",
                    "伺服器若整台沒開，組員端只能靠備援，無法知道不跑 AutoMAC 的裝置以外的租約。",
                    "實測踩過的坑：整網段掃描要數秒，若一般檢視也同步掃描，組員端 5 秒逾時會把「伺服器忙著掃描」誤判成連不上；改成檢視只讀快取、分配才等新掃描。"])
    h(3, "組員端背景服務與視窗")
    P("組員端不能因為不小心關掉視窗就停止偵測與用量記錄，所以拆成兩個程序：背景服務（Windows 排程工作，開機自動啟動、SYSTEM 身分、當掉自動重啟）"
      "做偵測、切換、同步與記錄；視窗只是檢視器，透過本機控制介面（127.0.0.1，token 檔只有系統管理員讀得到）看狀態、下指令。"
      "組員雙擊 exe 時自動安裝或升級服務：exe 複製到 ProgramData（原資料夾可刪），內容雜湊不同就升級，排程工作直接執行 exe（不經 cmd，否則 exe 變孤兒、當掉不重啟）。")
    H.figure(doc, DIAG / "08_客戶端背景服務與視窗.png", "組員端背景服務與視窗（可編輯檔：08_客戶端背景服務與視窗.drawio）", max_h=11.0)
    H.bullets(doc, ["效能：用 psutil 讀目前 MAC 與 IP（不再每 30 秒開兩次 PowerShell），低優先權執行；實測 60 秒 CPU 約單核 0.16%、記憶體約 41 MB。",
                    "流量監測可靠性：切換前先結算舊 MAC 的用量；看門狗在讀不到網卡計數器 10 秒時警告並重新偵測網卡；切換期間不算故障。",
                    "切換防重複：同時間只允許一個切換，第二個直接回覆進行中；切換中視窗停用按鈕。實機按下到完成由 41.7 秒降到 8.6 秒，再降到 4.4 秒：直接寫登錄 NetworkAddress 並用 pnputil 重啟網卡（省掉 PowerShell 啟動，下指令 1.8 秒降到 0.2 秒；失敗自動退回 Set-NetAdapter）、輪詢間隔 1 秒改 0.25 秒、換完後的 mDNS 重新宣告改背景執行。剩下約 4 秒是網卡驅動重啟與網路線重新協商，省不掉。",
                    "伺服器的區網鄰居表約一分鐘才忘記剛離開的 MAC，原本會把電腦自己剛離開的 MAC 誤判成他人使用中（換回去被擋）；現在只在掃描到的 IP 與它離開時的 IP 相同、且 120 秒內才豁免。",
                    "卡頓的真正原因是 .local 名稱的 IPv6 位址：標準連線先試 IPv6，伺服器只聽 IPv4，每個請求多等 5 到 21 秒；改成只解析 IPv4 後降到 10 毫秒內。",
                    "標籤跟著電腦走：伺服器依租約把電腦的標籤顯示在它目前用的 MAC 上。"])
    h(3, "系統匣圖示、解除安裝與介面")
    P("背景執行要像一般常駐程式一樣在螢幕右下角有個小圖示。背景服務跑在 SYSTEM 工作階段（沒有桌面，放不了圖示），所以另外有一個很小的「系統匣程序」"
      "（automac.exe tray，使用者登入時由排程工作啟動，不再跳權限詢問）：圓形圖示用程式畫出來，綠色代表正常、黃色代表切換中或自動輪流已停止、紅色代表背景服務沒有回應"
      "（連續 20 秒沒回應就叫排程工作重啟服務）；點一下開視窗，右鍵選單有開啟視窗、自動挑選並切換、被限速時自動切換、解除安裝、關閉圖示，換完 MAC 會跳出通知。"
      "只用 ctypes 呼叫 Win32（Shell_NotifyIcon），不新增套件。關閉視窗就是縮回右下角，圖示與服務都還在。")
    P("解除安裝放在介面裡（視窗右下角按鈕與圖示右鍵選單），不再需要執行批次檔：停止服務與圖示、刪排程工作與防火牆規則、刪除本機設定資料夾；"
      "資料夾因為可能是自己正在執行的 exe 所在，改由獨立的 cmd 在幾秒後刪除，且只在資料夾確實是 ProgramData 底下的 AutoMAC 時才整個刪。MAC 清單、標籤與用量在伺服器上，不受影響。")
    P("介面：組員端採粉彩天空色票（蜜桃白、粉、薰衣草、霧藍，使用者提供），所有填色取同一組飽和度（約 30 到 35%），綠色主按鈕是用霧藍的飽和度與明度算出來的；"
      "圓角用抗鋸齒小圖貼在四個角，按鈕與卡片共用同一個函式。「自動挑選並切換」是綠色大按鈕放在最上面，一鍵完成不再詢問；「切換到所選」是進階選項。"
      "伺服器與單機維持樸素版面（使用者決定美化只做客戶端）。")
    h(3, "陌生電腦自動入網")
    P("組員包要能放到任何同區網的電腦就用。若那台電腦目前的 MAC 不在池內（沒申請的 MAC，多半連不上外網），自動切換不會觸發："
      "探針失敗只會被判成網路中斷（刻意不當成限速，避免斷網時亂換）。所以組員端同步到清單後，若目前 MAC 不在池內就主動向伺服器要一個並切換；"
      "需要系統管理員權限，沒有權限只警告一次，失敗每 60 秒再試。前提是那台電腦用未申請的 MAC 時仍連得到伺服器，這點尚未實測。")
    h(3, "用量帳本")
    P("使用者要求看得到每個 MAC 的具體用量。每台跑本工具的電腦用網卡累計位元組算區間用量，歸給當時用的 MAC，每 30 秒回報伺服器；"
      "伺服器按額度日累加存檔，視窗顯示今日用量與佔每日上限的百分比，命令列 `automac usage` 列出歷史。分配時在可用的 MAC 中優先選今天用得最少的。"
      "切換成功時把累積的流量歸給舊 MAC，回報失敗的留著下次補送，網卡計數器歸零只重設基準。實測下載 75 MB，伺服器帳本增加 81.5 MB（含同期間其他流量）。")
    H.bullets(doc, ["只看得到跑本工具的電腦，Pi 等裝置不算，所以是下限，不是校方實際計算的值。",
                    "不知道校方算下載、上傳或兩者之和，兩個方向分開記，百分比以兩者之和估算；每日上限 10 GB 是使用者描述的規則，未經校方證實，只用來顯示。"])
    h(2, "切換流程")
    H.figure(doc, DIAG / "03_切換流程圖.png", "切換流程，含失敗回滾（可編輯檔：03_切換流程圖.drawio）", max_h=18.0)
    h(3, "選擇策略")
    P("池內排除目前使用中、固定占用（例如 Pi 長期用的）、他人使用中、已知被限速且未到恢復時間的 MAC，之後從目前這個的下一個開始輪流。"
      "不做流量帳本（使用者明確不要），輪流是不需要帳本的近似；被限速的 MAC 記到假定的恢復時間（預設每天 0 點，可調，規則未經校方證實）。")
    h(3, "失敗回滾")
    P("換完之後輪詢到「MAC 已變、網卡已連線、拿到非自動位址的 IPv4」才算成功；逾時就換回原 MAC 並釋放宣告。"
      "這取代舊版的固定等待 5 秒，也避免 DHCP 失敗（例如該 MAC 在校方系統沒登記到這個空間）時留下斷線狀態。網卡層面的失敗不再試下一個候選，避免連續折騰硬體。")
    h(3, "切換頻率限制")
    P("兩次切換至少隔 60 秒；一小時內超過 4 次就停止自動輪流並通知。連續換了幾個都慢，多半是整池都慢或校園網整體壅塞，再換只會一直斷線。")
    h(2, "服務執行與事件")
    P("服務是單一主迴圈加單一工作執行緒：主迴圈每秒取樣並餵給偵測器；探針與切換排隊在工作執行緒執行，不會同時探測又換 MAC。"
      "所有發生的事都經事件匯流排發出，命令列、視窗、日誌各自訂閱，核心不知道它們的存在。")
    H.figure(doc, DIAG / "05_執行時整體流程圖.png", "服務執行時的執行緒與資料流（可編輯檔：05_執行時整體流程圖.drawio）", max_h=11.0)
    H.bullets(doc, ["自動切換預設關閉；`--dry-run` 只報告「本來會切換到哪個 MAC」。",
                    "失敗的嘗試也要等 60 秒才能再試，避免失敗時每 10 秒折騰一次網卡。",
                    "沒有管理員權限時只能監看，並明確提示。"])
    h(2, "視窗")
    P("視窗使用標準函式庫 tkinter，只負責顯示與接收操作；掃描與切換在背景執行緒，結果經佇列回主執行緒更新畫面。"
      "視窗依角色顯示不同功能：組員端只有「切換到所選」「自動挑選並切換」「立即同步」；伺服器與單機另有以下編輯功能。功能包括 MAC 池清單（可用、他人使用中、本機使用中、固定占用、已知限速）、新增與刪除（目前使用中的 MAC 不可刪）、標記固定占用、"
      "編輯標籤（雙擊該列或按鈕）、切換到所選、自動挑選並切換、重新掃描、自動切換開關、立即檢查、匯入（相容舊設定檔）與匯出、產生組員設定檔，以及事件紀錄。沒有管理員權限時提供一鍵重新以管理員啟動。")
    shot = ROOT / "docs" / "gui_main.png"
    if shot.exists():
        H.figure(doc, shot, "AutoMAC 視窗（實機畫面）", max_w=14.5, max_h=13.0)
    h(2, "部署與監視器自動重掛")
    P("伺服器上的 AutoMAC 以排程工作（開機啟動、SYSTEM 身分）執行，預設為試運行；路徑在不屬於共用資料夾的目錄。")
    P("Pi 先前以 CIFS 掛載伺服器的共用資料夾，位址寫死伺服器 IP，伺服器換 MAC 後 IP 改變，掛載就一直指向舊位址，要等人重掛。"
      "Pi 上的錄影程式本來就設計得很穩（先存本機、背景推送、寫暫存檔後取代、確認大小一致），斷線期間不會掉資料；問題只在恢復要靠人。")
    P("修復做法：fstab 位址改用 mDNS 名稱（`LAB-SERVER.local`），再加一個每 30 秒檢查的 systemd 計時器，名稱解析出的 IP 與掛載使用的不同、或掛載讀不到就重新掛載。"
      "伺服器因此改成獨特名稱，避免與通用名稱衝突。")
    H.figure(doc, DIAG / "06_部署拓樸圖.png", "實驗室部署拓樸（可編輯檔：06_部署拓樸圖.drawio）", max_h=11.0)

    # ========================================================================= 陸
    h(1, "模組與函式說明")
    P("本章由原始碼的 docstring 自動產生，與程式一致。每個函式的詳細說明、設計理由與限制都寫在原始碼註解中。")
    groups = [("基礎層", ["macaddr.py", "stats.py", "config.py"]),
              ("系統與網路邊界", ["winshell.py", "nic.py", "traffic.py", "probe.py", "scanner.py", "presence.py"]),
              ("純邏輯層", ["detector.py", "policy.py", "replay.py"]),
              ("協調層", ["hub.py", "tlsutil.py", "ledger.py", "takeover.py", "switcher.py", "service.py", "events.py", "hooks.py"]),
              ("呈現層", ["cli.py", "gui/app.py", "gui/dialogs.py", "gui/elevate.py"])]
    for gname, files in groups:
        h(2, gname)
        for f in files:
            summary, rows = module_rows(SRC / f)
            h(3, f.replace("/", " / "))
            P(summary)
            if rows:
                H.table(doc, f"{f} 的公開項目", ["名稱", "類型", "說明"], rows, [5.0, 1.6, 9.4], font=9.5)

    # ========================================================================= 柒
    h(1, "驗證與反省")
    h(2, "自動化測試")
    P(f"目前共有 {total_tests} 項測試，全部通過。測試涵蓋真實樣本重播、各種失敗路徑的切換回滾，以及視窗操作。")
    H.table(doc, "測試項數", ["階段與範圍", "項數"], test_rows, [12.0, 4.0])
    h(2, "各階段驗證結果")
    H.table(doc, "各階段的真實驗證", ["階段", "驗證方式", "結果"],
            [["P1 偵測", "三份真實樣本重播；隨機忙碌流量 2 小時", "健康與使用者下載不誤判；使用者下載 98 Mbps 的 70 秒零探針；限速樣本 15 秒內判定；2 小時零探針"],
             ["P2 探針", "真實網路閒置連發與背景下載", "閒置有效，讀值 21 到 29 Mbps；背景下載時標為無效，其他流量 80 Mbps 以上"],
             ["P3 掃描與宣告", "真實掃描、伺服器端交叉比對、同機搶佔、雙機宣告", "7 台與伺服器端一致；搶佔恰好 1 勝；雙向發現成功"],
             ["P5 服務", "本機試運行 80 秒，中途下載 75 MB", "第一個探針約 8 秒後發出且健康；下載期間沒有探針"],
             ["P6 視窗", "實機擷取畫面與 10 項視窗測試", "版面正確；新增、刪除、固定占用、切換皆可用"],
             ["P7 端到端", "伺服器上用函式庫真的換 MAC 再換回", "網卡就緒 8.9 秒與 8.8 秒；另一台機器看到宣告 claiming 到 holding 與 IP 變化；Pi 約 1 分鐘自動重掛"]],
            [2.6, 5.6, 7.8], font=9.5)
    h(2, "反省與教訓")
    H.bullets(doc, ["第三方介面一定要真的跑。zeroconf 的 `update_service` 要求明確的 server 名稱，單元測試抓不到，是實機測試才炸出來。",
                    "執行緒競爭要靠事先想，測試抓不到。探針執行緒與主迴圈同時取樣會產生時間間隔極短的樣本，一個封包就能算出假尖峰，所以把累計讀取改成現場讀、不動取樣基準。",
                    "GUI 一定要看到真實畫面。第一版截圖發現右側按鈕被清單擠成一個字寬。",
                    "測試資料要貼近現實。測試時鐘用接近 1970 年的數字，在 Windows 的 `time.mktime` 會溢位。",
                    "早期估的探針健康讀值（約 7 Mbps）與後來實測（21 到 29 Mbps）差很多，提醒健康讀值是範圍不是常數，門檻應以限速值為基準。",
                    "claim 失敗時原本會把自己正在用的 MAC 的宣告一起撤掉，別人會誤以為舊 MAC 空了；改成輸了就還原原宣告。"])

    # ========================================================================= 捌
    h(1, "限制、風險與後續工作")
    h(2, "已知限制")
    H.bullets(doc, ["每個 MAC 每天 10 GB、午夜恢復，是使用者描述的規則，未經校方文件證實；恢復時間可由設定調整。",
                    "限速狀態下探針的讀值由下載曲線推算，尚未在被限速時直接量測探針。",
                    "真的被限速時的完整自動流程（偵測、切換、驗證）尚未在實機觸發過，因為無法人為製造限速；邏輯由重播樣本與單元測試覆蓋。",
                    "ARP 掃描只看得到自己的網段；跨網段與不跑本工具的裝置只能靠 mDNS 與掃描互補。",
                    "mDNS 要求各機器防火牆放行 UDP 5353；伺服器已新增規則，正式安裝流程需處理這一步。",
                    "Pi 重掛最久約 3 分鐘（受 Linux CIFS 對舊連線 180 秒逾時影響）；可在 fstab 加 `echo_interval=10` 縮短，尚未測試。"])
    h(2, "風險")
    H.bullets(doc, ["伺服器啟用自動切換會讓它的 IP 改變，其他服務 玩家與 Pi 傳輸會短暫中斷，因此伺服器服務預設為試運行。",
                    "換 MAC 前請確認有權使用該 MAC，並遵守校方網路規定；本工具不繞過任何校方限制，只是在使用者已登記的 MAC 之間切換。",
                    "設定檔含真實 MAC，存放在使用者設定資料夾，不可上傳。"])
    h(2, "後續工作")
    H.bullets(doc, ["下一次被限速時，錄下真實樣本並直接量測探針，用重播工具驗證並微調門檻。",
                    "伺服器試運行一段時間，確認日誌沒有誤判後再啟用自動切換。",
                    "打包成單一執行檔（PyInstaller）並加系統匣圖示；Linux 版只需替換 `winshell`、`nic`、`scanner`。",
                    "整理成 GitHub 倉庫並開啟 CI（測試流程已備好）。"])

    # ========================================================================= 玖
    h(1, "使用說明")
    h(2, "安裝與第一次使用")
    H.code_block(doc, "python -m venv .venv\n.venv\\Scripts\\python -m pip install psutil zeroconf\nset PYTHONPATH=src\n"
                      "python -m automac pool add 00-00-5E-00-53-01 \"電腦 A\"\npython -m automac status\n"
                      "python -m automac run --dry-run\npython -m automac gui")
    h(2, "常用指令")
    H.table(doc, "命令列指令", ["指令", "說明"],
            [["`automac status`", "目前網卡、MAC、IP、池內各 MAC 的狀態"],
             ["`automac scan`", "掃描區網鄰居，並標出池內的 MAC"],
             ["`automac probe`", "立即探測一次"],
             ["`automac pool list|add|remove|fixed|import|export`", "管理 MAC 池；import 相容舊版設定檔"],
             ["`automac switch [MAC] [--force]`", "手動切換（不給 MAC 由政策挑選）；需要系統管理員"],
             ["`automac run [--dry-run] [--auto|--no-auto] [--duration 秒]`", "背景監看與自動切換，伺服器用"],
             ["`automac gui`", "開啟視窗"]],
            [7.0, 9.0])
    h(2, "伺服器服務")
    P("在伺服器以系統管理員執行 `install-service.ps1` 安裝排程工作（預設試運行，日誌在設定檔旁的 `automac.log`）；"
      "確認日誌沒有問題後，加 `-Enable` 才會真的自動切換；`-Remove` 移除。")

    # ========================================================================= 拾
    h(1, "參考文獻、技術與專案")
    h(2, "參考文獻")
    P("論文與標準的完整清單與是否讀過全文，見「文獻調查結果」表。另有：Cloudflare 的速度測試端點（`speed.cloudflare.com/__down`）作為探針目標，實測單次 25 MB 可用、100 MB 以上回 403。")
    h(2, "使用的技術")
    H.bullets(doc, ["TCP 新連線測速與慢啟動特性；小探針的偏低讀值與有效性檢查。",
                    "網卡累計位元組計數器與滑動視窗統計（中位數、平均、變異係數）。",
                    "UDP 觸發 ARP 的鄰居探索；Windows 鄰居表。",
                    "mDNS 與 DNS-SD（RFC 6762、6763）、名稱衝突探測、TTL 到期與告別封包；佔用協定（借用 RFC 5227）。",
                    "Windows NetAdapter 的 `Set-NetAdapter -MacAddress`、DHCP 就緒輪詢與失敗回滾。",
                    "Windows 排程工作（SYSTEM、開機啟動）。",
                    "Linux：CIFS 掛載、systemd automount 與計時器、nss-mdns 名稱解析。",
                    "draw.io（mxGraph XML）可編輯圖表；Word 多層次清單、TOC 與 SEQ 欄位。"])
    h(2, "使用的專案")
    H.table(doc, "使用的軟體專案", ["專案", "版本", "授權", "用途"],
            [["Python", "3.12", "PSF", "執行環境；標準函式庫的 tkinter、urllib、threading、unittest"],
             ["python-zeroconf", "0.154.0", "LGPL-2.1 以上", "mDNS 宣告與發現（動態連結，pip 安裝）"],
             ["psutil", "7.2.2", "BSD-3-Clause", "讀網卡計數器"],
             ["ifaddr", "0.2.0", "MIT", "zeroconf 的相依套件"],
             ["matplotlib", "3.11.2", "PSF 類授權（Matplotlib License）", "產生文件用的圖（僅開發時使用）"],
             ["python-docx", "1.2.0", "MIT", "產生本文件（僅開發時使用）"],
             ["Pillow", "13.0.0", "HPND", "量測圖片尺寸（僅開發時使用）"],
             ["draw.io（diagrams.net）格式", "mxGraph XML", "Apache-2.0", "可編輯圖表檔案格式"],],
            [4.2, 2.2, 3.8, 5.8], font=9.5)
    P("評估過但未採用：MAC-SPOOFER、MacForger、change-mac（換 MAC，授權不一且改登錄檔）、ShaperProbe 及其 fork（限速型態偵測，停更且需對端伺服器）。")

    # ========================================================================= 附錄（不編號）
    H.unnumbered_heading(doc, "附錄　設定欄位一覽".replace("　", " "))
    P("以下由 `config.py` 的 `Settings` 自動產生，預設值都有實測依據（見「實測資料與分析」一章）。")
    H.table(doc, "設定欄位與預設值", ["欄位", "預設值", "說明"], settings_rows(), [4.6, 3.4, 8.0], font=9.5)
    P("角色相關欄位（role、hub_url、hub_port、hub_token、hub_sync_s、lease_ttl_s、pending_ttl_s）的用法見「伺服器登記處與組員端」一節；hub_token 屬於機密，不可上傳。")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    H.space_after_tables(doc)
    doc.save(OUT)
    print("已輸出", OUT)


if __name__ == "__main__":
    build()
