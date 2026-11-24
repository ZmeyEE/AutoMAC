"""主視窗（標準函式庫 tkinter，不增加依賴）。這一層只負責顯示與接收操作，
判斷與動作全部交給 Service；要換成別的 GUI 套件時，只需重寫這個檔案。

版面（由上到下）
    狀態區     網卡、MAC、IP、流量、狀態、最近探針、是否系統管理員
    池內清單   每個 MAC 的狀態（可用／他人使用中／本機使用中／固定占用／已知限速）＋操作按鈕
    控制列     自動切換開關、立即檢查、匯入／匯出（匯入匯出只有伺服器與單機有）

依角色（settings.role）顯示不同功能：
    client     組員端：清單由伺服器同步，只能「切換到所選」「自動挑選並切換」「立即同步」，不能改清單
    hub        伺服器：可新增、刪除、改標籤、設固定占用、匯入匯出、產生組員設定檔
    standalone 單機：同伺服器（沒有組員設定檔）
    事件紀錄   服務發出的所有事件

執行緒規則：tkinter 只能在主執行緒更新畫面。背景工作（掃描、切換）把結果放進 queue，
由 _poll 每 500 毫秒在主執行緒取出並更新。
"""
from __future__ import annotations

import json
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from .. import config
from ..config import Settings, StateStore
from ..events import Event, EventBus
from ..ledger import fmt_bytes
from ..macaddr import fmt
from ..nic import NicError
from ..policy import MacStatus
from ..service import Service
from .dialogs import AddMacDialog
from .elevate import relaunch_as_admin
from .widgets import (BG, CONSOLE_BG, CONSOLE_ERR, CONSOLE_FG, CONSOLE_OK, CONSOLE_WARN, FONT, GO, GO_HOVER, LIST_BG, QUIET, QUIET_HOVER,
                      SMALL, SMALL_HOVER, STATUS_BG, SUBTLE, TEXT, Card, RoundButton, style_ttk)
from ..tray import WINDOW_TITLE

KIND_TEXT = {"current": "本機使用中", "fixed": "固定占用", "in_use": "他人使用中", "throttled": "已知限速", "free": "可用"}
KIND_COLOR = {"current": "#2E6B4A", "fixed": "#7A5A1E", "in_use": "#A24A55", "throttled": "#9A6A2A", "free": "#3B3A4A"}


class App(tk.Tk):
    def __init__(self, settings: Settings, cfg_path: Path, service: Service, bus: EventBus):
        super().__init__()
        self.s, self.path, self.svc, self.bus = settings, cfg_path, service, bus
        self.title(WINDOW_TITLE)
        self.geometry("720x900" if settings.is_client else "920x720")
        self.minsize(640, 740 if settings.is_client else 600)
        self._q: queue.Queue = queue.Queue()
        self._pool_cache: list[MacStatus] = []
        self._refreshing = False
        self._ticks = 0
        self.bus.subscribe(lambda ev: self._q.put(("event", ev)))
        self._build()
        self.protocol("WM_DELETE_WINDOW", self._close)
        self._after_ids = [self.after(300, lambda: self.refresh_pool(force=True)), self.after(500, self._poll)]

    # ------------------------------------------------------------------ 版面
    def _label(self, parent, text="", var=None, bg=BG, size=10, bold=False, fg=TEXT, **kw) -> tk.Label:
        return tk.Label(parent, text=text, textvariable=var, bg=bg, fg=fg, font=(FONT, size, "bold" if bold else "normal"), **kw)

    def _build(self) -> None:
        if self.s.is_client:
            self._build_styled()
        else:
            self._build_plain()

    @staticmethod
    def _set_enabled(b, on: bool) -> None:
        """圓角按鈕（組員端）與 ttk 按鈕（伺服器、單機）的啟用／停用共用入口。"""
        b.set_enabled(on) if hasattr(b, "set_enabled") else b.state(["!disabled"] if on else ["disabled"])

    @staticmethod
    def _set_text(b, text: str) -> None:
        b.set_text(text) if hasattr(b, "set_text") else b.configure(text=text)

    def _build_plain(self) -> None:
        """伺服器與單機的版面：一般 ttk 元件，不做美化（只有組員端要好看）。"""
        pad = {"padx": 10, "pady": 4}
        top = ttk.LabelFrame(self, text="目前狀態")
        top.pack(fill="x", **pad)
        self.v = {k: tk.StringVar(value="-") for k in ("adapter", "mac", "ip", "traffic", "state", "probe", "admin", "hub")}
        rows = [("網卡", "adapter"), ("MAC", "mac"), ("IP", "ip"), ("流量", "traffic"), ("狀態", "state"), ("最近探針", "probe")]
        for i, (lab, key) in enumerate(rows):
            ttk.Label(top, text=lab + "：").grid(row=i // 2, column=(i % 2) * 2, sticky="e", padx=(10, 2), pady=2)
            ttk.Label(top, textvariable=self.v[key]).grid(row=i // 2, column=(i % 2) * 2 + 1, sticky="w", pady=2)
        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)
        adminrow = ttk.Frame(top)
        adminrow.grid(row=3, column=0, columnspan=4, sticky="we", padx=10, pady=(2, 6))
        self.admin_label = ttk.Label(adminrow, textvariable=self.v["admin"])
        self.admin_label.pack(side="left")
        self.elevate_btn = ttk.Button(adminrow, text="以系統管理員重新啟動", command=self._elevate)
        self.elevate_btn.pack(side="left", padx=10)
        if self.s.role != "standalone":
            ttk.Label(adminrow, textvariable=self.v["hub"], foreground="#1b5e8f").pack(side="right")

        mid = ttk.LabelFrame(self, text="MAC 池")
        mid.pack(fill="both", expand=True, **pad)
        cols = ("mac", "label", "kind", "usage", "detail")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse", height=9)
        for c, tt, w in zip(cols, ("MAC", "標籤", "狀態", "今日用量", "說明"), (130, 165, 80, 175, 165)):
            self.tree.heading(c, text=tt)
            self.tree.column(c, width=w, anchor="w")
        for k, color in KIND_COLOR.items():
            self.tree.tag_configure(k, foreground=color)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        # 先放右側的按鈕欄與捲軸，再讓清單吃掉剩餘空間；否則清單太寬時會把按鈕欄擠成一個字寬
        btns = ttk.Frame(mid)
        btns.pack(side="right", fill="y", padx=8, pady=8)
        edit = [("新增…", self._add), ("刪除所選", self._remove), ("編輯標籤…", self._edit_label), ("切換固定占用", self._toggle_fixed)]
        self._switch_btns = []
        self._switch_text = {}
        # 切換按鈕在最上面，自動挑選又在切換到所選之上（使用者要求：自動挑選是主要操作）
        for text, cmd in (("自動挑選並切換", self._switch_auto), ("切換到所選", self._switch_selected), *edit,
                          ("重新掃描", lambda: self.refresh_pool(force=True))):
            b = ttk.Button(btns, text=text, command=cmd, width=14)
            b.pack(pady=3)
            if text in ("切換到所選", "自動挑選並切換"):
                self._switch_btns.append(b)
                self._switch_text[b] = text
        self.tree.bind("<Double-1>", lambda _e: self._edit_label())
        sb.pack(side="right", fill="y", pady=8)
        self.tree.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)

        ctl = ttk.Frame(self)
        ctl.pack(fill="x", **pad)
        self.auto_var = tk.BooleanVar(value=self.s.auto_mode)
        ttk.Checkbutton(ctl, text="自動切換", variable=self.auto_var, command=self._on_auto).pack(side="left", padx=6)
        ttk.Button(ctl, text="立即檢查", command=self.svc.probe_now).pack(side="left", padx=6)
        ttk.Button(ctl, text="解除停止輪流", command=self.svc.resume).pack(side="left", padx=6)
        ttk.Button(ctl, text="匯入…", command=self._import).pack(side="right", padx=6)
        ttk.Button(ctl, text="匯出…", command=self._export).pack(side="right", padx=6)
        if self.s.role == "hub":
            ttk.Button(ctl, text="產生組員設定檔…", command=self._client_file).pack(side="right", padx=6)

        logf = ttk.LabelFrame(self, text="事件紀錄")
        logf.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logf, height=9, state="disabled", wrap="word", font=("Consolas", 9))
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(8, 0), pady=8)
        lsb.pack(side="left", fill="y", pady=8)
        self.log.tag_configure("warn", foreground="#b9770e")
        self.log.tag_configure("error", foreground="#b03a2e")
        self.log.tag_configure("switch", foreground="#1b7f3b")

    def _build_styled(self) -> None:
        """組員端版面：粉彩配色、圓角、綠色主要按鈕（給非專業使用者，一鍵達到目的）。"""
        self.configure(bg=BG)
        style_ttk(self)
        self.update_idletasks()                        # 讓換主題的待辦事件現在就跑完，避免視窗很快關閉時才在已銷毀的視窗上執行（噴 Tcl 錯誤）
        client = self.s.is_client                      # 組員端：清單由伺服器管理，不放任何改清單的按鈕
        self.v = {k: tk.StringVar(value="-") for k in ("adapter", "mac", "ip", "traffic", "state", "probe", "admin", "hub")}

        # 1) 狀態卡（粉藍）：最重要的是「現在用哪個 MAC、網路好不好」
        card = Card(self, STATUS_BG, fit=True, pad=14)
        card.pack(fill="x", padx=18, pady=(16, 8))
        inner = card.inner
        top = tk.Frame(inner, bg=STATUS_BG)
        top.pack(fill="x")
        self._label(top, "目前 MAC", bg=STATUS_BG, size=10).pack(side="left")
        self._label(top, var=self.v["mac"], bg=STATUS_BG, size=16, bold=True).pack(side="left", padx=(10, 0))
        if self.s.role != "standalone":
            self._label(top, var=self.v["hub"], bg=STATUS_BG, size=9, fg="#3F5A8C").pack(side="right")
        rows = tk.Frame(inner, bg=STATUS_BG)
        rows.pack(fill="x", pady=(6, 0))
        for i, (lab, key) in enumerate([("IP", "ip"), ("網卡", "adapter"), ("流量", "traffic"), ("狀態", "state"), ("最近探針", "probe")]):
            self._label(rows, lab + "：", bg=STATUS_BG, size=9).grid(row=i, column=0, sticky="ne", pady=1)
            self._label(rows, var=self.v[key], bg=STATUS_BG, size=9, justify="left", anchor="w").grid(row=i, column=1, sticky="w", pady=1)
        adminrow = tk.Frame(inner, bg=STATUS_BG)
        adminrow.pack(fill="x", pady=(6, 0))
        self.admin_label = self._label(adminrow, var=self.v["admin"], bg=STATUS_BG, size=9)
        self.admin_label.pack(side="left")
        self.elevate_btn = RoundButton(adminrow, "以系統管理員重新啟動", self._elevate, bg=SMALL, hover=SMALL_HOVER, outer=STATUS_BG, size=9, width=170, height=26, radius=10)
        self.elevate_btn.pack(side="left", padx=10)

        # 2) 主要操作：綠色大按鈕在最上面（非專業使用者，一鍵達到目的）；「切換到所選」是進階選項
        self.auto_btn = RoundButton(self, "自動挑選並切換", self._switch_auto, bg=GO, hover=GO_HOVER, size=15, height=52, radius=16)
        self.auto_btn.pack(fill="x", padx=18, pady=(4, 6))
        self.select_btn = RoundButton(self, "切換到所選", self._switch_selected, size=11, height=34)
        self.select_btn.pack(fill="x", padx=18, pady=(0, 8))
        self._switch_btns = [self.auto_btn, self.select_btn]
        self._switch_text = {self.auto_btn: "自動挑選並切換", self.select_btn: "切換到所選"}

        # 3) MAC 清單（淺粉）
        mid = tk.Frame(self, bg=BG)
        mid.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        self._label(mid, "MAC 清單（點一列再按「切換到所選」）", size=9).pack(anchor="w", pady=(0, 3))
        listcard = Card(mid, LIST_BG, pad=10)
        listcard.pack(fill="both", expand=True)
        box = listcard.inner
        cols = ("mac", "label", "kind", "usage", "detail")
        self.tree = ttk.Treeview(box, columns=cols, show="headings", selectmode="browse", height=8)
        for c, tt, w in zip(cols, ("MAC", "標籤", "狀態", "今日用量", "說明"), (140, 150, 80, 175, 150)):
            self.tree.heading(c, text=tt, anchor="w")
            self.tree.column(c, width=w, anchor="w")
        for k, color in KIND_COLOR.items():
            self.tree.tag_configure(k, foreground=color)
        sb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        if not client:
            self.tree.bind("<Double-1>", lambda _e: self._edit_label())
            # 伺服器／單機才有的清單管理按鈕（組員端的清單由伺服器管理，不放任何改清單的按鈕）
            edit = tk.Frame(mid, bg=BG)
            edit.pack(fill="x", pady=(6, 0))
            for text, cmd in (("新增…", self._add), ("刪除所選", self._remove), ("編輯標籤…", self._edit_label),
                              ("切換固定占用", self._toggle_fixed), ("重新掃描", lambda: self.refresh_pool(force=True))):
                RoundButton(edit, text, cmd, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=96, height=28, radius=10).pack(side="left", padx=(0, 6))

        # 4) 控制列
        ctl = tk.Frame(self, bg=BG)
        ctl.pack(fill="x", padx=18, pady=4)
        self.auto_var = tk.BooleanVar(value=self.s.auto_mode)
        tk.Checkbutton(ctl, text="被限速時自動切換", variable=self.auto_var, command=self._on_auto, bg=BG, activebackground=BG,
                       fg=TEXT, selectcolor="white", font=(FONT, 10, "bold")).pack(side="left")
        RoundButton(ctl, "立即檢查", self.svc.probe_now, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=80, height=28, radius=10).pack(side="left", padx=(12, 6))
        RoundButton(ctl, "解除停止輪流", self.svc.resume, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=100, height=28, radius=10).pack(side="left")
        if not client:
            RoundButton(ctl, "匯入…", self._import, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=70, height=28, radius=10).pack(side="right", padx=(6, 0))
            RoundButton(ctl, "匯出…", self._export, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=70, height=28, radius=10).pack(side="right", padx=(6, 0))
        if self.s.role == "hub":
            RoundButton(ctl, "產生組員設定檔…", self._client_file, bg=SMALL, hover=SMALL_HOVER, size=9, bold=False, width=130, height=28, radius=10).pack(side="right", padx=(6, 0))

        # 5) 事件紀錄：深色終端機風格（沿用舊版）
        logcard = Card(self, CONSOLE_BG, pad=10, height=170)               # 日誌固定高度，清單才有空間（8 個 MAC 要看得到）
        logcard.pack(fill="x", padx=18, pady=(6, 6))
        logf = logcard.inner
        tk.Label(logf, text=">_ 系統動態日誌", bg=CONSOLE_BG, fg=CONSOLE_FG, font=("Consolas", 10, "bold")).pack(anchor="w", padx=10, pady=(6, 0))
        self.log = tk.Text(logf, height=8, state="disabled", wrap="word", font=("Consolas", 9), bg=CONSOLE_BG, fg=CONSOLE_FG,
                           bd=0, highlightthickness=0, insertbackground=CONSOLE_FG)
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview, style="Log.Vertical.TScrollbar")
        self.log.configure(yscrollcommand=lsb.set)
        lsb.pack(side="right", fill="y", pady=6)
        self.log.pack(side="left", fill="both", expand=True, padx=(10, 0), pady=6)
        self.log.tag_configure("warn", foreground=CONSOLE_WARN)
        self.log.tag_configure("error", foreground=CONSOLE_ERR)
        self.log.tag_configure("switch", foreground=CONSOLE_OK)

        # 6) 頁尾：組員端的「解除安裝」放在介面裡（不用另外執行批次檔）
        if client:
            foot = tk.Frame(self, bg=BG)
            foot.pack(fill="x", padx=18, pady=(0, 10))
            self._label(foot, "關閉視窗後 AutoMAC 仍在右下角背景執行", size=9, fg=SUBTLE).pack(side="left")
            RoundButton(foot, "解除安裝…", self._uninstall, bg=QUIET, hover=QUIET_HOVER, size=9, bold=False, width=90, height=26, radius=10).pack(side="right")

    # ------------------------------------------------------------------ 週期更新（主執行緒）
    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "event":
                    self._append(payload)
                elif kind == "pool":
                    self._pool_cache = payload
                    self._fill_tree()
                    self._refreshing = False
        except queue.Empty:
            pass
        self._update_status()
        self._ticks += 1
        if self._ticks % 20 == 0:                   # 每 10 秒自動更新清單畫面（伺服器端改動、別人切換都會反映）
            self.refresh_pool()
        self._after_ids.append(self.after(500, self._poll))

    def _update_status(self) -> None:
        st = self.svc.status()
        self.v["adapter"].set(st.adapter or "-")
        self.v["mac"].set(fmt(st.mac) if st.mac else "-")
        self.v["ip"].set(st.ip or "-")
        self.v["traffic"].set(f"下載 {st.rx:.1f} Mbps／上傳 {st.tx:.1f} Mbps" + ("（安靜）" if st.quiet else ""))
        self.v["state"].set(st.state + (f"｜{st.halted}" if st.halted else ""))
        if st.last_probe and st.last_verdict:
            p = st.last_probe
            self.v["probe"].set(f"{p.mbps:.1f} Mbps → {st.last_verdict.kind}（偏慢次數 {st.strikes}）"
                                f"，{time.strftime('%H:%M:%S', time.localtime(st.last_probe_ts))}")
        if self.auto_var is not None and self.auto_var.get() != st.auto_mode and time.time() - getattr(self, "_auto_clicked", 0) > 3:
            self.auto_var.set(st.auto_mode)            # 以服務的實際狀態為準（別處改了、或服務重啟後）
        for b in self._switch_btns:                # 切換中停用並說明，避免重複點擊
            self._set_enabled(b, not st.switching)
            self._set_text(b, "切換中…請稍候（約 5 秒）" if st.switching else self._switch_text[b])
        self.v["hub"].set(st.hub or "-")
        self.v["admin"].set("系統管理員：是" if st.admin else "系統管理員：否（只能監看，無法換 MAC）")
        self._set_enabled(self.elevate_btn, not st.admin)
        # 目前 MAC 可能因切換而改變，狀態表也要跟著更新
        if self._pool_cache and st.mac and not any(s.mac == st.mac and s.kind == "current" for s in self._pool_cache) \
                and any(s.mac == st.mac for s in self._pool_cache):
            self.refresh_pool(force=True)

    def _append(self, ev: Event) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", ev.line() + "\n", ev.kind if ev.kind in ("warn", "error", "switch") else ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def _usage_text(self, s: MacStatus) -> str:
        """今日用量：↓下載 ↑上傳（佔每日上限的百分比，兩者之和估算）。沒有資料顯示 -。"""
        if not (s.used_rx or s.used_tx):
            return "-"
        pct = (s.used_rx + s.used_tx) / (self.s.daily_quota_gb * 1e9) * 100
        return f"↓{fmt_bytes(s.used_rx)} ↑{fmt_bytes(s.used_tx)}（{pct:.0f}%）"

    def _fill_tree(self) -> None:
        sel = self._selected_mac()
        self.tree.delete(*self.tree.get_children())
        for s in self._pool_cache:
            detail = s.detail
            if s.until:
                detail += f"（約 {time.strftime('%m/%d %H:%M', time.localtime(s.until))} 恢復）"
            self.tree.insert("", "end", iid=s.mac, values=(fmt(s.mac), s.label, KIND_TEXT[s.kind], self._usage_text(s), detail),
                             tags=(s.kind,))
        if sel and self.tree.exists(sel):
            self.tree.selection_set(sel)

    # ------------------------------------------------------------------ 操作
    def refresh_pool(self, force: bool = False) -> None:
        """掃描要數秒，放到背景執行緒；結果經 queue 回主執行緒。"""
        if self._refreshing or self.svc.switcher is None:
            return
        self._refreshing = True

        def work():
            try:
                self._q.put(("pool", self.svc.pool_statuses(max_age_s=8.0, force=force)))
            except Exception as e:
                self._refreshing = False
                self.bus.emit("error", f"更新池內狀態失敗：{e}")
        th = threading.Thread(target=work, daemon=True)
        self._threads = [x for x in getattr(self, "_threads", []) if x.is_alive()] + [th]
        th.start()

    def _selected_mac(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _save(self) -> None:
        config.save(self.s, self.path)

    def _add(self) -> None:
        dlg = AddMacDialog(self)
        self.wait_window(dlg)
        if dlg.result:
            mac, label, fixed = dlg.result
            if self.s.add_mac(mac, label, fixed):
                self._save()
                self.bus.emit("info", f"已新增 {fmt(mac)} {label}")
                self.refresh_pool(force=True)
            else:
                messagebox.showinfo("已存在", f"{fmt(mac)} 已在池內", parent=self)

    def _remove(self) -> None:
        mac = self._selected_mac()
        if not mac:
            return
        if mac == self.svc.status().mac:
            messagebox.showwarning("不能刪除", "這是本機目前使用中的 MAC，請先切換到別的 MAC。", parent=self)
            return
        if messagebox.askyesno("刪除", f"從池中刪除 {fmt(mac)}？\n（不會影響校方登記）", parent=self):
            self.s.remove_mac(mac)
            self._save()
            self.bus.emit("info", f"已刪除 {fmt(mac)}")
            self.refresh_pool(force=True)

    def _edit_label(self) -> None:
        """標籤跟著設備走：選到有電腦在用的 MAC，改的是「那台電腦」的標籤（它換 MAC 標籤跟著換）；
        固定占用的 MAC（Pi 之類）改的是 MAC 自己的標籤；沒人用的一般 MAC 沒有標籤可改。"""
        mac = self._selected_mac()
        e = self.s.find(mac) if mac else None
        st = next((s for s in self._pool_cache if s.mac == mac), None)
        if not e or st is None:
            messagebox.showinfo("編輯標籤", "請先在清單中點選一個 MAC。", parent=self)
            return
        hub = getattr(self.svc, "hub", None)
        if st.holder and hub is not None:
            new = simpledialog.askstring("編輯標籤", f"電腦「{st.holder}」的標籤（留空 = 改顯示電腦名稱）。\n"
                                         "標籤會跟著這台電腦，它換到哪個 MAC 就顯示在哪個 MAC：",
                                         initialvalue=hub.devices.get(st.holder), parent=self)
            if new is not None:
                hub.set_device_label(st.holder, new)
                self.bus.emit("info", f"電腦 {st.holder} 的標籤改為「{new.strip() or st.holder}」")
        elif e.fixed or hub is None:
            new = simpledialog.askstring("編輯標籤", f"{fmt(e.mac)} 的標籤（留空 = 清除）：", initialvalue=e.label, parent=self)
            if new is not None:
                self.s.set_label(e.mac, new)
                self._save()
                self.bus.emit("info", f"{fmt(e.mac)} 標籤改為「{new.strip()}」")
        else:
            messagebox.showinfo("編輯標籤", "這個 MAC 目前沒有電腦在用。標籤跟著電腦走，請在有電腦用它的時候再編輯，"
                                "或把它標成「固定占用」（給 Pi 這類不跑本工具的裝置）後再編輯。", parent=self)
            return
        self.refresh_pool(force=True)

    def _client_file(self) -> None:
        if not self.s.hub_token:
            messagebox.showwarning("沒有 token", "尚未設定 hub_token。請先在命令列執行 automac hub init。", parent=self)
            return
        f = filedialog.asksaveasfilename(parent=self, initialfile="automac-client.json", defaultextension=".json",
                                         filetypes=[("JSON", "*.json")])
        if f:
            Path(f).write_text(json.dumps(config.client_file_dict(self.s), ensure_ascii=False, indent=2), encoding="utf-8")
            self.bus.emit("info", f"已產生組員設定檔：{f}（含 token，只給實驗室成員）")

    def _toggle_fixed(self) -> None:
        mac = self._selected_mac()
        e = self.s.find(mac) if mac else None
        if e:
            e.fixed = not e.fixed
            self._save()
            self.refresh_pool(force=True)

    def _need_admin(self) -> bool:
        if self.svc.status().admin:
            return False
        messagebox.showwarning("需要系統管理員", "換 MAC 需要系統管理員權限。\n請按上方「以系統管理員重新啟動」。", parent=self)
        return True

    def _switch_selected(self) -> None:
        """切換到清單選到的那一列。按下就做（不再跳確認：使用者要的是快），切換中按鈕會停用並顯示進度。"""
        mac = self._selected_mac()
        if not mac:
            messagebox.showinfo("切換到所選", "請先在清單中點選一個 MAC，或直接按綠色的「自動挑選並切換」。", parent=self)
            return
        if self._need_admin():
            return
        self.svc.switch_now(mac)

    def _switch_auto(self) -> None:
        """由伺服器（或本機政策）挑一個沒人用的 MAC 並切換。一鍵完成，不跳確認。"""
        if self._need_admin():
            return
        self.svc.switch_now(None)

    def _uninstall(self) -> None:
        """解除安裝：停止背景服務與系統匣、移除開機自動啟動與防火牆規則、刪除本機設定。MAC 清單與用量在伺服器上，不受影響。"""
        from .. import svcinstall
        if not messagebox.askyesno("解除安裝 AutoMAC",
                                   "要解除安裝 AutoMAC 嗎？\n\n會停止背景偵測、移除開機自動啟動與防火牆規則，並刪除這台電腦上的設定。\n"
                                   "MAC 清單與用量都記在伺服器上，不會遺失。", icon="warning", parent=self):
            return
        try:
            svcinstall.uninstall(self.path.parent, purge=True)
        except Exception as e:
            messagebox.showerror("解除安裝失敗", f"{e}\n\n請確認是以系統管理員身分執行。", parent=self)
            return
        messagebox.showinfo("解除安裝 AutoMAC", "已移除。解壓縮的資料夾可以直接刪除。", parent=self)
        self._close()

    def _on_auto(self) -> None:
        self.svc.set_auto(self.auto_var.get())               # 存檔由服務負責（視窗是檢視器時，視窗手上的設定可能是舊的）
        self._auto_clicked = time.time()

    def _import(self) -> None:
        f = filedialog.askopenfilename(parent=self, filetypes=[("JSON", "*.json"), ("全部", "*.*")])
        if not f:
            return
        try:
            raw = json.loads(Path(f).read_text(encoding="utf-8"))
            imp = Settings.from_legacy(raw) if "mac_list" in raw else Settings.from_dict(raw)
            added = sum(self.s.add_mac(e.mac, e.label, e.fixed) for e in imp.pool)
            self._save()
            self.bus.emit("info", f"匯入 {added} 個新 MAC（重複 {len(imp.pool) - added} 個已略過）")
            self.refresh_pool(force=True)
        except (OSError, ValueError, KeyError) as e:
            messagebox.showerror("匯入失敗", str(e), parent=self)

    def _export(self) -> None:
        f = filedialog.asksaveasfilename(parent=self, defaultextension=".json", filetypes=[("JSON", "*.json")])
        if f:
            Path(f).write_text(json.dumps({"pool": [vars(e) for e in self.s.pool]}, ensure_ascii=False, indent=2),
                               encoding="utf-8")
            self.bus.emit("info", f"已匯出 {len(self.s.pool)} 個 MAC 到 {f}")

    def _elevate(self) -> None:
        if relaunch_as_admin(f'--config "{self.path}"' if str(self.path) != str(config.default_config_path()) else ""):
            self._close()

    def destroy(self) -> None:
        """關窗前取消排程中的 after 回呼，否則視窗銷毀後它們還會執行並噴錯。"""
        for i in getattr(self, "_after_ids", []):
            try:
                self.after_cancel(i)
            except Exception:
                pass
        for th in getattr(self, "_threads", []):       # 等背景工作結束：Tk 變數若在別的執行緒被回收會讓 Tcl 當掉（測試時偶發）
            th.join(timeout=3)
        super().destroy()
        self.v.clear()                                  # 讓 StringVar 在主執行緒被釋放
        self.auto_var = None

    def _close(self) -> None:
        try:
            self.svc.stop()
        finally:
            self.destroy()


def build_app(config_path: str | None = None, dry_run: bool = False) -> App:
    path = Path(config_path) if config_path else config.default_config_path()
    settings = config.load(path)
    bus = EventBus()
    svc = Service(settings, StateStore(), bus=bus, dry_run=dry_run, config_path=path)
    try:
        svc.start()
    except NicError as e:
        # 沒有可用網卡時仍開視窗，讓使用者管理清單並看到原因
        bus.emit("error", f"服務無法啟動：{e}")
    return App(settings, path, svc, bus)


__all__ = ["App", "build_app"]
