"""端到端實機測試：用函式庫（Switcher + WindowsNic + Presence）真的換到指定 MAC，停留幾秒再換回原 MAC。
必須以系統管理員執行；換 MAC 會讓 IP 改變、遠端連線可能中斷，所以建議用排程工作（SYSTEM）執行，才不會連帶中止。
用法：python switch_roundtrip.py <目標MAC> <停留秒數>      結果寫在 roundtrip.log（可用環境變數 AUTOMAC_RT_LOG 指定位置）"""
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src") if (HERE.parent / "src").exists() else str(HERE / "src"))
os.environ.setdefault("AUTOMAC_CONFIG", str(HERE.parent / "config.json"))

from automac import config, nic as nicmod  # noqa: E402
from automac.config import StateStore  # noqa: E402
from automac.events import EventBus  # noqa: E402
from automac.macaddr import fmt  # noqa: E402
from automac.presence import Presence  # noqa: E402
from automac.scanner import scan  # noqa: E402
from automac.switcher import Switcher  # noqa: E402

LOG = Path(os.environ.get("AUTOMAC_RT_LOG", HERE.parent / "roundtrip.log"))


def log(msg: str) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")


target, hold = sys.argv[1], float(sys.argv[2])
s = config.load()
n = nicmod.WindowsNic()
a = n.detect(s.iface)
bus = EventBus()
bus.subscribe(lambda e: log(e.line()))
p = Presence(s.mdns_service, role="server")
p.start(n.current_ip(a.name))
p.announce(a.mac, "holding")
time.sleep(3)
sw = Switcher(s, StateStore(), n, a.name, scan_fn=lambda: scan(a.name, own_mac=n.current_mac(a.name)),
              presence=p, bus=bus)
orig = a.mac
log(f"開始：{fmt(orig)} → {fmt(target)}，停留 {hold:.0f} 秒")
try:
    r = sw.switch_to(target, "e2e-test")
    log(f"切換結果 ok={r.ok} ip={r.ip} 耗時 {r.seconds:.1f}s")
    time.sleep(hold)
finally:
    if n.current_mac(a.name) != orig:
        r2 = sw.switch_to(orig, "e2e-revert", force=True)
        log(f"換回結果 ok={r2.ok} ip={r2.ip} 耗時 {r2.seconds:.1f}s")
    p.stop()
    log("結束")
