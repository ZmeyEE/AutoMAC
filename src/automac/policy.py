"""選哪個 MAC、何時可以切換（純邏輯，時間由外部傳入）。

選擇規則：池內的 MAC，排除 ①目前這個 ②固定占用（例如 Pi 長期用的）③別人正在用（掃描或宣告看到）
④已知被限速且還沒到恢復時間 之後，從「目前這個的下一個」開始輪流（round-robin），讓各 MAC 的用量平均。

為什麼輪流而不是挑「最久沒用」：我們不記流量帳本（使用者明確不要），輪流是不需要帳本的近似；
被限速的 MAC 另外記到恢復時間，不會一直撞到。

切換頻率限制（Governor）：兩次切換至少隔 min_switch_gap_s；一小時內超過 max_switches_per_hour 次
就停止自動輪流。理由：連續換了幾個都慢，代表很可能整池都慢或校園網整體壅塞，再換只會一直斷線，
停下來通知使用者比盲目輪流好。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .config import Settings, StateStore
from .macaddr import normalize


@dataclass(frozen=True)
class MacStatus:
    mac: str
    label: str
    kind: str                 # current | fixed | in_use | throttled | free
    detail: str = ""
    until: float | None = None  # 限速預計恢復時間（kind == throttled 時）
    used_rx: int = 0            # 今天已知的下載位元組（用量帳本，只含跑本工具的電腦）
    used_tx: int = 0            # 今天已知的上傳位元組
    holder: str = ""            # 目前持有這個 MAC 的電腦（伺服器登記的電腦名稱）；沒人持有為空。標籤跟著它走


def classify(settings: Settings, state: StateStore, current_mac: str, in_use: dict[str, str],
             now: float) -> list[MacStatus]:
    """池內每個 MAC 現在的狀態，順序與設定檔相同（視窗直接用這個畫表格）。
    優先序：current > fixed > in_use > throttled > free。"""
    cur = normalize(current_mac) if current_mac else ""
    out: list[MacStatus] = []
    for e in settings.pool:
        until = state.throttled_until_of(e.mac, now)
        if e.mac == cur:
            out.append(MacStatus(e.mac, e.label, "current"))
        elif e.fixed:
            out.append(MacStatus(e.mac, e.label, "fixed", "不參與輪流"))
        elif e.mac in in_use:
            out.append(MacStatus(e.mac, e.label, "in_use", in_use[e.mac]))
        elif until:
            out.append(MacStatus(e.mac, e.label, "throttled", "已知被限速", until))
        else:
            out.append(MacStatus(e.mac, e.label, "free"))
    return out


def rank_candidates(statuses: list[MacStatus], current_mac: str, usage: dict[str, int] | None = None) -> list[str]:
    """可選的 MAC，從目前這個的下一個開始繞一圈。目前 MAC 不在池內就從頭開始。
    有用量帳本（usage：MAC → 今天已用位元組）時，在可選的當中優先今天用得最少的；同樣多（例如都還沒用）
    維持輪流順序，所以沒有帳本資料時行為和以前完全一樣。"""
    macs = [s.mac for s in statuses]
    cur = normalize(current_mac) if current_mac else ""
    start = (macs.index(cur) + 1) if cur in macs else 0
    order = macs[start:] + macs[:start]
    free = {s.mac for s in statuses if s.kind == "free"}
    throttled = {s.mac for s in statuses if s.kind == "throttled"}
    out = [m for m in order if m in free]
    last = [m for m in order if m in throttled]
    if usage:
        out.sort(key=lambda m: usage.get(m, 0))              # 穩定排序：同樣用量保持輪流順序
        last.sort(key=lambda m: usage.get(m, 0))
    # 被標限速的 MAC 排最後：沒被限速的都輪過了、或都在使用中時才輪到它。這樣每個 MAC 都有機會輪流使用
    # （長期不用可能被學校關掉），又盡量避開限速的（2026-11-24 使用者要求）。固定占用與他人使用中的仍然不選。
    return out + last


class GovernorConfig(Protocol):
    min_switch_gap_s: float
    max_switches_per_hour: int


class Governor:
    """自動切換的「煞車」：回傳 (能不能切, 原因代碼, 說明)。手動切換不受限。"""

    def __init__(self, cfg: GovernorConfig, state: StateStore):
        self.cfg, self.state = cfg, state

    def allow(self, now: float) -> tuple[bool, str, str]:
        last = self.state.last_switch()
        if last is not None and now - last < self.cfg.min_switch_gap_s:
            wait = self.cfg.min_switch_gap_s - (now - last)
            return False, "gap", f"距離上次切換不足 {self.cfg.min_switch_gap_s:.0f} 秒，還要等 {wait:.0f} 秒"
        n = self.state.switches_within(now, 3600)
        if n >= self.cfg.max_switches_per_hour:
            return False, "rate", (f"一小時內已切換 {n} 次（上限 {self.cfg.max_switches_per_hour}），"
                                   "可能整池都慢或校園網壅塞，已停止自動輪流")
        return True, "ok", ""
