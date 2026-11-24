"""網卡偵測與換 MAC（系統邊界）。上層只依賴 NicBackend 介面，測試時換成假物件。

與舊工具的差異（舊工具 mac_switcher.py）
- 舊：網卡名稱必須含 'ethernet' 或 '乙太' 才認得；新：用 -Physical 與 MediaType 802.3 判斷，不靠名稱。
- 舊：換完固定 sleep 5 秒再 ipconfig /renew 兩次；新：輪詢到「MAC 已變、網卡已連線、拿到 DHCP 位址」才算完成，
  逾時則換回原 MAC，避免留下斷線狀態。
- 舊：失敗了也不知道；新：丟 NicError 並附原因（最常見是沒有系統管理員權限、驅動不支援改 MAC）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from .macaddr import check_assignable, fmt, normalize
from .winshell import PowerShellError, ps_json, ps_quote, run_ps, is_admin


class NicError(RuntimeError):
    pass


@dataclass(frozen=True)
class Adapter:
    name: str
    mac: str          # 目前生效的 MAC（12 字元大寫）；改過 MAC 後這裡是改後的值
    up: bool
    speed: str
    ifindex: int


class NicBackend(Protocol):
    def detect(self, hint: str = "") -> Adapter: ...
    def current_mac(self, name: str) -> str: ...
    def current_ip(self, name: str) -> str | None: ...
    def gateway(self, name: str) -> str | None: ...
    def set_mac(self, name: str, mac: str) -> None: ...


class WindowsNic:
    """用 PowerShell 的 NetAdapter 模組實作。"""

    def detect(self, hint: str = "") -> Adapter:
        rows = ps_json("Get-NetAdapter -Physical | Where-Object { $_.MediaType -eq '802.3' } | "
                       "Select-Object Name,MacAddress,Status,LinkSpeed,ifIndex,InterfaceGuid,PnPDeviceID")
        for r in rows:                                    # 順便記下快速換 MAC 要用的識別，第一次切換就不必再查一次
            if r.get("Name") and r.get("InterfaceGuid") and r.get("PnPDeviceID"):
                self._ids[r["Name"]] = (r["InterfaceGuid"], r["PnPDeviceID"])
        adapters = [Adapter(r["Name"], normalize(r["MacAddress"]), str(r["Status"]) == "Up",
                            str(r.get("LinkSpeed", "")), int(r["ifIndex"])) for r in rows if r.get("MacAddress")]
        if hint:
            for a in adapters:
                if a.name == hint:
                    return a
            raise NicError(f"找不到指定的網卡 {hint!r}；現有有線網卡：{[a.name for a in adapters]}")
        up = [a for a in adapters if a.up]
        if not up:
            raise NicError("沒有已連線的有線網卡（請確認網路線與交換機）")
        return up[0]

    @staticmethod
    def _addrs(name: str):
        """psutil 的網卡位址表。讀一次約 1 毫秒；PowerShell 每次要開一個處理程序（約 0.3 到 0.5 秒 CPU）。
        背景服務每 30 秒就要讀 MAC 與 IP，改用 psutil 是降低常駐耗用的主要手段。找不到網卡（例如重啟中）回傳 None。"""
        try:
            import psutil
            return psutil.net_if_addrs().get(name), psutil.AF_LINK
        except Exception:
            return None, None

    def current_mac(self, name: str) -> str:
        addrs, af_link = self._addrs(name)
        for a in addrs or []:
            if a.family == af_link and a.address:
                try:
                    return normalize(a.address)
                except ValueError:
                    break
        rows = ps_json(f"Get-NetAdapter -Name {ps_quote(name)} | Select-Object MacAddress")
        if not rows or not rows[0].get("MacAddress"):
            raise NicError(f"讀不到網卡 {name!r} 的 MAC")
        return normalize(rows[0]["MacAddress"])

    def current_ip(self, name: str) -> str | None:
        """第一個 IPv4 位址；略過 169.254（DHCP 沒拿到時的自動位址）與回環。"""
        addrs, _ = self._addrs(name)
        if addrs:
            import socket
            for a in addrs:
                if a.family == socket.AF_INET and a.address and not a.address.startswith(("169.254.", "127.")):
                    return a.address
            return None                      # 網卡在表裡但沒有可用的 IPv4：確實沒拿到，不必再問 PowerShell
        rows = ps_json(f"Get-NetIPAddress -InterfaceAlias {ps_quote(name)} -AddressFamily IPv4 "
                       "-ErrorAction SilentlyContinue | Select-Object IPAddress")
        for r in rows:
            ip = r.get("IPAddress", "")
            if ip and not ip.startswith(("169.254.", "127.")):
                return ip
        return None

    def gateway(self, name: str) -> str | None:
        rows = ps_json(f"Get-NetRoute -InterfaceAlias {ps_quote(name)} -DestinationPrefix '0.0.0.0/0' "
                       "-ErrorAction SilentlyContinue | Select-Object NextHop")
        return rows[0]["NextHop"] if rows and rows[0].get("NextHop") else None

    # 網卡類別在登錄中的位置（所有網路卡的驅動設定都在這底下，NetworkAddress 就是「自訂 MAC」）
    _CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e972-e325-11ce-bfc1-08002be10318}"
    _ids: dict[str, tuple[str, str]] = {}          # 網卡名稱 → (InterfaceGuid, PnPDeviceID)，第一次查一次就快取

    def _fast_set_mac(self, name: str, m: str) -> bool:
        """快速做法：直接寫登錄的 NetworkAddress，再用 pnputil 重啟該網卡裝置。成功回傳 True，任何一步失敗回傳 False
        （呼叫者改用 Set-NetAdapter）。為什麼：Set-NetAdapter 做的就是這兩件事，但要先啟動 PowerShell 並載入 CIM，
        實測多花約 1.5 秒（5.76 秒 → 4.20 秒，tools/bench_switch.py）。剩下的 4 秒是網卡驅動重啟與網路線重新協商，省不掉。
        shortcut: pnputil /restart-device 需要 Windows 10 2004（19041）以上；更舊的系統自動退回 Set-NetAdapter。"""
        try:
            import subprocess
            import winreg
            ids = self._ids.get(name)
            if not ids:
                rows = ps_json(f"Get-NetAdapter -Name {ps_quote(name)} | Select-Object InterfaceGuid,PnPDeviceID")
                if not rows or not rows[0].get("InterfaceGuid") or not rows[0].get("PnPDeviceID"):
                    return False
                ids = (rows[0]["InterfaceGuid"], rows[0]["PnPDeviceID"])
                self._ids[name] = ids
            guid, pnp = ids
            done = False
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, self._CLASS_KEY) as k:
                for i in range(winreg.QueryInfoKey(k)[0]):
                    try:
                        with winreg.OpenKey(k, winreg.EnumKey(k, i), 0, winreg.KEY_READ | winreg.KEY_WRITE) as sub:
                            if winreg.QueryValueEx(sub, "NetCfgInstanceId")[0].lower() == guid.lower():
                                winreg.SetValueEx(sub, "NetworkAddress", 0, winreg.REG_SZ, m)
                                done = True
                                break
                    except OSError:
                        continue                   # 沒有 NetCfgInstanceId 的項目或沒權限讀：略過
            if not done:
                return False
            cp = subprocess.run(["pnputil", "/restart-device", pnp], capture_output=True, timeout=60, creationflags=0x08000000)
            return cp.returncode == 0
        except Exception:
            return False

    def set_mac(self, name: str, mac: str) -> None:
        """只下指令，不等待（等待由 wait_ready 負責）。先試快速做法，失敗再用 Set-NetAdapter（會自動重啟網卡）。"""
        m = check_assignable(mac)
        if not is_admin():
            raise NicError("換 MAC 需要系統管理員權限，請用「以系統管理員身分執行」開啟")
        if self._fast_set_mac(name, m):
            return
        try:
            run_ps(f"Set-NetAdapter -Name {ps_quote(name)} -MacAddress {ps_quote(fmt(m))} -Confirm:$false",
                   timeout=60, check=True)
        except PowerShellError as e:
            raise NicError(f"Set-NetAdapter 失敗：{e}") from e


class LinuxNic:
    """Linux 實作（2026-11-24 新增，組員端用，不需要視窗）。

    讀取只用 /sys/class/net、/proc/net/route 與 psutil：不需要 root、不需要 iproute2、不必解析指令輸出，
    也因此能用假的目錄結構測試。只有「改 MAC」與「叫 DHCP 重新取址」才呼叫外部指令。

    換 MAC 的流程：ip link set dev X down → address M → up，再通知網路管理程式重新取址（_kick_dhcp）。
    為什麼要先 down：多數驅動在介面啟用中改位址會回 EBUSY。
    Linux 的改法不會寫進設定，重開機後網卡回到硬體位址；服務開機後發現 MAC 不在池內，會依 auto_join 再向伺服器要一個。

    shortcut: 只認 NetworkManager、systemd-networkd、dhcpcd 三種取址程式；用 ifupdown + dhclient 的舊系統
    網卡 down/up 後不一定會自動重新取址，wait_ready 會逾時並換回原 MAC（不會留下斷線）。要支援就在 _kick_dhcp 加 dhclient。
    shortcut: 實體有線網卡以「/sys/class/net/X/device 存在、type=1、不是無線」判斷；虛擬網卡（橋接、veth、docker0）
    不會被自動挑中，但用 --iface 明確指定時照辦（方便在容器與命名空間裡測試）。
    """

    def __init__(self, sysfs: str = "/sys/class/net", proc: str = "/proc", run=None, which=None, admin=None):
        import shutil
        import subprocess
        self.sysfs, self.proc = sysfs, proc
        self._run = run or subprocess.run
        self._which = which or shutil.which
        self._admin = admin                     # 測試用：None = 真的檢查 root

    # ---- 讀 /sys
    def _read(self, name: str, leaf: str) -> str | None:
        import os
        try:
            with open(os.path.join(self.sysfs, name, leaf), encoding="ascii") as f:
                return f.read().strip()
        except (OSError, UnicodeDecodeError):
            return None

    def _is_wired_physical(self, name: str) -> bool:
        import os
        base = os.path.join(self.sysfs, name)
        if name == "lo" or self._read(name, "type") != "1":
            return False
        if os.path.exists(os.path.join(base, "wireless")) or os.path.exists(os.path.join(base, "phy80211")):
            return False
        return os.path.exists(os.path.join(base, "device"))       # 虛擬網卡沒有 device

    def _adapter(self, name: str) -> Adapter:
        mac = self._read(name, "address")
        if not mac:
            raise NicError(f"讀不到網卡 {name!r} 的 MAC")
        oper, carrier, speed = self._read(name, "operstate"), self._read(name, "carrier"), self._read(name, "speed")
        up = oper == "up" or (oper == "unknown" and carrier == "1")      # 部分 USB 網卡的 operstate 會一直是 unknown
        idx = self._read(name, "ifindex")
        return Adapter(name, normalize(mac), up, f"{speed} Mbps" if speed and speed.lstrip("-").isdigit() and int(speed) > 0 else "",
                       int(idx) if idx and idx.isdigit() else 0)

    def detect(self, hint: str = "") -> Adapter:
        import os
        try:
            names = sorted(os.listdir(self.sysfs))
        except OSError as e:
            raise NicError(f"讀不到網卡清單 {self.sysfs}：{e}") from e
        if hint:
            if hint not in names:
                raise NicError(f"找不到指定的網卡 {hint!r}；現有網卡：{[n for n in names if n != 'lo']}")
            return self._adapter(hint)
        wired = [self._adapter(n) for n in names if self._is_wired_physical(n)]
        up = [a for a in wired if a.up]
        if not up:
            raise NicError("沒有已連線的有線網卡（請確認網路線與交換機；無線網卡不支援）")
        return up[0]

    def current_mac(self, name: str) -> str:
        mac = self._read(name, "address")
        if not mac:
            raise NicError(f"讀不到網卡 {name!r} 的 MAC")
        try:
            return normalize(mac)
        except ValueError as e:
            raise NicError(f"網卡 {name!r} 的 MAC 格式不認得：{mac!r}") from e

    def current_ip(self, name: str) -> str | None:
        """第一個 IPv4 位址；略過 169.254（DHCP 沒拿到時的自動位址）與回環。"""
        try:
            import psutil
            import socket
            for a in psutil.net_if_addrs().get(name, []):
                if a.family == socket.AF_INET and a.address and not a.address.startswith(("169.254.", "127.")):
                    return a.address
        except Exception:
            pass
        return None

    def gateway(self, name: str) -> str | None:
        """從 /proc/net/route 找該網卡的預設路由（目的地全 0）；位址是小端序十六進位。"""
        import os
        import socket
        import struct
        try:
            with open(os.path.join(self.proc, "net", "route"), encoding="ascii") as f:
                for line in f.read().splitlines()[1:]:
                    c = line.split()
                    if len(c) >= 3 and c[0] == name and c[1] == "00000000":
                        return socket.inet_ntoa(struct.pack("<L", int(c[2], 16)))
        except (OSError, ValueError):
            pass
        return None

    # ---- 改 MAC
    def _is_root(self) -> bool:
        from .winshell import is_admin
        return is_admin() if self._admin is None else bool(self._admin)

    def _ip(self, *args: str, check: bool = True, timeout: float = 15.0):
        ip = self._which("ip")
        if not ip:
            raise NicError("找不到 ip 指令（請安裝 iproute2）")
        cp = self._run([ip, *args], capture_output=True, text=True, timeout=timeout)
        if check and cp.returncode != 0:
            raise NicError(f"ip {' '.join(args)} 失敗：{(cp.stderr or cp.stdout or '').strip()[:300]}")
        return cp

    def set_mac(self, name: str, mac: str) -> None:
        """只下指令，不等待（等待由 wait_ready 負責）。任何一步失敗都先把網卡拉起來再丟 NicError，不留下停用狀態。"""
        m = check_assignable(mac)
        if not self._is_root():
            raise NicError("換 MAC 需要 root 權限，請用 sudo 執行（背景服務由 systemd 以 root 啟動）")
        self._ip("link", "set", "dev", name, "down")
        try:
            self._ip("link", "set", "dev", name, "address", fmt(m, ":").lower())
        finally:
            self._ip("link", "set", "dev", name, "up", check=False)        # 位址設失敗也要拉起來
        self._kick_dhcp(name)

    def _kick_dhcp(self, name: str) -> None:
        """叫網路管理程式重新取址（盡力而為，失敗不影響換 MAC 本身；拿不到位址由 wait_ready 逾時並換回）。
        多數管理程式會自己偵測到網卡 down/up 而重新取址，這裡是補一腳，縮短等待。"""
        def sh(*cmd: str) -> int:
            try:
                return self._run(list(cmd), capture_output=True, text=True, timeout=10).returncode
            except Exception:
                return 1
        if self._which("nmcli") and sh("nmcli", "-t", "-f", "RUNNING", "general") == 0:
            if sh("nmcli", "-w", "0", "device", "connect", name) == 0:          # -w 0：不等啟用完成
                return
        if self._which("networkctl") and self._which("systemctl") and sh("systemctl", "is-active", "--quiet", "systemd-networkd") == 0:
            if sh("networkctl", "reconfigure", name) == 0:
                return
        if self._which("dhcpcd"):
            sh("dhcpcd", "-n", name)


def default_backend() -> NicBackend:
    """依作業系統選網卡實作：Windows 用 PowerShell，其他（Linux）用 sysfs 與 ip。呼叫端不必知道差別。"""
    import sys
    return WindowsNic() if sys.platform == "win32" else LinuxNic()


def wait_ready(backend: NicBackend, name: str, expect_mac: str, timeout: float,
               sleep: Callable[[float], None] = time.sleep,
               clock: Callable[[], float] = time.monotonic, poll: float = 0.25) -> str:
    """輪詢直到「MAC 已是 expect_mac、網卡已連線、拿到非自動位址的 IPv4」，回傳 IP；逾時丟 NicError。

    取代舊工具的固定 sleep：換 MAC 後網卡重啟與 DHCP 的時間不固定（實測 5 秒內就拿到，
    但交換機慢時可能更久），輪詢既快又不會等不夠。
    """
    expect = normalize(expect_mac)
    deadline = clock() + timeout
    last = "尚未取得資料"
    while clock() < deadline:
        try:
            mac = backend.current_mac(name)
            ip = backend.current_ip(name) if mac == expect else None
            last = f"MAC={mac}，IP={ip}"
            if mac == expect and ip:
                return ip
        except NicError as e:
            last = str(e)       # 網卡重啟的瞬間查不到屬正常，繼續等
        sleep(poll)
    raise NicError(f"等待 {timeout:.0f} 秒仍未就緒（{last}）")
