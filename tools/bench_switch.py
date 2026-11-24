"""量測「換 MAC」各階段耗時（需系統管理員；會真的換網卡 MAC 兩次，最後換回原 MAC）。

用途：找出切換慢在哪一段，比較兩種做法：
    A  現行：PowerShell Set-NetAdapter -MacAddress
    B  直接寫登錄 NetworkAddress + 用 pnputil 重啟裝置（省掉 PowerShell 啟動與 CIM 載入）
每個階段記錄：指令結束、MAC 生效、連線（isup）、拿到 IP 的秒數（以輪詢 50 毫秒取樣，psutil 讀取約 1 毫秒）。
用法：python tools/bench_switch.py <網卡名稱> <MAC_A> <MAC_B>   （先從 A 換到 B 用做法 A，再換回用做法 B）
"""
import subprocess
import sys
import time
import winreg

import psutil

CLASS = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"


def mac_ip(name):
    mac = ip = None
    for a in psutil.net_if_addrs().get(name, []):
        if a.family == psutil.AF_LINK:
            mac = a.address.replace("-", "").upper()
        elif a.family == 2 and not a.address.startswith(("169.254.", "127.")):
            ip = a.address
    up = psutil.net_if_stats().get(name).isup if name in psutil.net_if_stats() else False
    return mac, ip, up


def watch(name, want, t0, label, cmd_done):
    seen = {}
    while time.perf_counter() - t0 < 60:
        mac, ip, up = mac_ip(name)
        t = time.perf_counter() - t0
        if mac == want and "mac" not in seen:
            seen["mac"] = t
        if mac == want and up and "up" not in seen:
            seen["up"] = t
        if mac == want and ip and "ip" not in seen:
            seen["ip"] = t
            break
        time.sleep(0.05)
    print(f"{label}: 指令結束 {cmd_done:.2f}s｜MAC 生效 {seen.get('mac', -1):.2f}s｜連線 {seen.get('up', -1):.2f}s｜拿到 IP {seen.get('ip', -1):.2f}s")


def method_a(name, mac):
    fm = "-".join(mac[i:i + 2] for i in range(0, 12, 2))
    t0 = time.perf_counter()
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                    f"Set-NetAdapter -Name '{name}' -MacAddress '{fm}' -Confirm:$false"], capture_output=True, creationflags=0x08000000)
    return t0, time.perf_counter() - t0


def method_b(name, mac):
    """找出網卡的登錄項（NetCfgInstanceId 對應），寫 NetworkAddress，再用 pnputil 重啟該裝置。"""
    guid = None
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          f"(Get-NetAdapter -Name '{name}').InterfaceGuid + '|' + (Get-NetAdapter -Name '{name}').PnPDeviceID"],
                         capture_output=True, text=True).stdout.strip().split("|")
    guid, pnp = out[0], out[1]
    t0 = time.perf_counter()
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, CLASS) as k:
        i = 0
        while True:
            try:
                sub = winreg.EnumKey(k, i)
            except OSError:
                break
            i += 1
            try:
                with winreg.OpenKey(k, sub, 0, winreg.KEY_READ | winreg.KEY_WRITE) as s:
                    if winreg.QueryValueEx(s, "NetCfgInstanceId")[0].lower() == guid.lower():
                        winreg.SetValueEx(s, "NetworkAddress", 0, winreg.REG_SZ, mac)
                        break
            except OSError:
                continue
    subprocess.run(["pnputil", "/restart-device", pnp], capture_output=True, creationflags=0x08000000)
    return t0, time.perf_counter() - t0


if __name__ == "__main__":
    name, a, b = sys.argv[1], sys.argv[2].upper(), sys.argv[3].upper()
    cur, _, _ = mac_ip(name)
    assert cur == a, f"目前 MAC {cur} 不是 {a}"
    t0, cmd = method_a(name, b)
    watch(name, b, t0, "A PowerShell Set-NetAdapter", cmd)
    time.sleep(2)
    t0, cmd = method_b(name, a)
    watch(name, a, t0, "B 登錄＋pnputil", cmd)
