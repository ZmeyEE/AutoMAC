"""每秒讀網卡流量，換算成 Mbps，保留最近幾分鐘供偵測器取視窗。

為什麼讀網卡計數器：這是「目前主機實際收發多少」最便宜的來源，不產生任何流量，
也不需要封包擷取驅動（Npcap）。偵測器靠它判斷「使用者是不是正在大量下載」。

設計重點
- 計數器來源可注入（counters 參數），測試時餵假資料；時間也可注入。
- 網卡重啟（換 MAC）時計數器可能歸零：偵測到變小就把差值當 0 並重新取基準，
  累計值（cumulative）永遠單調遞增，探針有效性判斷靠它做差。
- 取不到計數器（網卡暫時消失）就跳過這一秒，不補 0，偵測器會因樣本不足而不下結論。
"""
from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

Counters = Optional[tuple[int, int]]   # (已收位元組, 已送位元組) 或 None


@dataclass(frozen=True)
class Sample:
    t: float     # 單調時鐘秒
    rx: float    # 下載 Mbps
    tx: float    # 上傳 Mbps


def psutil_counters(iface: str) -> Counters:
    """從 psutil 讀指定網卡的累計位元組；網卡不存在回傳 None。"""
    import psutil  # 延遲載入：只在真的要讀時才需要，讓純邏輯模組不依賴它
    c = psutil.net_io_counters(pernic=True).get(iface)
    return (c.bytes_recv, c.bytes_sent) if c else None


class TrafficSampler:
    def __init__(self, iface: str, interval: float = 1.0, keep_s: float = 180.0,
                 counters: Callable[[str], Counters] = psutil_counters,
                 clock: Callable[[], float] = time.monotonic):
        self.iface, self.interval, self._counters, self._clock = iface, interval, counters, clock
        self._samples: deque[Sample] = deque(maxlen=max(10, int(keep_s / interval)))
        self._lock = threading.Lock()
        self._prev: Counters = None
        self._prev_t: float | None = None
        self._cum_rx = 0
        self._cum_tx = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # ---- 核心：一次取樣（公開，測試可手動驅動）
    def step(self, now: float | None = None) -> Sample | None:
        now = self._clock() if now is None else now
        cur = self._counters(self.iface)
        if cur is None:
            self._prev, self._prev_t = None, None     # 網卡消失：重新取基準
            return None
        with self._lock:
            if self._prev is None or self._prev_t is None:
                self._prev, self._prev_t = cur, now
                return None
            d_rx = cur[0] - self._prev[0]
            d_tx = cur[1] - self._prev[1]
            if d_rx < 0 or d_tx < 0:                  # 計數器歸零（網卡重啟）
                d_rx = d_tx = 0
            dt = max(now - self._prev_t, 1e-6)
            self._cum_rx += d_rx
            self._cum_tx += d_tx
            s = Sample(now, d_rx * 8 / dt / 1e6, d_tx * 8 / dt / 1e6)
            self._samples.append(s)
            self._prev, self._prev_t = cur, now
            return s

    # ---- 查詢
    def window(self, seconds: float, now: float | None = None) -> list[Sample]:
        now = self._clock() if now is None else now
        with self._lock:
            return [s for s in self._samples if now - s.t <= seconds]

    def latest(self) -> Sample | None:
        with self._lock:
            return self._samples[-1] if self._samples else None

    def cumulative(self) -> tuple[float, int, int]:
        """(現在時間, 累計收位元組, 累計送位元組)。兩次呼叫相減即為區間流量（探針有效性用）。

        現場讀一次計數器，但不更新取樣基準，也不產生 Sample：否則探針執行緒與主迴圈幾乎同時取樣時，
        會產生時間間隔極短的樣本，一個封包就能算出數十 Mbps 的假尖峰，污染偵測視窗。"""
        now = self._clock()
        cur = self._counters(self.iface)
        with self._lock:
            if cur is None or self._prev is None:
                return now, self._cum_rx, self._cum_tx
            return (now, self._cum_rx + max(0, cur[0] - self._prev[0]),
                    self._cum_tx + max(0, cur[1] - self._prev[1]))

    # ---- 背景執行緒
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="automac-sampler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.step()
            except Exception:  # 取樣失敗不可讓整個程式掛掉，下一秒再試
                pass
