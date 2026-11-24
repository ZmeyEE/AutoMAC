"""被動錄製：每秒記錄網卡速度；每 10 秒量一次閘道延遲；每 N 分鐘做一個 1 MB 基準探針。
目的：被限速時，把「限速長什麼樣」錄下來當樣本。不換 MAC，不改任何設定。
用法：python recorder.py [--hours 24] [--probe-min 10]   （--probe-min 0 = 完全不產生流量）
"""
import argparse, csv, datetime, re, subprocess, threading, time
import psutil, requests

IFACE, GW = "乙太網路", "192.0.2.62"
OUT = r"samples"
latest = {"gw": "", "probe": ""}  # 背景執行緒寫入，主迴圈讀取後清空


def mac_now():
    for a in psutil.net_if_addrs().get(IFACE, []):
        if a.family == psutil.AF_LINK:
            return a.address.replace("-", "").upper()
    return ""


def gw_loop():
    while True:
        r = subprocess.run(["ping", "-n", "1", "-w", "1000", GW], capture_output=True, text=True,
                           errors="replace", creationflags=0x08000000)
        m = re.search(r"[=<](\d+)\s*ms", r.stdout)
        latest["gw"] = m.group(1) if m else "timeout"
        time.sleep(10)


def probe_loop(minutes):
    while True:
        time.sleep(minutes * 60)
        try:
            t = time.perf_counter()
            n = len(requests.get("https://speed.cloudflare.com/__down?bytes=1048576", timeout=15).content)
            latest["probe"] = f"{n * 8 / (time.perf_counter() - t) / 1e6:.2f}"
        except Exception:
            latest["probe"] = "fail"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=24)
    ap.add_argument("--probe-min", type=float, default=10)
    a = ap.parse_args()
    threading.Thread(target=gw_loop, daemon=True).start()
    if a.probe_min > 0:
        threading.Thread(target=probe_loop, args=(a.probe_min,), daemon=True).start()
    fn = f"{OUT}\\record_{datetime.datetime.now():%Y%m%d-%H%M%S}.csv"
    end = time.time() + a.hours * 3600 if a.hours > 0 else float("inf")
    print("錄製中 →", fn, "（Ctrl+C 結束）")
    with open(fn, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["time", "rx_mbps", "tx_mbps", "mac", "gw_rtt_ms", "probe_mbps"])
        c = psutil.net_io_counters(pernic=True)[IFACE]
        prev, pt = (c.bytes_recv, c.bytes_sent), time.perf_counter()
        mac, n = mac_now(), 0
        while time.time() < end:
            time.sleep(1.0)
            c = psutil.net_io_counters(pernic=True)[IFACE]
            t = time.perf_counter()
            rx, tx = ((c.bytes_recv - prev[0]) * 8 / (t - pt) / 1e6, (c.bytes_sent - prev[1]) * 8 / (t - pt) / 1e6)
            prev, pt, n = (c.bytes_recv, c.bytes_sent), t, n + 1
            if n % 30 == 0:
                mac = mac_now()  # MAC 可能被切換，定時重讀
            w.writerow([datetime.datetime.now().isoformat(timespec="seconds"), f"{rx:.3f}", f"{tx:.3f}",
                        mac, latest["gw"], latest["probe"]])
            f.flush()
            latest["gw"] = latest["probe"] = ""


if __name__ == "__main__":
    main()
