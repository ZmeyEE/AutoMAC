"""判斷「這個 MAC 是否被限速」的狀態機。純邏輯：不碰網路、不碰時鐘，時間與結果都由外部傳入，
因此可以用已錄下的真實流量樣本重播驗證（見 replay.py 與 tests/test_p1_detector.py）。

判斷依據（2026-11-23 在實驗室實測）
    健康：下載可貼近約 95 Mbps；使用者自己下載時網卡 98 Mbps 平線
    限速：下載被壓在約 1.0 Mbps 的平線，且 1 條或 4 條連線總和相同（以 MAC 為單位）
    探針：256 KB 單串流新連線，限速時約 0.9 Mbps、健康閒置時 7 到 29 Mbps；
          但使用者正在下載時探針只讀到 3.5 Mbps，離門檻太近

因此規則是（使用者提出的邏輯）：
    網卡流量大  → 一定不是限速（線路明顯通），不探測、不打擾
    網卡流量小  → 才發探針；探針期間若出現別的流量，這次讀值不算（無效）
    連續兩次有效且低於門檻 → 判定限速

為什麼不直接信一次探針：一次偏慢可能是瞬間壅塞；要求連續兩次（間隔數秒）把誤判壓低。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Protocol

from .stats import cv, mean, median


class DetectorConfig(Protocol):
    """偵測器需要的設定欄位（config.Settings 滿足這個介面；測試可傳簡單物件）。"""
    gate_mbps: float
    probe_low_mbps: float
    quiet_window_s: float
    recent_window_s: float
    idle_probe_interval_s: float
    confirm_strikes: int
    strike_gap_s: float
    strike_ttl_s: float
    invalid_retry_s: float
    flat_low_range: tuple[float, float]
    flat_low_cv: float
    sample_interval_s: float


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool        # 探針有完成（連得上、收滿）；False = 連不上或逾時
    valid: bool     # 探測期間沒有別的流量干擾，讀值可信
    mbps: float     # 探針測得速度


@dataclass(frozen=True)
class Verdict:
    kind: str       # healthy | slow | throttled | net_down | invalid
    strikes: int    # 目前累計偏慢次數
    detail: str


class Detector:
    NET_DOWN_BACKOFF_S = 15.0
    FAST_IDLE_S = 10.0          # 今天用量已超過額度的 MAC：最可能被限速，探測間隔縮短到這麼久
    ACTIVE_MIN_MBPS = 0.2       # 長視窗中位數達這個值就算「有在用網路」（低於門檻的前提下，被限速的典型狀態）
    FLAT_REPROBE_S = 8.0        # 平線特徵出現時，兩次探測至少隔這麼久，避免連續探

    def __init__(self, cfg: DetectorConfig):
        self.cfg = cfg
        self._rx: deque[tuple[float, float]] = deque()
        self._last_probe_t: float | None = None     # 最近一次完成探針的時間（不論結果）
        self._last_healthy_t: float | None = None   # 最近一次「健康」探針；平線來源慢但線路健康時避免一直重探
        self._strikes = 0
        self._first_strike_t = 0.0
        self._last_strike_t = 0.0
        self._next_allowed_t = 0.0                  # 無效或失敗後的退避
        self._need_verify = False                   # 剛換過 MAC，需要驗證新 MAC 是否健康
        self.fast_probe = False                     # 由服務設定：目前 MAC 今天用量已超過額度（使用者描述的規則：超過會被限速）

    @property
    def strikes(self) -> int:
        return self._strikes

    # ------------------------------------------------------------------ 輸入
    def on_sample(self, t: float, rx_mbps: float) -> None:
        self._rx.append((t, rx_mbps))
        horizon = max(self.cfg.quiet_window_s, self.cfg.recent_window_s) + 5
        while self._rx and t - self._rx[0][0] > horizon:
            self._rx.popleft()

    def reset_after_switch(self, t: float) -> None:
        """換 MAC 後呼叫：舊樣本屬於舊 MAC，清掉；並要求下一次安靜時先驗證新 MAC。"""
        self._rx.clear()
        self._strikes = 0
        self._last_probe_t = None
        self._last_healthy_t = None
        self._need_verify = True
        self._next_allowed_t = t + 5.0

    # ------------------------------------------------------------------ 查詢
    def _window(self, t: float, seconds: float) -> list[float]:
        return [x for tt, x in self._rx if t - tt <= seconds]

    def _need(self) -> int:
        """視窗內至少要有多少樣本才敢下結論（容許約 30% 遺失，例如網卡重啟那幾秒）。"""
        return max(3, int(self.cfg.quiet_window_s / self.cfg.sample_interval_s * 0.7))

    def is_quiet(self, t: float) -> bool:
        """主機目前沒有大量網路流量：長視窗中位數與最近幾秒平均都低於門檻。
        長視窗用中位數（抗突發）；短視窗用平均（抓住「剛開始下載」）。"""
        w = self._window(t, self.cfg.quiet_window_s)
        if len(w) < self._need():
            return False
        if median(w) >= self.cfg.gate_mbps:
            return False
        r = self._window(t, self.cfg.recent_window_s)
        return bool(r) and mean(r) < self.cfg.gate_mbps

    def is_flat_low(self, t: float) -> bool:
        """限速的指紋：貼在約 1 Mbps 且幾乎不起伏。符合就不必等閒置間隔，立刻探測。"""
        w = self._window(t, self.cfg.quiet_window_s)
        if len(w) < self._need():
            return False
        lo, hi = self.cfg.flat_low_range
        return lo <= median(w) <= hi and cv(w) <= self.cfg.flat_low_cv

    def want_probe(self, t: float) -> str | None:
        """現在該不該發探針？回傳理由字串（給日誌與測試用），不該就回傳 None。"""
        if t < self._next_allowed_t or not self.is_quiet(t):
            return None
        if self._strikes:
            if t - self._first_strike_t > self.cfg.strike_ttl_s:
                self._strikes = 0                         # 第一次偏慢太久沒確認，作廢
            elif t - self._last_strike_t >= self.cfg.strike_gap_s:
                return "confirm"
            else:
                return None
        if self._need_verify:
            return "post_switch"
        last = self._last_probe_t
        # 探測間隔依「使用者有沒有在等」分兩種：完全閒置時慢（沒人在等，探針也吃額度）；有在用但流量低於門檻（被限速的
        # 典型狀態）時快。2026-11-24 使用者回報限速偵測要分鐘級，太慢。今天用量已達額度的 MAC 更快。
        w = self._window(t, self.cfg.quiet_window_s)
        active = bool(w) and median(w) >= self.ACTIVE_MIN_MBPS
        interval = self.cfg.idle_probe_interval_s
        if active:
            interval = min(interval, getattr(self.cfg, "active_probe_interval_s", 30.0))
        if self.fast_probe:
            interval = min(interval, self.FAST_IDLE_S)
        healthy_recently = (self._last_healthy_t is not None and t - self._last_healthy_t < interval)
        if self.is_flat_low(t) and not healthy_recently and (last is None or t - last >= self.FLAT_REPROBE_S):
            return "flat_low"
        if last is None or t - last >= interval:
            return "active_interval" if active else "idle_interval"
        return None

    # ------------------------------------------------------------------ 結果
    def on_probe(self, t: float, out: ProbeOutcome) -> Verdict:
        cfg = self.cfg
        if not out.ok:
            # 連不上不等於被限速（可能整個網路斷線），不記次數，退避一陣子
            self._next_allowed_t = t + self.NET_DOWN_BACKOFF_S
            return Verdict("net_down", self._strikes, "探針失敗，網路可能中斷，不計入限速")
        self._last_probe_t = t
        if not out.valid:
            self._next_allowed_t = t + cfg.invalid_retry_s
            return Verdict("invalid", self._strikes, "探測期間有其他流量，這次讀值不採用")
        if out.mbps < cfg.probe_low_mbps:
            if self._strikes == 0:
                self._first_strike_t = t
            self._strikes += 1
            self._last_strike_t = t
            if self._strikes >= cfg.confirm_strikes:
                n, self._strikes = self._strikes, 0
                return Verdict("throttled", n, f"連續 {n} 次有效探針偏慢（最後 {out.mbps:.2f} Mbps）")
            return Verdict("slow", self._strikes, f"探針偏慢 {out.mbps:.2f} Mbps，待確認")
        self._strikes = 0
        self._need_verify = False
        self._last_healthy_t = t
        return Verdict("healthy", 0, f"探針 {out.mbps:.2f} Mbps，正常")
