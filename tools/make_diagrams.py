"""產生專案的架構圖與流程圖：同一份圖面規格輸出兩種格式。
    docs/diagrams/*.drawio   可編輯（用 draw.io / diagrams.net 開啟，連線會自動重新排）
    docs/diagrams/*.png      給 Word 文件嵌入（中文標楷體、英數 Times New Roman）
為什麼這樣做：開發守則要求每個技術都有「可編輯」的圖；直接畫在 Word 裡的圖不能再編輯，
所以先用規格檔產生可編輯的 .drawio，再由同一份規格繪出 PNG，兩者內容保證一致。
用法（需要 matplotlib，使用 <含 matplotlib 的 Python 環境>）：python tools/make_diagrams.py
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from xml.sax.saxutils import escape

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import FancyBboxPatch, Polygon  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "docs" / "diagrams"
plt.rcParams["font.family"] = ["Times New Roman", "DFKai-SB"]   # 英數 Times New Roman、中文標楷體
plt.rcParams["axes.unicode_minus"] = False

COLORS = {   # kind: (填色, 邊框)
    "process": ("#dae8fc", "#6c8ebf"),
    "decision": ("#fff2cc", "#d6b656"),
    "start": ("#d5e8d4", "#82b366"),
    "end": ("#f8cecc", "#b85450"),
    "system": ("#e1d5e7", "#9673a6"),
    "data": ("#f5f5f5", "#666666"),
    "ui": ("#ffe6cc", "#d79b00"),
    "logic": ("#d5e8d4", "#82b366"),
    "boundary": ("#dae8fc", "#6c8ebf"),
    "base": ("#f5f5f5", "#666666"),
    "band_ui": ("#fff6ea", "#d79b00"),
    "band_sys": ("#f6effa", "#9673a6"),
    "band_logic": ("#f0f8ee", "#82b366"),
    "band_edge": ("#eef4fc", "#6c8ebf"),
    "band_base": ("#fafafa", "#999999"),
}


@dataclass
class Node:
    id: str
    label: str
    x: float          # 左上角
    y: float
    w: float = 150
    h: float = 54
    kind: str = "process"
    top: bool = False       # True：文字貼在框上緣（分層底色框用）


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""


@dataclass
class Diagram:
    name: str
    title: str
    width: float
    height: float
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    def node(self, id: str) -> Node:
        return next(n for n in self.nodes if n.id == id)


# ----------------------------------------------------------------------------- 輸出：draw.io
def to_drawio(d: Diagram) -> str:
    cells = ['<mxCell id="0"/>', '<mxCell id="1" parent="0"/>']
    for n in d.nodes:
        fill, stroke = COLORS[n.kind]
        shape = {"decision": "rhombus", "start": "ellipse", "end": "ellipse"}.get(n.kind, "rounded=1;arcSize=12")
        shape = shape if "=" in shape else f"shape={shape}" if shape != "rhombus" else "rhombus"
        style = (f"{shape};whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};"
                 "fontFamily=DFKai-SB;fontSize=13;" + ("verticalAlign=top;" if n.top else ""))
        label = escape(n.label).replace("\n", "<br>")
        cells.append(f'<mxCell id="{n.id}" value="{label}" style="{style}" vertex="1" parent="1">'
                     f'<mxGeometry x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" as="geometry"/></mxCell>')
    for i, e in enumerate(d.edges):
        style = ("edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;html=1;endArrow=block;"
                 "fontFamily=DFKai-SB;fontSize=12;")
        cells.append(f'<mxCell id="e{i}" value="{escape(e.label)}" style="{style}" edge="1" parent="1" '
                     f'source="{e.src}" target="{e.dst}"><mxGeometry relative="1" as="geometry"/></mxCell>')
    return ('<mxfile host="app.diagrams.net"><diagram name="' + escape(d.title) + '"><mxGraphModel dx="1000" dy="700" '
            f'grid="1" gridSize="10" page="1" pageWidth="{int(d.width)}" pageHeight="{int(d.height)}"><root>'
            + "".join(cells) + "</root></mxGraphModel></diagram></mxfile>")


# ----------------------------------------------------------------------------- 輸出：PNG
def _edge_point(n: Node, tx: float, ty: float) -> tuple[float, float]:
    """從節點中心朝 (tx,ty) 射線與節點邊界的交點（菱形與矩形分別計算）。"""
    cx, cy = n.x + n.w / 2, n.y + n.h / 2
    dx, dy = tx - cx, ty - cy
    if dx == 0 and dy == 0:
        return cx, cy
    if n.kind == "decision":
        k = abs(dx) / (n.w / 2) + abs(dy) / (n.h / 2)
        s = 1 / k
    else:
        s = min((n.w / 2) / abs(dx) if dx else math.inf, (n.h / 2) / abs(dy) if dy else math.inf)
    return cx + dx * s, cy + dy * s


def to_png(d: Diagram, path: Path) -> None:
    fig, ax = plt.subplots(figsize=(d.width / 100, d.height / 100), dpi=150)
    ax.set_xlim(0, d.width)
    ax.set_ylim(d.height, 0)
    ax.axis("off")
    ax.set_title(d.title, fontsize=15, pad=8)
    for e in d.edges:
        a, b = d.node(e.src), d.node(e.dst)
        acx, acy, bcx, bcy = a.x + a.w / 2, a.y + a.h / 2, b.x + b.w / 2, b.y + b.h / 2
        p1, p2 = _edge_point(a, bcx, bcy), _edge_point(b, acx, acy)
        ax.annotate("", xy=p2, xytext=p1, arrowprops=dict(arrowstyle="-|>", color="#444", lw=1.2, shrinkA=0, shrinkB=0))
        if e.label:
            mx, my = (p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2
            ax.text(mx + 4, my - 4, e.label, fontsize=10, color="#222", ha="left", va="bottom",
                    bbox=dict(fc="white", ec="none", alpha=0.85, pad=1))
    for n in d.nodes:
        fill, stroke = COLORS[n.kind]
        if n.kind == "decision":
            pts = [(n.x + n.w / 2, n.y), (n.x + n.w, n.y + n.h / 2), (n.x + n.w / 2, n.y + n.h), (n.x, n.y + n.h / 2)]
            ax.add_patch(Polygon(pts, closed=True, fc=fill, ec=stroke, lw=1.4))
        else:
            r = n.h / 2 if n.kind in ("start", "end") else 8
            ax.add_patch(FancyBboxPatch((n.x, n.y), n.w, n.h, boxstyle=f"round,pad=0,rounding_size={r}",
                                        fc=fill, ec=stroke, lw=1.4))
        if n.top:
            ax.text(n.x + n.w / 2, n.y + 8, n.label, ha="center", va="top", fontsize=12, color="#333")
        else:
            ax.text(n.x + n.w / 2, n.y + n.h / 2, n.label, ha="center", va="center", fontsize=11, linespacing=1.25)
    fig.tight_layout()
    fig.savefig(path)
    plt.close(fig)


# ----------------------------------------------------------------------------- 圖面規格
def d_architecture() -> Diagram:
    """分層底色框＋框內模組；只畫層與層之間的依賴方向，不畫每條模組間的線（會糾纏難讀，細節見文件的模組表）。"""
    d = Diagram("01_架構分層圖", "圖：AutoMAC 架構分層（上層只依賴下層）", 1000, 790)
    N = d.nodes
    bands = [("band_ui", "呈現層：只做顯示與接收操作，不含判斷", 50, 90),
             ("band_sys", "協調層：把下層串起來、排程、發出事件；伺服器登記處與用量帳本", 186, 150),
             ("band_logic", "純邏輯層：不碰系統與網路，時間由外部傳入，可用真實樣本重播", 382, 90),
             ("band_edge", "系統與網路邊界：碰作業系統與網路，都有小介面，測試時換成假物件", 518, 90),
             ("band_base", "基礎層：格式、統計、設定檔、PowerShell 唯一入口", 654, 90)]
    for k, label, y, h in bands:
        N.append(Node(k, label, 40, y, 920, h, k, top=True))

    def box(id: str, label: str, x: float, y: float, kind: str) -> None:
        N.append(Node(id, label, x, y, 200, 48, kind))

    box("cli", "cli\n命令列", 120, 90, "ui")
    box("gui", "gui/\n視窗（tkinter）", 400, 90, "ui")
    box("service", "service\n背景主迴圈、入網配發", 120, 226, "system")
    box("switcher", "switcher\n切換流程與回滾", 400, 226, "system")
    box("events", "events / hooks\n事件、外掛命令", 680, 226, "system")
    box("hub", "hub\n登記處、組員端、API", 120, 280, "system")
    box("ledger", "ledger\n用量帳本", 400, 280, "system")
    box("takeover", "takeover\n接手背景服務", 680, 280, "system")
    box("detector", "detector\n限速判斷狀態機", 120, 422, "logic")
    box("policy", "policy\n選 MAC、頻率限制", 400, 422, "logic")
    box("replay", "replay\n樣本重播", 680, 422, "logic")
    for i, (id, lab) in enumerate([("probe", "probe\n小探針"), ("traffic", "traffic\n網卡流量"), ("scanner", "scanner\n鄰居掃描"),
                                   ("presence", "presence\nmDNS 宣告"), ("nic", "nic\n網卡與換 MAC"),
                                   ("tlsutil", "tlsutil\nTLS 1.3、憑證釘選")]):
        N.append(Node(id, lab, 52 + i * 150, 558, 140, 48, "boundary"))
    box("config", "config\n設定檔與狀態檔", 120, 694, "base")
    box("macaddr", "macaddr / stats\nMAC 格式與統計", 400, 694, "base")
    box("winshell", "winshell\nPowerShell 入口", 680, 694, "base")
    for a, b, t_ in [("band_ui", "band_sys", "呼叫"), ("band_sys", "band_logic", "使用"),
                     ("band_logic", "band_edge", "取得資料／下指令"), ("band_edge", "band_base", "依賴")]:
        d.edges.append(Edge(a, b, t_))
    return d


def d_detect() -> Diagram:
    d = Diagram("02_偵測流程圖", "圖：限速偵測流程（每秒一次）", 1000, 900)
    N = d.nodes
    N += [Node("s", "每秒取樣\n網卡下載 rx", 420, 30, 160, 50, "start"),
          Node("q", "安靜？\n10 秒中位數＜3 Mbps\n且最近 3 秒平均＜3 Mbps", 380, 110, 240, 90, "decision"),
          Node("busy", "流量大\n不探測、不打擾", 700, 130, 180, 50, "end"),
          Node("when", "該探測了？\n（確認、換後驗證、\n平線指紋、閒置間隔）", 380, 240, 240, 90, "decision"),
          Node("wait", "等下一秒", 700, 260, 150, 50, "data"),
          Node("probe", "發 256 KB 探針\n記錄探針前後網卡位元組", 400, 370, 200, 56, "process"),
          Node("ok", "探針完成？", 420, 465, 160, 70, "decision"),
          Node("down", "net_down：不計次\n退避 15 秒", 700, 475, 180, 50, "end"),
          Node("valid", "有效？\n（期間其他流量＜3 Mbps）", 400, 575, 200, 80, "decision"),
          Node("inv", "無效：不計次\n退避 10 秒", 700, 585, 180, 50, "data"),
          Node("slow", "低於 2 Mbps？", 420, 695, 160, 70, "decision"),
          Node("healthy", "healthy：\n偏慢次數歸零", 700, 705, 180, 50, "start"),
          Node("strike", "偏慢次數 +1\n≥ 2 次？", 420, 805, 160, 70, "decision"),
          Node("again", "5 秒後再確認\n（60 秒內有效）", 150, 815, 180, 50, "data"),
          Node("thr", "判定限速\n→ 交給切換流程", 700, 815, 180, 50, "end")]
    for a, b, t in [("s", "q", ""), ("q", "busy", "否"), ("q", "when", "是"), ("when", "wait", "否"), ("when", "probe", "是"),
                    ("probe", "ok", ""), ("ok", "down", "否"), ("ok", "valid", "是"), ("valid", "inv", "否"), ("valid", "slow", "是"),
                    ("slow", "healthy", "否"), ("slow", "strike", "是"), ("strike", "again", "否"), ("strike", "thr", "是")]:
        d.edges.append(Edge(a, b, t))
    return d


def d_switch() -> Diagram:
    d = Diagram("03_切換流程圖", "圖：切換流程（含失敗回滾）", 1000, 1000)
    N = d.nodes
    N += [Node("a", "判定限速", 420, 20, 160, 46, "start"),
          Node("b", "標記舊 MAC 限速\n到假定恢復時間", 400, 90, 200, 54, "process"),
          Node("c", "自動切換開啟？\nGovernor 允許？", 400, 170, 200, 80, "decision"),
          Node("c2", "只通知或停止輪流\n（太頻繁＝可能整池都慢）", 700, 180, 220, 60, "end"),
          Node("d", "取得「他人使用中」清單\n鄰居掃描＋mDNS 宣告", 390, 280, 220, 56, "process"),
          Node("e", "依輪流順序挑候選\n排除目前、固定占用、使用中、限速中", 360, 365, 280, 56, "process"),
          Node("f", "搶佔成功？\n（宣告 claiming 並等待）", 400, 450, 200, 80, "decision"),
          Node("f2", "讓給對方\n換下一個候選", 700, 460, 180, 56, "data"),
          Node("g", "切換前外掛命令\nset_mac 換 MAC", 400, 560, 200, 56, "process"),
          Node("h", "輪詢到「MAC 已變、\n網卡連線、拿到 IP」？", 390, 645, 220, 80, "decision"),
          Node("h2", "換回原 MAC\n釋放宣告，回報失敗", 700, 655, 200, 60, "end"),
          Node("i", "記錄切換次數\n重新綁定並宣告 holding", 390, 760, 220, 56, "process"),
          Node("j", "切換後外掛命令\n偵測器重設", 400, 840, 200, 54, "process"),
          Node("k", "下次安靜時驗證新 MAC\n也被限速就再換（受 Governor 限制）", 340, 920, 320, 56, "start")]
    for a, b, t in [("a", "b", ""), ("b", "c", ""), ("c", "c2", "否"), ("c", "d", "是"), ("d", "e", ""), ("e", "f", ""),
                    ("f", "f2", "否"), ("f", "g", "是"), ("g", "h", ""), ("h", "h2", "逾時"), ("h", "i", "是"), ("i", "j", ""),
                    ("j", "k", "")]:
        d.edges.append(Edge(a, b, t))
    d.edges.append(Edge("f2", "e", "下一個"))
    return d


def d_claim() -> Diagram:
    d = Diagram("04_佔用協定流程圖", "圖：MAC 佔用協定（借用 RFC 5227 的探測與宣告概念）", 1000, 560)
    N = d.nodes
    N += [Node("a", "想使用某 MAC", 420, 20, 160, 46, "start"),
          Node("b", "宣告 state=claiming\n附毫秒時間戳", 400, 100, 200, 56, "process"),
          Node("c", "等待 1.5 秒＋隨機抖動\n讓同伴回應", 400, 190, 200, 56, "process"),
          Node("d", "看同一 MAC 的其他宣告", 400, 280, 200, 50, "process"),
          Node("e", "有人已 holding？", 410, 360, 180, 70, "decision"),
          Node("f", "有人 claiming 且\n（時間戳, 名稱）更小？", 380, 455, 240, 80, "decision"),
          Node("lose", "放棄：還原原宣告\n換下一個候選", 700, 380, 200, 56, "end"),
          Node("win", "取得使用權\n接著換 MAC，換完宣告 holding", 700, 470, 240, 60, "start")]
    for a, b, t in [("a", "b", ""), ("b", "c", ""), ("c", "d", ""), ("d", "e", ""), ("e", "lose", "是"), ("e", "f", "否"),
                    ("f", "lose", "是"), ("f", "win", "否")]:
        d.edges.append(Edge(a, b, t))
    return d


def d_runtime() -> Diagram:
    d = Diagram("05_執行時整體流程圖", "圖：服務執行時的執行緒與資料流", 1000, 560)
    N = d.nodes
    N += [Node("main", "主迴圈執行緒\n每秒 tick", 60, 60, 190, 60, "system"),
          Node("samp", "TrafficSampler\n讀網卡計數器→Mbps", 60, 200, 190, 60, "boundary"),
          Node("det", "Detector\n要不要探測？", 60, 340, 190, 60, "logic"),
          Node("work", "工作執行緒（單一）\n探針、切換排隊執行", 400, 200, 220, 70, "system"),
          Node("probe", "run_probe\n小探針＋有效性", 330, 340, 200, 60, "boundary"),
          Node("sw", "Switcher\n掃描、宣告、換 MAC、回滾", 480, 450, 240, 60, "system"),
          Node("bus", "EventBus\n事件匯流排", 750, 200, 190, 60, "data"),
          Node("ui", "CLI 輸出／視窗紀錄／\n日誌檔 automac.log", 750, 340, 200, 70, "ui"),
          Node("pres", "Presence\nmDNS 宣告與發現", 790, 450, 180, 60, "boundary")]
    for a, b, t in [("main", "samp", "取樣"), ("samp", "det", "rx 樣本"), ("det", "work", "需要探測"), ("work", "probe", ""),
                    ("probe", "det", "探針結果"), ("det", "sw", "判定限速"), ("work", "sw", ""), ("work", "bus", "事件"),
                    ("sw", "bus", "事件"), ("bus", "ui", "訂閱"), ("sw", "pres", "佔用與宣告")]:
        d.edges.append(Edge(a, b, t))
    return d


def d_deploy() -> Diagram:
    d = Diagram("06_部署拓樸圖", "圖：實驗室部署（同一台不可管理交換機）", 1000, 560)
    N = d.nodes
    N += [Node("net", "校園網\n（限速發生在閘道之後）", 380, 20, 240, 56, "end"),
          Node("gw", "閘道", 420, 110, 160, 46, "data"),
          Node("sw", "不可管理交換機\n（二層網路，含另一子網的裝置）", 360, 200, 280, 60, "data"),
          Node("pc", "實驗室電腦（Win10）\nAutoMAC 視窗／服務", 40, 330, 230, 70, "system"),
          Node("srv", "伺服器 LAB-SERVER（Win11）\nAutoMAC 服務、共用資料夾", 370, 330, 260, 70, "system"),
          Node("pi", "監視器 Pi4 pi-monitor\nYOLO 錄影 + nas-heal 自動重掛", 730, 330, 250, 70, "system"),
          Node("mdns", "mDNS（UDP 5353）互相宣告\n誰在用哪個 MAC", 40, 470, 260, 56, "boundary"),
          Node("smb", "SMB 共用資料夾\n（以 LAB-SERVER.local 名稱掛載）", 600, 470, 300, 56, "boundary")]
    for a, b, t in [("net", "gw", ""), ("gw", "sw", ""), ("sw", "pc", ""), ("sw", "srv", ""), ("sw", "pi", ""),
                    ("pc", "mdns", ""), ("srv", "mdns", ""), ("pi", "smb", "影像"), ("smb", "srv", "寫入")]:
        d.edges.append(Edge(a, b, t))
    return d


def d_hub() -> Diagram:
    d = Diagram("07_伺服器與組員端分配流程圖", "圖：組員端同步清單、向伺服器要 MAC（伺服器登記處）", 1000, 480)
    N = d.nodes
    N += [Node("c1", "組員端（exe／視窗，可有多台）\n清單唯讀、自動或手動切換", 30, 40, 280, 70, "ui"),
          Node("api", "伺服器 HTTPS API\nTLS 1.3＋憑證釘選＋token\nGET /pool　POST /allocate　/report　/usage", 410, 30, 360, 90, "boundary"),
          Node("srv", "伺服器自己的切換\nLocalPort，不經網路", 810, 40, 170, 70, "system"),
          Node("sync", "每 30 秒：抓清單、回報目前 MAC 與用量\nMAC 不在池內就自動向伺服器要一個", 30, 190, 340, 70, "process"),
          Node("hub", "Hub 登記處\n池、租約、預約、限速紀錄、用量帳本", 390, 190, 380, 70, "logic"),
          Node("cfg", "config.json、usage.json\n（只能在伺服器改）", 810, 190, 170, 70, "data"),
          Node("fb", "連不上伺服器時：用本機快取的清單\n＋mDNS 宣告＋鄰居掃描備援", 30, 340, 340, 70, "end"),
          Node("scan", "區網鄰居掃描\n看得到不跑 AutoMAC 的裝置", 390, 340, 380, 70, "boundary")]
    for a, b, t in [("c1", "api", "同步／回報"), ("srv", "hub", ""), ("api", "hub", ""), ("hub", "cfg", ""),
                    ("hub", "scan", "分配時重掃"), ("c1", "sync", ""), ("sync", "fb", "連不上（逾時）")]:
        d.edges.append(Edge(a, b, t))
    return d


def d_client() -> Diagram:
    d = Diagram("08_客戶端背景服務與視窗", "圖：組員端——背景服務（開機自動啟動）與視窗檢視器", 1000, 540)
    N = d.nodes
    N += [Node("dbl", "組員雙擊 automac.exe\n（解壓縮資料夾，可事後刪除）", 20, 30, 340, 70, "start"),
          Node("ens", "launcher：確保背景服務\n沒裝就裝、版本不同就升級、沒在跑就啟動", 20, 160, 340, 80, "process"),
          Node("inst", "svcinstall：複製 exe 到 ProgramData\n登記排程工作（開機啟動、SYSTEM、當掉重啟）\n與防火牆規則", 20, 300, 340, 90, "system"),
          Node("gui", "視窗（檢視器）\n看狀態、下指令；關掉不影響服務", 400, 30, 300, 70, "ui"),
          Node("api", "本機控制介面 127.0.0.1\n（token 檔，只有管理員讀得到）", 400, 160, 300, 70, "boundary"),
          Node("svc", "背景服務 automac.exe run\n偵測／換 MAC／同步／用量（低優先權）", 400, 300, 300, 80, "logic"),
          Node("hub", "伺服器 HTTPS API（每 30 秒同步）\nTLS 1.3＋釘選＋token", 740, 300, 240, 80, "boundary"),
          Node("wd", "看門狗：讀不到網卡計數器 10 秒\n→ 警告並重新偵測網卡", 740, 430, 240, 80, "end"),
          Node("tray", "系統匣圖示 automac.exe tray\n由 svcinstall 登記，登入時自動啟動\n綠／黃／紅表示狀態\n點一下開視窗、右鍵選單含解除安裝", 735, 30, 255, 110, "ui")]
    for a_, b_, t_ in [("dbl", "ens", ""), ("ens", "inst", "需要安裝／升級"), ("ens", "gui", "開視窗"), ("inst", "svc", "啟動"),
                       ("gui", "api", "狀態／切換／探測"), ("api", "svc", ""), ("svc", "hub", ""), ("svc", "wd", ""), ("tray", "api", "讀狀態、跳通知")]:
        d.edges.append(Edge(a_, b_, t_))
    return d


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for build in (d_architecture, d_detect, d_switch, d_claim, d_runtime, d_deploy, d_hub, d_client):
        d = build()
        (OUT / f"{d.name}.drawio").write_text(to_drawio(d), encoding="utf-8")
        to_png(d, OUT / f"{d.name}.png")
        print("已產生", d.name)


if __name__ == "__main__":
    main()
