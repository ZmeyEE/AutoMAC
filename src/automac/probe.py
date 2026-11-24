"""小探針：下載一個小檔測速，並判斷「這次讀值可不可信」。

為什麼是 256 KB 單串流新連線（2026-11-23 實測）
    限速時被壓在約 1 Mbps，所以 256 KB 約 2 到 3 秒讀到約 0.9 Mbps；
    健康閒置時同樣的探針實測 7 到 29 Mbps（不同時段不同，快取與 TLS 會影響），兩者差 8 倍以上，離 2 Mbps 門檻都有餘裕；
    128 KB 在健康線路只讀到 3.8 Mbps（握手與慢啟動吃掉太多時間），太接近門檻，所以不用。
    新連線的讀值本來就比線路實際頻寬低（TCP 慢啟動），所以不能拿絕對值和「線路速度」比，
    只能和固定門檻比，而門檻已用同一種探針的限速／健康實測值校準過。

有效性（使用者提出的邏輯）：探測期間網卡若還有別的流量（使用者正在下載），探針會被擠壓而偏低，
這次讀值就不算數。做法：探針前後各讀一次網卡累計位元組，扣掉探針自己的位元組，剩下的就是「別的流量」。
"""
from __future__ import annotations

import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Protocol


class ProbeConfig(Protocol):
    gate_mbps: float
    probe_bytes: int
    probe_timeout_s: float
    probe_urls: list[str]


class CumulativeSource(Protocol):
    def cumulative(self) -> tuple[float, int, int]: ...


@dataclass(frozen=True)
class ProbeResult:
    ok: bool                 # 完整收完（False = 連不上、逾時或收不滿）
    valid: bool              # 探測期間沒有別的流量干擾
    mbps: float              # 酬載位元數 / 耗時（含 DNS 與握手）
    bytes: int
    seconds: float
    other_mbps: float        # 探測期間扣掉探針自己後的其他流量
    url: str = ""
    error: str = ""


# 網卡計數器包含 TCP/IP 標頭與 TLS 握手，酬載之外要多扣一點才算「探針自己的流量」。
# 估法：乙太網路標頭約 3%（40/1460 加 14 位元組幀頭），握手憑證鏈約 8 KB，另留一些裕度。
OVERHEAD_RATIO = 1.05
OVERHEAD_FIXED = 12_000


def _fetch(url: str, want: int, timeout: float, opener, clock) -> tuple[int, float]:
    """下載並回傳 (收到位元組, 耗時秒)。耗時從發出請求前算起，含 DNS、TCP、TLS。"""
    t0 = clock()
    req = urllib.request.Request(url, headers={"User-Agent": "automac-probe/1"})
    got = 0
    with opener(req, timeout=timeout) as r:
        while got < want:
            chunk = r.read(min(65536, want - got))
            if not chunk:
                break
            got += len(chunk)
            if clock() - t0 > timeout:
                raise TimeoutError(f"探針超過 {timeout} 秒")
    return got, clock() - t0


def run_probe(cfg: ProbeConfig, meter: CumulativeSource | None = None,
              opener: Callable = urllib.request.urlopen,
              clock: Callable[[], float] = time.perf_counter) -> ProbeResult:
    """依序嘗試 probe_urls，第一個成功就回傳；全失敗回傳 ok=False 與最後一個錯誤。

    meter 不給就無法判斷有效性，一律視為無效（寧可不採用，也不要在沒把握時判人限速）。
    """
    want = cfg.probe_bytes
    last_err, last_url = "沒有可用的探針網址", ""
    for base in cfg.probe_urls:
        url = f"{base}{'&' if '?' in base else '?'}bytes={want}"
        last_url = url
        before = meter.cumulative() if meter else None
        try:
            got, secs = _fetch(url, want, cfg.probe_timeout_s, opener, clock)
        except Exception as e:  # 網路層各種錯誤（DNS、連線、TLS、逾時）統一視為「這次沒測成」
            last_err = f"{type(e).__name__}: {e}"
            continue
        after = meter.cumulative() if meter else None
        if got < want:
            last_err = f"只收到 {got}/{want} 位元組"
            continue
        mbps = got * 8 / max(secs, 1e-6) / 1e6
        other, valid = 0.0, False
        if before and after:
            dt = max(after[0] - before[0], 1e-6)
            own = got * OVERHEAD_RATIO + OVERHEAD_FIXED
            other = max(0.0, (after[1] - before[1]) - own) * 8 / dt / 1e6
            valid = other < cfg.gate_mbps
        return ProbeResult(True, valid, mbps, got, secs, other, url)
    return ProbeResult(False, False, 0.0, 0, 0.0, 0.0, last_url, last_err)
