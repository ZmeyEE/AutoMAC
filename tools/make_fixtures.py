"""把 samples/ 的原始採樣轉成測試用的精簡樣本（每秒一點，只留下載速度與階段名）。
這些樣本只含速度數字，沒有 MAC、IP 或任何可識別資訊，可放進公開倉庫。
用法：python tools/make_fixtures.py"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
from automac.replay import load_probe_json  # noqa: E402

SRC = ROOT / "samples"
OUT = ROOT / "tests" / "fixtures"
OUT.mkdir(parents=True, exist_ok=True)

JOBS = {   # 輸出檔名: (原始檔, 說明)
    "healthy_probe.json": ("probe_20261123-163212.json", "未被限速：閒置 20 秒、單串流與四串流下載（我方產生）"),
    "throttled_probe.json": ("probe_20261123-205916.json", "被限速：同一套採樣，下載被壓在約 1 Mbps"),
    "healthy_user_download.json": ("probe3_20261123-210918.json", "未被限速：使用者自己下載約 98 Mbps 平線"),
}
for out_name, (src_name, note) in JOBS.items():
    rows = load_probe_json(SRC / src_name)
    t0 = rows[0][0]
    buckets: dict[int, list] = {}
    for t, ph, rx in rows:
        b = buckets.setdefault(int(t - t0), [ph, []])
        b[1].append(rx)
    series = [[k + 1, b[0], round(sum(b[1]) / len(b[1]), 2)] for k, b in sorted(buckets.items())]
    (OUT / out_name).write_text(json.dumps({"note": note, "step_s": 1, "series": series}, ensure_ascii=False),
                                encoding="utf-8")
    print(out_name, len(series), "點")
