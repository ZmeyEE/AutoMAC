"""區網鄰居掃描：找出同一網段內現在有哪些 MAC 在線，用來避免切換到別人正在用的 MAC。

做法（不需要 Npcap 等封包擷取驅動）
    1. 對網段內每個位址送一個 UDP 小封包到 9 號埠（discard）。目的不是通訊，而是逼作業系統先做 ARP。
    2. 等一下，讀鄰居表：Windows 用 `arp -a -N <本機 IP>`；Linux 讀 /proc/net/arp（不需要 root，清空用 ip neigh flush 才需要）。
       Windows 原本用 PowerShell 的 Get-NetNeighbor 與 Get-NetIPAddress，每次要啟動一個 PowerShell，各約 1.3 秒（2026-11-24 實測），
       一次掃描光這兩個就 2.6 秒；改成 psutil（0.00 秒）與 arp -a（0.02 秒）。在真實網路上連續三輪比對，兩種做法讀到的鄰居完全相同。
       任何一步失敗（沒有 psutil、arp 指令失敗）都退回 PowerShell 做法，結果不變、只是慢。
為什麼不用 ping：2026-11-23 實測，ICMP 漏掉約一半的主機（有些裝置擋 ping 但會回 ARP）。

限制（要誠實寫在這裡）
    - 只掃得到「自己的網段」。實測這個二層網路還有 10.13.200.x 的裝置（印表機），ARP 掃描看不到它們，
      所以防重疊另外靠 presence（mDNS 宣告）補強。
    - 鄰居表的 Stale 項目可能殘留一陣子；非管理員無法先清空，會偏保守（誤判成使用中只會少選一個 MAC，不會撞）。
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
import threading
import time
from dataclasses import dataclass
from typing import Callable, Iterable

from .macaddr import is_broadcast_or_multicast, normalize
from .winshell import is_admin, ps_json, ps_quote, run_ps

# Get-NetNeighbor 的 State：0 Unreachable、1 Incomplete、2 Probe、3 Delay、4 Stale、5 Reachable、6 Permanent
_LIVE_STATES = {2, 3, 4, 5, "Probe", "Delay", "Stale", "Reachable"}
MAX_SWEEP_HOSTS = 1024


@dataclass(frozen=True)
class Neighbor:
    ip: str
    mac: str       # 12 字元大寫
    state: str


def parse_neighbors(rows: Iterable[dict], network: ipaddress.IPv4Network | None = None,
                    own_mac: str = "") -> list[Neighbor]:
    """把 Get-NetNeighbor 的輸出轉成 Neighbor 清單（純函式，方便測試）。
    略過：沒有 MAC、無法解析、廣播與群播、非活躍狀態、不在本網段、本機自己。"""
    out: list[Neighbor] = []
    own = normalize(own_mac) if own_mac else ""
    for r in rows:
        ip, raw = r.get("IPAddress") or "", r.get("LinkLayerAddress") or ""
        if not ip or not raw or r.get("State") not in _LIVE_STATES:
            continue
        try:
            mac = normalize(raw)
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if addr.version != 4 or is_broadcast_or_multicast(mac) or mac == own:
            continue
        if network is not None and addr not in network:
            continue
        out.append(Neighbor(ip, mac, str(r.get("State"))))
    return sorted(out, key=lambda n: ipaddress.ip_address(n.ip))


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def parse_proc_arp(text: str, iface: str, network: ipaddress.IPv4Network | None = None,
                   own_mac: str = "") -> list[Neighbor]:
    """解析 Linux 的 /proc/net/arp（純函式，方便測試），只留指定網卡、已完成解析的項目。
    欄位：IP address / HW type / Flags / HW address / Mask / Device；Flags 0x2 = 已完成、0x0 = 還在解析（沒人回），略過後者。
    轉成 Get-NetNeighbor 的欄位格式後交給 parse_neighbors，兩個平台共用同一套過濾規則（廣播、群播、自己、網段外）。"""
    rows = []
    for line in text.splitlines()[1:]:
        c = line.split()
        if len(c) < 6 or c[5] != iface:
            continue
        try:
            complete = bool(int(c[2], 16) & 0x2)
        except ValueError:
            continue
        if complete:
            rows.append({"IPAddress": c[0], "LinkLayerAddress": c[3], "State": "Reachable"})
    return parse_neighbors(rows, network, own_mac)


_ARP_ROW = re.compile(r"^\s*(\d+\.\d+\.\d+\.\d+)\s+([0-9a-fA-F]{2}(?:-[0-9a-fA-F]{2}){5})\s", re.M)


def parse_arp_a(text: str, network: ipaddress.IPv4Network | None = None, own_mac: str = "") -> list[Neighbor]:
    """解析 Windows `arp -a` 的輸出（純函式，方便測試）。只認「IP、MAC」兩個數字欄位，不看「動態／dynamic」那一欄，
    因為那欄會隨系統語言變（繁中是「動態」「靜態」）。靜態項目是廣播與群播（224.x、255.x），交給 parse_neighbors 用 MAC 濾掉。"""
    rows = [{"IPAddress": m.group(1), "LinkLayerAddress": m.group(2), "State": "Reachable"} for m in _ARP_ROW.finditer(text)]
    return parse_neighbors(rows, network, own_mac)


def _arp_a_neighbors(own_ip: str, net: ipaddress.IPv4Network, own_mac: str) -> list[Neighbor] | None:
    """用 `arp -a -N <本機 IP>` 讀這張網卡的鄰居表；指令失敗回傳 None（呼叫者退回 PowerShell）。"""
    import subprocess
    try:
        cp = subprocess.run(["arp", "-a", "-N", own_ip], capture_output=True, timeout=10,
                            creationflags=0x08000000 if sys.platform == "win32" else 0)
    except (OSError, subprocess.SubprocessError):
        return None
    if cp.returncode != 0:
        return None
    return parse_arp_a(cp.stdout.decode("mbcs" if sys.platform == "win32" else "utf-8", "replace"), net, own_mac)


def _psutil_local_network(iface: str) -> tuple[str, ipaddress.IPv4Network] | None:
    """用 psutil 讀網卡的 IPv4 位址與子網路遮罩（不啟動外部程序）。讀不到回傳 None。"""
    try:
        import psutil
        for a in psutil.net_if_addrs().get(iface, []):
            if a.family == socket.AF_INET and a.address and a.netmask and not a.address.startswith(("169.254.", "127.")):
                return a.address, ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False)
    except Exception:
        return None
    return None


def _linux_local_network(iface: str) -> tuple[str, ipaddress.IPv4Network] | None:
    """Linux：用 psutil 讀網卡的 IPv4 位址與子網路遮罩。"""
    import psutil
    for a in psutil.net_if_addrs().get(iface, []):
        if a.family == socket.AF_INET and a.address and a.netmask and not a.address.startswith(("169.254.", "127.")):
            return a.address, ipaddress.IPv4Network(f"{a.address}/{a.netmask}", strict=False)
    return None


def _linux_flush(iface: str) -> None:
    """清空該網卡的鄰居表（需要 root），讓之後讀到的是「現在真的在線」。失敗（沒有 ip 指令、權限不足）就算了，結果會偏保守。"""
    import shutil
    import subprocess
    ip = shutil.which("ip")
    if ip:
        try:
            subprocess.run([ip, "-4", "neigh", "flush", "dev", iface], capture_output=True, timeout=10)
        except Exception:
            pass


def _linux_neighbors(iface: str, net: ipaddress.IPv4Network, own_mac: str) -> list[Neighbor]:
    try:
        with open("/proc/net/arp", encoding="ascii") as f:
            return parse_proc_arp(f.read(), iface, net, own_mac)
    except OSError:
        return []


def local_network(iface: str) -> tuple[str, ipaddress.IPv4Network] | None:
    """(本機 IP, 所在網段)；網卡沒有可用 IPv4 回傳 None。網段過大（>1024 台）縮成本機所在的 /24。"""
    info = _linux_local_network(iface) if _is_linux() else _psutil_local_network(iface)
    if info or _is_linux():
        if info and info[1].num_addresses > MAX_SWEEP_HOSTS:
            return info[0], ipaddress.IPv4Network(f"{info[0]}/24", strict=False)
        return info
    rows = ps_json(f"Get-NetIPAddress -InterfaceAlias {ps_quote(iface)} -AddressFamily IPv4 "
                   "-ErrorAction SilentlyContinue | Select-Object IPAddress,PrefixLength")
    for r in rows:
        ip = r.get("IPAddress", "")
        if ip and not ip.startswith(("169.254.", "127.")):
            net = ipaddress.IPv4Network(f"{ip}/{r['PrefixLength']}", strict=False)
            if net.num_addresses > MAX_SWEEP_HOSTS:
                net = ipaddress.IPv4Network(f"{ip}/24", strict=False)
            return ip, net
    return None


def sweep(network: ipaddress.IPv4Network, own_ip: str = "", port: int = 9,
          sock_factory: Callable[[], socket.socket] = lambda: socket.socket(socket.AF_INET, socket.SOCK_DGRAM)) -> int:
    """對網段內每個主機位址送 1 個位元組的 UDP，逼出 ARP。回傳送出的數量。"""
    s = sock_factory()
    n = 0
    try:
        for host in network.hosts():
            if str(host) == own_ip:
                continue
            try:
                s.sendto(b"\x00", (str(host), port))
                n += 1
            except OSError:
                pass            # 個別位址送不出去（例如網卡剛重啟）不影響其他
    finally:
        s.close()
    return n


def scan(iface: str, own_mac: str = "", wait_s: float = 1.5, rounds: int = 2,
         flush: bool = True, stop: threading.Event | None = None) -> list[Neighbor]:
    """掃描並回傳鄰居。管理員且 flush=True 時先清空該網卡的鄰居表，結果才是「現在真的在線」。
    stop：服務停止時會被設起來。掃描中的固定等待（等 ARP 回應）改成可以被它叫醒，視窗關閉時不必等掃描跑完
    （2026-11-24 實測：掃描進行中關視窗，主執行緒要等 3.29 秒，視窗這段時間是凍結的）。被中斷時回傳空清單。"""
    info = local_network(iface)
    if info is None:
        return []
    own_ip, net = info
    linux = _is_linux()
    if flush and is_admin() and linux:
        _linux_flush(iface)
    elif flush and is_admin():
        run_ps(f"Remove-NetNeighbor -InterfaceAlias {ps_quote(iface)} -AddressFamily IPv4 -Confirm:$false "
               "-ErrorAction SilentlyContinue")
    for i in range(rounds):                 # 兩輪：第二輪補抓第一輪 ARP 還沒回來的裝置
        if stop is not None and stop.is_set():
            return []
        sweep(net, own_ip)
        nap = wait_s if i == rounds - 1 else 0.7
        if stop is not None:
            if stop.wait(nap):
                return []
        else:
            time.sleep(nap)
    if linux:
        return _linux_neighbors(iface, net, own_mac)
    fast = _arp_a_neighbors(own_ip, net, own_mac)
    if fast is not None:
        return fast
    rows = ps_json(f"Get-NetNeighbor -InterfaceAlias {ps_quote(iface)} -AddressFamily IPv4 "
                   "-ErrorAction SilentlyContinue | Select-Object IPAddress,LinkLayerAddress,State")
    return parse_neighbors(rows, net, own_mac)
