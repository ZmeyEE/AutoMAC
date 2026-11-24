"""重播：把一段已錄下的流量序列餵給偵測器，看它會在什麼時候發探針、做出什麼判斷。

用途
1. 驗證：用真實樣本（健康閒置、限速、使用者自己下載）檢查偵測器沒有誤判。
2. 調參：之後蒐集到新的限速樣本（recorder.py 的 CSV），可用同一套流程重播、比較門檻。

重播把探針視為瞬間完成；探針結果由 probe_model(t, rx) 提供（模擬「此刻線路的真實狀態」）。
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .detector import Detector, DetectorConfig, ProbeOutcome, Verdict


def resample(points: Iterable[tuple[float, float]], step: float = 1.0) -> list[tuple[float, float]]:
    """把不等距序列重取樣成每 step 秒一點（區間內取平均），時間從 0 開始。"""
    pts = sorted(points)
    if not pts:
        return []
    t0 = pts[0][0]
    buckets: dict[int, list[float]] = {}
    for t, v in pts:
        buckets.setdefault(int((t - t0) // step), []).append(v)
    return [((k + 1) * step, sum(vs) / len(vs)) for k, vs in sorted(buckets.items())]


def load_probe_json(path: str | Path) -> list[tuple[float, str, float]]:
    """讀 samples/probe*.json 的 nic 欄位，回傳 (秒, 階段名, 下載 Mbps)。
    probe.py 的格式是 (t, 階段, rx, tx)；probe3.py 的是 (t, rx, tx)，後者階段名一律 'download'。"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    out = []
    for row in d["nic"]:
        out.append((row[0], row[1], row[2]) if len(row) == 4 else (row[0], "download", row[1]))
    return out


def load_recorder_csv(path: str | Path) -> list[tuple[float, float]]:
    """讀 recorder.py 的 CSV（每秒一列），回傳 (秒, 下載 Mbps)。"""
    rows = list(csv.DictReader(Path(path).open(encoding="utf-8")))
    return [(float(i), float(r["rx_mbps"])) for i, r in enumerate(rows)]


@dataclass
class ReplayResult:
    probes: list[tuple[float, str, ProbeOutcome, Verdict]] = field(default_factory=list)

    def kinds(self) -> list[str]:
        return [v.kind for _, _, _, v in self.probes]

    def first(self, kind: str) -> float | None:
        return next((t for t, _, _, v in self.probes if v.kind == kind), None)


def replay(cfg: DetectorConfig, series: list[tuple[float, float]],
           probe_model: Callable[[float, float], float],
           ok: Callable[[float], bool] = lambda t: True,
           on_throttled: Callable[[Detector, float], None] | None = None) -> ReplayResult:
    """series：(秒, 下載 Mbps)，間隔應等於 cfg.sample_interval_s。
    probe_model(t, rx)：此刻發探針會測到幾 Mbps。ok(t)：此刻探針能不能完成（模擬斷網）。
    探針是否「有效」取決於當下網卡流量是否低於門檻，與真實探針的判斷方式一致。"""
    det = Detector(cfg)
    res = ReplayResult()
    for t, rx in series:
        det.on_sample(t, rx)
        reason = det.want_probe(t)
        if not reason:
            continue
        out = ProbeOutcome(ok=ok(t), valid=rx < cfg.gate_mbps, mbps=probe_model(t, rx))
        verdict = det.on_probe(t, out)
        res.probes.append((t, reason, out, verdict))
        if verdict.kind == "throttled" and on_throttled:
            on_throttled(det, t)
    return res
