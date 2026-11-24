"""限速狀態第二輪：外部延遲（看有沒有排隊）與休息後的突發額度。唯讀、不換 MAC，總下載量上限約 30 MB。"""
import socket, statistics as st, subprocess, threading, time, re
import psutil, requests

IFACE, HOST = "乙太網路", "speed.cloudflare.com"
URL = "https://speed.cloudflare.com/__down?bytes=25000000"
stop = threading.Event()
rtts = []  # (秒, 毫秒或None)


def tcp_rtt(t0):
    while not stop.is_set():
        s = time.perf_counter()
        try:
            socket.create_connection((HOST, 443), timeout=3).close()
            rtts.append((s - t0, (time.perf_counter() - s) * 1000))
        except OSError:
            rtts.append((s - t0, None))
        time.sleep(0.3)


def nic():
    c = psutil.net_io_counters(pernic=True)[IFACE]
    return c.bytes_recv


def download(sec, cap):
    got, t0 = 0, time.perf_counter()
    series = []  # (秒, 累計位元組)
    with requests.get(URL, stream=True, timeout=10) as r:
        for ch in r.iter_content(16384):
            got += len(ch)
            now = time.perf_counter() - t0
            series.append((now, got))
            if now > sec or got >= cap:
                break
    return series


def run(label, sec, cap):
    rtts.clear(); stop.clear()
    t0 = time.perf_counter()
    th = threading.Thread(target=tcp_rtt, args=(t0,), daemon=True); th.start()
    time.sleep(2)  # 先量 2 秒閒置外部延遲
    n0 = len(rtts)
    s = download(sec, cap)
    stop.set(); th.join()
    idle = [v for _, v in rtts[:n0] if v is not None]
    load = [v for _, v in rtts[n0:] if v is not None]
    lost = sum(1 for _, v in rtts[n0:] if v is None)
    by_sec = {}
    for t, b in s:
        by_sec[int(t)] = b
    prev, per = 0, []
    for k in sorted(by_sec):
        per.append(round((by_sec[k] - prev) * 8 / 1e6, 2)); prev = by_sec[k]
    print(f"[{label}] 共 {s[-1][1]/1e6:.2f} MB / {s[-1][0]:.1f} s；每秒 Mbps = {per}")
    print(f"    外部 TCP 連線時間 閒置中位 {st.median(idle):.0f} ms；下載中位 {st.median(load):.0f} ms / 最大 {max(load):.0f} ms / 失敗 {lost}"
          if idle and load else f"    外部延遲資料不足 idle={len(idle)} load={len(load)}")


run("A 立刻再測", 8, 12_000_000)
print("休息 60 秒看突發額度是否回補…")
time.sleep(60)
run("B 休息 60 秒後", 8, 12_000_000)
