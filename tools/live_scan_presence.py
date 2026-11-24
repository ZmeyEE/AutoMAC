"""真實網路驗證 P3。
  scan                      掃描本網段並列出鄰居（對照已知池內 MAC 請自行比對）
  local-conflict            同一程序內兩個 Presence 同時搶同一個 MAC，恰好一個應勝出
  pub <MAC> <秒>            宣告 holding 並維持指定秒數（給另一台機器觀察）
  watch <秒>                列出看得到的同伴
用法：python tools/live_scan_presence.py <模式> ...   （網卡自動偵測）"""
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from automac import nic, scanner  # noqa: E402
from automac.presence import Presence  # noqa: E402

SVC = "_automactest2._tcp.local."
n = nic.WindowsNic()
a = n.detect("")
ip = n.current_ip(a.name)
mode = sys.argv[1]

if mode == "scan":
    t = time.perf_counter()
    res = scanner.scan(a.name, own_mac=a.mac)
    print(f"掃描耗時 {time.perf_counter()-t:.1f} 秒，網卡 {a.name}，本機 {ip}")
    for x in res:
        print(f"  {x.ip:16} {x.mac}  {x.state}")
elif mode == "local-conflict":
    P = [Presence(SVC, hostname=f"t{i}") for i in range(2)]
    for p in P:
        p.start(ip)
    P[0].announce("00005E005301", "holding")           # 先讓 P0 有宣告，P1 才看得到
    P[1].announce("00005E005302", "holding")
    time.sleep(4)
    print("P0 看到的同伴:", [(x.mac, x.state) for x in P[0].peers()])
    print("P1 看到的同伴:", [(x.mac, x.state) for x in P[1].peers()])
    res = {}
    def go(i): res[i] = P[i].claim("00005E0053AA", wait_s=1.5)
    ths = [threading.Thread(target=go, args=(i,)) for i in range(2)]
    [t.start() for t in ths]; [t.join() for t in ths]
    print("同時搶 00005E0053AA 的結果:", res, "→ 勝出者數量", sum(res.values()), "（應為 1）")
    for p in P:
        p.stop()
elif mode == "pub":
    p = Presence(SVC, role="test"); p.start(ip); p.announce(sys.argv[2], "holding")
    print("已宣告", sys.argv[2], "於", ip); time.sleep(float(sys.argv[3])); p.stop()
elif mode == "watch":
    p = Presence(SVC); p.start(ip); time.sleep(float(sys.argv[2]))
    for x in p.peers():
        print("  看到:", x)
    print("共", len(p.peers()), "個同伴"); p.stop()
