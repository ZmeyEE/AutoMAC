"""真實網路驗證 P2：閒置時連發探針看讀值與有效性；再開背景下載，確認探針會被標為「無效」。
下載量上限約 40 MB。用法：python tools/live_probe_check.py [網卡名稱]"""
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from automac import nic  # noqa: E402
from automac.config import Settings  # noqa: E402
from automac.probe import run_probe  # noqa: E402
from automac.traffic import TrafficSampler  # noqa: E402

iface = sys.argv[1] if len(sys.argv) > 1 else nic.WindowsNic().detect("").name
cfg = Settings()
sm = TrafficSampler(iface)
sm.start()
time.sleep(4)
print(f"網卡 {iface}；門檻 gate={cfg.gate_mbps} Mbps")

print("== 閒置時連發 5 次探針（每次間隔 3 秒）")
for i in range(5):
    r = run_probe(cfg, sm)
    print(f"  #{i+1} ok={r.ok} valid={r.valid} {r.mbps:6.2f} Mbps  {r.seconds*1000:5.0f} ms  其他流量 {r.other_mbps:.2f} Mbps {r.error}")
    time.sleep(3)

print("== 背景下載中發探針（應為 valid=False，且其他流量遠高於門檻）")
stop = threading.Event()


def bg():
    got = 0
    while not stop.is_set() and got < 40_000_000:
        # Cloudflare 會擋預設的 Python-urllib 識別字串（回 403），所以要自訂 User-Agent
        req = urllib.request.Request("https://speed.cloudflare.com/__down?bytes=25000000",
                                     headers={"User-Agent": "automac-probe/1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            while not stop.is_set():
                c = r.read(65536)
                if not c:
                    break
                got += len(c)


th = threading.Thread(target=bg, daemon=True)
th.start()
time.sleep(3)
for i in range(3):
    r = run_probe(cfg, sm)
    print(f"  #{i+1} ok={r.ok} valid={r.valid} {r.mbps:6.2f} Mbps  其他流量 {r.other_mbps:6.1f} Mbps {r.error}")
    time.sleep(1)
stop.set()
sm.stop()
