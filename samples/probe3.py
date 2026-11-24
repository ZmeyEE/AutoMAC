"""使用者自己在下載時的被動採樣：網卡速度、外部連線時間，中間穿插兩個小探針。不換 MAC，探針共約 1.3 MB。"""
import json, socket, statistics as st, threading, time, datetime
import psutil, requests

IFACE, HOST, SEC = "乙太網路", "speed.cloudflare.com", 70
nic, rtt, probes = [], [], []
stop = threading.Event()
T0 = time.perf_counter()


def nic_loop():
    c = psutil.net_io_counters(pernic=True)[IFACE]
    p = (time.perf_counter(), c.bytes_recv, c.bytes_sent)
    while not stop.is_set():
        time.sleep(0.5)
        c = psutil.net_io_counters(pernic=True)[IFACE]
        t = time.perf_counter()
        nic.append((round(t - T0, 1), round((c.bytes_recv - p[1]) * 8 / (t - p[0]) / 1e6, 2),
                    round((c.bytes_sent - p[2]) * 8 / (t - p[0]) / 1e6, 2)))
        p = (t, c.bytes_recv, c.bytes_sent)


def rtt_loop():
    while not stop.is_set():
        s = time.perf_counter()
        try:
            socket.create_connection((HOST, 443), timeout=5).close()
            rtt.append((round(s - T0, 1), round((time.perf_counter() - s) * 1000)))
        except OSError:
            rtt.append((round(s - T0, 1), None))
        time.sleep(0.5)


def probe(size):
    s = time.perf_counter()
    try:
        n = len(requests.get(f"https://{HOST}/__down?bytes={size}", timeout=20).content)
        probes.append((round(s - T0, 1), size, round(n * 8 / (time.perf_counter() - s) / 1e6, 2)))
    except Exception as e:
        probes.append((round(s - T0, 1), size, str(e)[:40]))


for f in (nic_loop, rtt_loop):
    threading.Thread(target=f, daemon=True).start()
time.sleep(25); probe(262144)
time.sleep(15); probe(1048576)
time.sleep(max(0, SEC - (time.perf_counter() - T0)))
stop.set(); time.sleep(0.7)

rx = [x[1] for x in nic]
print(f"網卡下載 Mbps：中位 {st.median(rx):.2f} / 最小 {min(rx):.2f} / 最大 {max(rx):.2f} / 標準差 {st.pstdev(rx):.2f}")
print("每 5 秒平均 Mbps：", [round(st.mean(rx[i:i + 10]), 1) for i in range(0, len(rx), 10)])
v = [x[1] for x in rtt if x[1] is not None]
print(f"外部連線時間 ms：中位 {st.median(v):.0f} / P90 {sorted(v)[int(len(v)*.9)]} / 最大 {max(v)} / 失敗 {sum(1 for x in rtt if x[1] is None)}")
print("探針 (秒, 位元組, Mbps)：", probes)
fn = rf"samples\probe3_{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
json.dump({"nic": nic, "rtt": rtt, "probes": probes}, open(fn, "w"))
print("原始資料:", fn)
