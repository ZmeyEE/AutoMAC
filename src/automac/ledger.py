"""用量帳本：記錄「每個 MAC 每天用了多少流量」，存在伺服器（單機模式存本機），組員端只負責回報。

為什麼要做（2026-11-24 使用者改變主意：看得到每個 MAC 的具體用量更好；先前說不要時是為了不增加複雜度）
    - 校方對每個 MAC 有每日流量上限（使用者描述的規則，未經校方文件證實），看得到用量才知道哪個 MAC 快到了。
    - 分配時優先選今天用得最少的 MAC（policy.rank_candidates），比單純輪流更不容易撞到上限。

怎麼量
    每台跑本工具的電腦用網卡累計位元組計數器（和偵測用的是同一個來源）算出區間用量，歸給「當時用的那個 MAC」，
    定期回報伺服器；伺服器按「額度日」累加。額度日以 quota_reset_hour 切（預設 0 點），與限速恢復時間的假設一致。
    shortcut: 只看得到「跑本工具的電腦」的流量，Pi 之類不跑本工具的裝置用掉的流量不在帳上，所以顯示的是下限，不是校方實際計算的值。
    另一個限制：用的是整張網卡的流量，電腦如果同時走別的網路（例如 Wi-Fi 另有一張網卡）不會被算進來；
    以及不知道校方算下載、上傳或兩者之和，所以兩個方向分開記，百分比以兩者之和估算。

存檔：JSON，按日期分組，保留最近 keep_days 天；原子寫入（先寫暫存檔再取代），壞掉的檔案當成空帳本（可再生的資料）。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

from .config import _atomic_write
from .macaddr import normalize

MAX_SINGLE_REPORT = 1 << 40        # 單次回報上限 1 TB：擋掉寫錯或惡意的巨大數字，不讓帳本被一筆資料毀掉


class Ledger:
    def __init__(self, path: str | Path | None, reset_hour: int = 0, clock: Callable[[], float] = time.time,
                 keep_days: int = 60):
        self.path = Path(path) if path else None
        self.reset_hour, self.clock, self.keep_days = reset_hour, clock, keep_days
        self._lock = threading.Lock()
        self._d: dict[str, dict[str, list[int]]] = {}        # 額度日 → MAC → [下載位元組, 上傳位元組]
        self._load()

    # ---- 日期
    def day_key(self, now: float | None = None) -> str:
        """額度日：把時間往前挪 reset_hour 小時再取日期，所以 reset_hour=4 時，凌晨 3 點仍算前一天。"""
        t = self.clock() if now is None else now
        return time.strftime("%Y-%m-%d", time.localtime(t - self.reset_hour * 3600))

    # ---- 寫入
    def add(self, mac: str, rx: int, tx: int, now: float | None = None) -> None:
        """累加一筆區間用量（位元組）。負數、非數字或單筆過大會丟 ValueError（不寫入）。"""
        rx, tx = int(rx), int(tx)
        if rx < 0 or tx < 0 or rx > MAX_SINGLE_REPORT or tx > MAX_SINGLE_REPORT:
            raise ValueError("用量數字不合理（負數或超過 1 TB）")
        if rx == 0 and tx == 0:
            return
        m = normalize(mac)
        with self._lock:
            row = self._d.setdefault(self.day_key(now), {}).setdefault(m, [0, 0])
            row[0] += rx
            row[1] += tx
            self._prune()
            self._save()

    # ---- 讀取
    def today(self, now: float | None = None) -> dict[str, tuple[int, int]]:
        with self._lock:
            return {m: (v[0], v[1]) for m, v in self._d.get(self.day_key(now), {}).items()}

    def history(self, days: int = 7, now: float | None = None) -> dict[str, dict[str, tuple[int, int]]]:
        """最近 days 個額度日（含今天），由新到舊；沒有資料的日子不列出。"""
        t = self.clock() if now is None else now
        keys = [self.day_key(t - i * 86400) for i in range(days)]
        with self._lock:
            return {k: {m: (v[0], v[1]) for m, v in self._d[k].items()} for k in keys if k in self._d}

    # ---- 內部
    def _prune(self) -> None:
        for k in sorted(self._d)[:-self.keep_days]:
            del self._d[k]

    def _load(self) -> None:
        if not self.path:
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._d = {str(k): {normalize(m): [int(v[0]), int(v[1])] for m, v in day.items()} for k, day in raw.items()}
        except (OSError, ValueError, TypeError, IndexError):
            self._d = {}                                    # 沒有或壞掉：從空帳本開始

    def _save(self) -> None:
        if self.path:
            _atomic_write(self.path, json.dumps(self._d, indent=1))


def fmt_bytes(n: float) -> str:
    """位元組轉成人看的單位（十進位，和校方、網卡廠商一致：1 GB = 10^9 位元組）。"""
    for unit, size in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= size:
            return f"{n / size:.2f} {unit}" if unit == "GB" else f"{n / size:.0f} {unit}"
    return f"{int(n)} B"


__all__ = ["Ledger", "fmt_bytes", "MAX_SINGLE_REPORT"]
