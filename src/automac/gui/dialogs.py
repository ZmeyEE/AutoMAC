"""小對話框：新增 MAC。"""
from __future__ import annotations

import tkinter as tk
from tkinter import ttk

from ..macaddr import check_assignable, fmt


class AddMacDialog(tk.Toplevel):
    """輸入 MAC、標籤、是否固定占用。result 為 (mac12, label, fixed) 或 None（取消）。
    輸入檢查在按下「新增」時做，錯誤直接顯示在對話框內，不關閉。"""

    def __init__(self, parent: tk.Misc):
        super().__init__(parent)
        self.title("新增 MAC")
        self.resizable(False, False)
        self.transient(parent)
        self.result: tuple[str, str, bool] | None = None
        f = ttk.Frame(self, padding=14)
        f.grid()
        ttk.Label(f, text="MAC 位址").grid(row=0, column=0, sticky="w")
        self.mac = ttk.Entry(f, width=24)
        self.mac.grid(row=0, column=1, padx=8, pady=4)
        ttk.Label(f, text="任何格式皆可，例如 00-00-5E-00-53-33", foreground="#666").grid(row=1, column=1, sticky="w")
        ttk.Label(f, text="標籤（選填）").grid(row=2, column=0, sticky="w")
        self.label = ttk.Entry(f, width=24)
        self.label.grid(row=2, column=1, padx=8, pady=4)
        self.fixed = tk.BooleanVar()
        ttk.Checkbutton(f, text="被其他裝置長期占用（不參與輪流）", variable=self.fixed).grid(row=3, column=0, columnspan=2, sticky="w", pady=4)
        self.err = ttk.Label(f, text="", foreground="#c0392b")
        self.err.grid(row=4, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=5, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(b, text="新增", command=self._ok).pack(side="left", padx=4)
        ttk.Button(b, text="取消", command=self.destroy).pack(side="left")
        self.bind("<Return>", lambda _e: self._ok())
        self.bind("<Escape>", lambda _e: self.destroy())
        self.mac.focus_set()
        self.grab_set()

    def _ok(self) -> None:
        try:
            m = check_assignable(self.mac.get())
        except ValueError as e:
            self.err.config(text=str(e))
            return
        self.result = (m, self.label.get().strip(), self.fixed.get())
        self.mac.delete(0, "end")
        self.mac.insert(0, fmt(m))
        self.destroy()
