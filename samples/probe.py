"""基準採樣：閒置雜訊、單串流、多串流下載，同時量延遲與 TCP 重傳。
唯讀，不換 MAC。下載量有上限（單串流 80 MB、四串流 150 MB）。
用途：分辨校方限速是 shaping（延遲上升）還是 policing（丟包），以及限速是對單一連線還是整個 MAC。
"""
import json, re, socket, subprocess, sys, threading, time, datetime, statistics as st
import psutil, requests

IFACE = "乙太網路"
GW = "192.0.2.62"
URL = "https://speed.cloudflare.com/__down?bytes=25000000"
OUT = r"samples"
NOWIN = 0x08000000

phase = {"name": "init"}
nic, rtt, stop = [], [], threading.Event()


def nic_sampler(step=0.5):
    c = psutil.net_io_counters(pernic=True)[IFACE]
    prev = (time.perf_counter(), c.bytes_recv, c.bytes_sent)
    while not stop.is_set():
        time.sleep(step)
        c = psutil.net_io_counters(pernic=True)[IFACE]
        t = time.perf_counter()
        dt = t - prev[0]
        nic.append((t, phase["name"], (c.bytes_recv - prev[1]) * 8 / dt / 1e6,
                    (c.bytes_sent - prev[2]) * 8 / dt / 1e6))
        prev = (t, c.bytes_recv, c.bytes_sent)


def tcp_rtt():
    while not stop.is_set():
        s = time.perf_counter()
        try:
            socket.create_connection(("1.1.1.1", 443), timeout=1).close()
            rtt.append((s, phase["name"], "tcp_1.1.1.1", (time.perf_counter() - s) * 1000))
        except OSError:
            rtt.append((s, phase["name"], "tcp_1.1.1.1", None))
        time.sleep(0.25)


def gw_ping():
    while not stop.is_set():
        s = time.perf_counter()
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", GW], capture_output=True,
                           text=True, errors="replace", creationflags=NOWIN)
        m = re.search(r"[=<](\d+)\s*ms", r.stdout)
        rtt.append((s, phase["name"], "icmp_gateway", float(m.group(1)) if m else None))
        time.sleep(0.4)


def tcp_counters():
    ps = ("Get-CimInstance Win32_PerfRawData_Tcpip_TCPv4 | "
          "Select-Object SegmentsRetransmittedPersec,SegmentsSentPersec | ConvertTo-Json -Compress")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True,
                       text=True, creationflags=NOWIN)
    d = json.loads(r.stdout)
    return int(d["SegmentsRetransmittedPersec"]), int(d["SegmentsSentPersec"])


def worker(cap, deadline, counter, lock):
    try:
        while time.perf_counter() < deadline and counter[0] < cap:
            with requests.get(URL, stream=True, timeout=10) as r:
                if r.status_code != 200:  # Cloudflare 對 100 MB 以上單次請求會回 403
                    counter[1] = f"HTTP {r.status_code}"
                    return
                for chunk in r.iter_content(65536):
                    with lock:
                        counter[0] += len(chunk)
                        full = counter[0] >= cap
                    if full or time.perf_counter() > deadline:
                        return
    except Exception as e:  # 記錄失敗原因，不中斷整個採樣
        counter[1] = str(e)


def load_phase(name, streams, cap, max_s):
    phase["name"] = name
    r0 = tcp_counters()
    counter, lock = [0, None], threading.Lock()
    t0 = time.perf_counter()
    deadline = t0 + max_s
    ths = [threading.Thread(target=worker, args=(cap, deadline, counter, lock)) for _ in range(streams)]
    [t.start() for t in ths]
    [t.join() for t in ths]
    dur = time.perf_counter() - t0
    r1 = tcp_counters()
    return {"streams": streams, "bytes": counter[0], "seconds": dur, "error": counter[1],
            "app_mbps": counter[0] * 8 / dur / 1e6, "retrans": r1[0] - r0[0], "sent": r1[1] - r0[1]}


def pct(v, p):
    v = sorted(v)
    return v[min(len(v) - 1, int(len(v) * p))] if v else None


def main():
    for f in (nic_sampler, tcp_rtt, gw_ping):
        threading.Thread(target=f, daemon=True).start()
    info: dict = {"time": datetime.datetime.now().isoformat(timespec="seconds")}
    phase["name"] = "idle"; r0 = tcp_counters(); time.sleep(20); r1 = tcp_counters()
    info["idle_retrans"] = r1[0] - r0[0]
    info["load1"] = load_phase("load1", 1, 80_000_000, 8)
    phase["name"] = "cool1"; time.sleep(6)
    info["load4"] = load_phase("load4", 4, 150_000_000, 8)
    phase["name"] = "cool2"; time.sleep(4)
    stop.set(); time.sleep(0.6)

    print("=== 摘要 ===")
    for ph in ("idle", "load1", "load4"):
        rx = [x[2] for x in nic if x[1] == ph]
        tx = [x[3] for x in nic if x[1] == ph]
        if ph != "idle" and len(rx) > 2:
            rx = rx[1:]  # 略過起跑那一格
        if rx:
            print(f"[{ph}] 下載 Mbps 中位 {st.median(rx):.1f} / P90 {pct(rx,.9):.1f} / 最大 {max(rx):.1f} "
                  f"| 上傳中位 {st.median(tx):.2f} | 樣本 {len(rx)}")
        for kind in ("icmp_gateway", "tcp_1.1.1.1"):
            v = [x[3] for x in rtt if x[1] == ph and x[2] == kind and x[3] is not None]
            lost = sum(1 for x in rtt if x[1] == ph and x[2] == kind and x[3] is None)
            if v:
                print(f"    {kind}: 中位 {st.median(v):.1f} ms / P90 {pct(v,.9):.1f} / 最大 {max(v):.1f} | 失敗 {lost}/{len(v)+lost}")
    for k in ("load1", "load4"):
        d = info[k]
        ratio = d["retrans"] / d["sent"] * 100 if d["sent"] else 0
        print(f"[{k}] 實下載 {d['bytes']/1e6:.1f} MB / {d['seconds']:.1f} s = {d['app_mbps']:.1f} Mbps | "
              f"重傳 {d['retrans']} / 送出 {d['sent']} 段 ({ratio:.2f}%) | 錯誤 {d['error']}")
    print(f"[idle] 重傳 {info['idle_retrans']} 段")

    info["nic"] = [(round(t, 3), p, round(a, 3), round(b, 3)) for t, p, a, b in nic]
    info["rtt"] = [(round(t, 3), p, k, None if v is None else round(v, 2)) for t, p, k, v in rtt]
    fn = f"{OUT}\\probe_{datetime.datetime.now():%Y%m%d-%H%M%S}.json"
    json.dump(info, open(fn, "w", encoding="utf-8"), ensure_ascii=False)
    print("原始資料:", fn)


if __name__ == "__main__":
    main()
