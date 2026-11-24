"""視窗用的小元件與配色。標準函式庫 tkinter，不增加依賴。

配色（2026-11-24 使用者提供的粉彩天空色票）：蜜桃白、粉、薰衣草灰紫、霧藍；最舊版介面也是這個調性。
上一版的問題：直接沿用舊版色碼，薄荷綠、青藍、粉紅的飽和度與明度各不相同，加上一個鮮綠，看起來很雜；
圓角用 Canvas 的 smooth 多邊形，角度不一致，卡片與清單又是直角。這一版的做法：
    1. 所有填色都取同一組飽和度（約 30 到 35%）與相近明度，只換色相：藍＝一般按鈕、綠＝主要操作、粉＝清單、薰衣草＝狀態與次要按鈕。
       綠色是用霧藍的飽和度與明度算出來的（HSL 150 度、33%、52%），不是另外挑一個鮮綠。
    2. 圓角全部用同一個函式畫：四個角各貼一張預先算好抗鋸齒的小圖（邊緣像素與背景色混合），中間用矩形補滿；
       按鈕、狀態卡、清單卡、日誌卡共用，圓角半徑一致。
shortcut: Tk 沒有透明度混合，所以角落圖要預先知道「外面的底色」（outer 參數必須等於父元件底色）；
要做會變色的背景或漸層，就得換成 customtkinter / Pillow（目前不為此多裝套件）。
"""
from __future__ import annotations

import tkinter as tk

# ---- 色票：蜜桃白、粉、薰衣草、霧藍（取自使用者提供的色票圖），再依同一組飽和度推出綠與配套色
BG = "#FAEEE8"            # 視窗底色：蜜桃白再淡一點（讓卡片浮得起來）
STATUS_BG = "#C3C8E2"     # 狀態卡：薰衣草（比色票淡，深色文字才讀得清楚）
LIST_BG = "#EBCDD0"       # 清單卡：粉
BTN = "#5F7DB1"           # 一般按鈕：霧藍（HSL 219°、33%、53%）
BTN_HOVER = "#506B9B"
GO = "#5CAD85"            # 主要操作：綠，同樣的飽和度與明度（HSL 150°、33%、52%）
GO_HOVER = "#4D9A74"
SMALL = "#8E98C6"         # 次要小按鈕：薰衣草深一階
SMALL_HOVER = "#7C87B7"
QUIET = "#B4B1C6"         # 解除安裝等不想讓人手滑點的按鈕：灰紫
QUIET_HOVER = "#A19EB6"
DISABLED = "#CFCAD3"
TEXT = "#3B3A4A"
SUBTLE = "#7A7890"
CONSOLE_BG = "#2D3148"    # 日誌卡：深藍灰（同一個藍色相，不用純黑）
CONSOLE_FG = "#E4E6F2"
CONSOLE_WARN, CONSOLE_ERR, CONSOLE_OK = "#EBCB8B", "#F2A0A8", "#9BD8B4"
FONT = "Microsoft JhengHei UI"
RADIUS = 14


def _hex(c: str) -> tuple[int, int, int]:
    return int(c[1:3], 16), int(c[3:5], 16), int(c[5:7], 16)


def _corner_image(root, r: int, fill: str, outer: str, which: str) -> tk.PhotoImage:
    """r×r 的圓角單角圖：圓內是 fill、圓外是 outer，邊緣依覆蓋比例混色（抗鋸齒）。which：tl、tr、bl、br。
    圖存在最上層視窗的屬性裡（PhotoImage 必須有人持有參照，也不能跨 Tk 直譯器共用）。"""
    cache = root.__dict__.setdefault("_corner_cache", {})
    key = (r, fill, outer, which)
    if key in cache:
        return cache[key]
    f, o = _hex(fill), _hex(outer)
    rows = []
    for y in range(r):
        row = []
        for x in range(r):
            xx = x if which in ("tl", "bl") else r - 1 - x          # 鏡射成左上角的座標來算
            yy = y if which in ("tl", "tr") else r - 1 - y
            d = ((r - (xx + 0.5)) ** 2 + (r - (yy + 0.5)) ** 2) ** 0.5
            a = max(0.0, min(1.0, r - d + 0.5))                      # 圓內覆蓋比例
            row.append("#%02x%02x%02x" % tuple(round(f[i] * a + o[i] * (1 - a)) for i in range(3)))
        rows.append("{" + " ".join(row) + "}")
    img = tk.PhotoImage(master=root, width=r, height=r)
    img.put(" ".join(rows))
    cache[key] = img
    return img


def draw_round_rect(cv: tk.Canvas, w: int, h: int, r: int, fill: str, outer: str, tag: str = "bg") -> None:
    """在 canvas 上畫圓角矩形：兩塊矩形補滿，四個角貼抗鋸齒小圖。尺寸太小時縮小半徑。"""
    r = max(0, min(r, w // 2, h // 2))
    cv.delete(tag)
    if r == 0:
        cv.create_rectangle(0, 0, w, h, fill=fill, outline=fill, tags=tag)
        return
    root = cv.winfo_toplevel()
    cv.create_rectangle(r, 0, w - r, h, fill=fill, outline=fill, tags=tag)
    cv.create_rectangle(0, r, w, h - r, fill=fill, outline=fill, tags=tag)
    for which, x, y in (("tl", 0, 0), ("tr", w - r, 0), ("bl", 0, h - r), ("br", w - r, h - r)):
        cv.create_image(x, y, image=_corner_image(root, r, fill, outer, which), anchor="nw", tags=tag)
    cv.tag_lower(tag)


class RoundButton(tk.Canvas):
    """圓角按鈕。介面：set_text、set_enabled、text（目前文字）、enabled；點擊呼叫 command()。
    outer 必須等於父元件的底色（角落抗鋸齒要跟它混色）。"""

    def __init__(self, master, text: str, command, *, bg: str = BTN, hover: str = BTN_HOVER, fg: str = "white",
                 outer: str = BG, size: int = 11, bold: bool = True, width: int = 120, height: int = 32, radius: int = 12):
        super().__init__(master, width=width, height=height, highlightthickness=0, bd=0, bg=outer, cursor="hand2")
        self.text, self.enabled, self.command = text, True, command
        self._bg, self._hover, self._fg, self._radius, self._outer = bg, hover, fg, radius, outer
        self._font = (FONT, size, "bold" if bold else "normal")
        self._over = False
        self.bind("<Configure>", lambda _e: self._draw())
        self.bind("<Enter>", lambda _e: self._set_over(True))
        self.bind("<Leave>", lambda _e: self._set_over(False))
        self.bind("<ButtonRelease-1>", self._click)
        self._draw()

    def _set_over(self, v: bool) -> None:
        self._over = v
        self._draw()

    def _click(self, e) -> None:
        if self.enabled and 0 <= e.x <= self.winfo_width() and 0 <= e.y <= self.winfo_height():
            self.command()

    def set_text(self, text: str) -> None:
        if text != self.text:
            self.text = text
            self._draw()

    def set_enabled(self, on: bool) -> None:
        if on != self.enabled:
            self.enabled = on
            self.configure(cursor="hand2" if on else "arrow")
            self._draw()

    def _draw(self) -> None:
        w, h = max(self.winfo_width(), int(self["width"])), max(self.winfo_height(), int(self["height"]))
        fill = (self._hover if self._over else self._bg) if self.enabled else DISABLED
        draw_round_rect(self, w, h, self._radius, fill, self._outer)
        self.delete("label")
        self.create_text(w // 2, h // 2, text=self.text, fill=self._fg if self.enabled else "#F4F1F4", font=self._font, tags="label")


class Card(tk.Canvas):
    """圓角卡片：外層是畫圓角底色的 Canvas，內容放在 .inner（一般的 tk.Frame，底色相同）。
    fit=True：高度跟著內容（狀態卡）；fit=False：高度由外層 pack 決定，內容跟著撐滿（清單、日誌）。"""

    def __init__(self, master, fill: str, *, outer: str = BG, radius: int = RADIUS + 2, pad: int = 12, fit: bool = False, height: int = 60):
        super().__init__(master, highlightthickness=0, bd=0, bg=outer, height=height)
        self.fill, self.outer, self.radius, self.pad, self.fit = fill, outer, radius, pad, fit
        self.inner = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, window=self.inner, anchor="nw")
        self.bind("<Configure>", self._layout)
        if fit:
            self.inner.bind("<Configure>", lambda _e: self.configure(height=self.inner.winfo_reqheight() + 2 * pad))

    def _layout(self, e) -> None:
        draw_round_rect(self, e.width, e.height, self.radius, self.fill, self.outer)
        kw = {"width": max(1, e.width - 2 * self.pad)}
        if not self.fit:
            kw["height"] = max(1, e.height - 2 * self.pad)
        self.itemconfigure(self._win, **kw)


def style_ttk(root) -> None:
    """清單（Treeview）與捲軸的配色；用 clam 主題，因為預設的 Windows 主題不吃底色設定。"""
    from tkinter import ttk
    st = ttk.Style(root)
    try:
        st.theme_use("clam")
    except tk.TclError:
        pass
    st.configure("Treeview", background=LIST_BG, fieldbackground=LIST_BG, foreground=TEXT, rowheight=28,
                 font=(FONT, 10), borderwidth=0)
    st.configure("Treeview.Heading", background=LIST_BG, foreground=TEXT, font=(FONT, 10, "bold"), relief="flat", borderwidth=0)
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])          # 拿掉預設的細邊框，讓清單融進圓角卡片
    st.map("Treeview", background=[("selected", BTN)], foreground=[("selected", "white")])
    st.map("Treeview.Heading", background=[("active", LIST_BG)])
    for name, trough in (("Vertical.TScrollbar", LIST_BG),):
        st.configure(name, background=SMALL, troughcolor=trough, bordercolor=trough, arrowcolor="white", relief="flat")
    st.configure("Log.Vertical.TScrollbar", background="#4A5075", troughcolor=CONSOLE_BG, bordercolor=CONSOLE_BG, arrowcolor=CONSOLE_FG,
                 relief="flat")
