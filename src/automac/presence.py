"""區網宣告與發現（mDNS / DNS-SD，使用 python-zeroconf）：每台跑本工具的機器宣告「我現在用哪個 MAC」，
大家互相看得到，不需要中央伺服器。

為什麼選 mDNS 而不是自己寫廣播或架 MQTT/HTTP 伺服器（2026-11-23 評估與實測，見 research/方案比較.md）
    - 標準（RFC 6762/6763），內建名稱衝突探測、TTL 到期、離線告別封包，不必自己重造。
    - 伺服器換 MAC 換 IP 後仍能被找到（宣告跟著新位址走）。
    - 實測：Win10 與 Win11 互相通告與發現成功；需要伺服器防火牆放行 Python 的 UDP 5353 進站。

佔用協定（claim，借用 RFC 5227 位址衝突探測的概念）
    1. 想用某 MAC 時先宣告 state=claiming（附毫秒時間戳）。
    2. 等 claim_wait_s 加隨機抖動，看有沒有別人也在宣告同一個 MAC。
    3. 已有人 holding → 放棄；也有人 claiming → 時間戳小的贏，平手比實例名稱；輸的放棄並換下一個。
    4. 實際換完才改宣告為 holding。輸或失敗就撤回宣告。
    這個設計把「兩人同時挑同一個 MAC」的空窗縮到幾乎不可能，不是數學保證（網路掉封包時仍可能漏看），
    所以切換後還有驗證步驟，且鄰居掃描仍是第二道防線。
"""
from __future__ import annotations

import queue
import random
import re
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .macaddr import normalize

TTL = 120   # 秒；程式當掉沒發告別封包時，最久這麼久後別人就不再看到它（預設 4500 秒太久）


@dataclass(frozen=True)
class Peer:
    instance: str       # mDNS 實例名稱（唯一）
    mac: str            # 對方宣告的 MAC（12 字元）
    state: str          # holding | claiming
    ip: str
    ts: int             # 宣告時間（毫秒）
    role: str = "client"


def decide_claim(my_ts: int, my_instance: str, rivals: list[Peer]) -> bool:
    """純函式：給定同一個 MAC 的其他宣告者，我能不能拿到？規則見模組說明。"""
    for p in rivals:
        if p.state == "holding":
            return False
        if p.state == "claiming" and (p.ts, p.instance) < (my_ts, my_instance):
            return False
    return True


def _label(hostname: str) -> str:
    """mDNS 實例標籤：只留英數與連字號，最長 40 字元。"""
    return "automac-" + re.sub(r"[^A-Za-z0-9-]", "-", hostname)[:40]


class Presence:
    def __init__(self, service_type: str, hostname: str | None = None, role: str = "client",
                 clock: Callable[[], float] = time.time, rand: Callable[[float, float], float] = random.uniform):
        self.type = service_type
        label = _label(hostname or socket.gethostname())
        self.instance = label + "." + service_type
        self._server = label + ".local."            # update_service 需要明確的 server；register 才會自動補
        self.role = role
        self._clock, self._rand = clock, rand
        self._zc = None
        self._info = None
        self._browser = None
        self._ip = ""
        self._peers: dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._running = False

    @property
    def running(self) -> bool:
        return self._zc is not None

    # ------------------------------------------------------------------ 生命週期
    def start(self, ip: str) -> None:
        """綁定到指定網卡的 IPv4 開始監聽（機器有多張網卡時，避免從錯的網卡宣告）。"""
        from zeroconf import ServiceBrowser, Zeroconf  # 延遲載入，沒裝 zeroconf 時其他功能仍可用
        self.stop()
        self._ip = ip
        self._zc = Zeroconf(interfaces=[ip])
        self._running = True
        self._worker = threading.Thread(target=self._resolve_loop, name="automac-presence", daemon=True)
        self._worker.start()
        self._browser = ServiceBrowser(self._zc, self.type, handlers=[self._on_change])

    def restart(self, ip: str) -> None:
        """換 MAC 後 IP 變了，要重新綁定。會保留目前宣告的內容。"""
        mac_state = self._current()
        self.start(ip)
        if mac_state:
            self.announce(*mac_state)

    def _current(self) -> tuple[str, str] | None:
        """目前對外宣告的 (MAC, 狀態)；沒宣告回傳 None。"""
        if self._info is None:
            return None
        props = {k.decode(): v.decode() for k, v in self._info.properties.items() if v}
        return (props["mac"], props.get("state", "holding")) if props.get("mac") else None

    def stop(self) -> None:
        self._running = False
        if self._zc is not None:
            try:
                if self._info is not None:
                    self._zc.unregister_service(self._info)   # 送出 TTL=0 的告別封包
            except Exception:
                pass
            try:
                self._zc.close()
            except Exception:
                pass
        self._zc = self._info = self._browser = None
        with self._lock:
            self._peers.clear()

    # ------------------------------------------------------------------ 宣告
    def announce(self, mac: str, state: str = "holding", ts: int | None = None) -> None:
        from zeroconf import ServiceInfo
        if self._zc is None:
            raise RuntimeError("Presence 尚未 start()")
        props = {"mac": normalize(mac), "state": state, "ts": str(ts or int(self._clock() * 1000)),
                 "role": self.role, "v": "1"}
        info = ServiceInfo(self.type, self.instance, port=47600, addresses=[socket.inet_aton(self._ip)],
                           properties=props, server=self._server, host_ttl=TTL, other_ttl=TTL)
        if self._info is None:
            self._zc.register_service(info)         # 含名稱衝突探測，約 1.7 秒
        else:
            self._zc.update_service(info)
        self._info = info

    def withdraw(self) -> None:
        """撤回宣告（放棄佔用時）。"""
        if self._zc is not None and self._info is not None:
            try:
                self._zc.unregister_service(self._info)
            finally:
                self._info = None

    # ------------------------------------------------------------------ 發現
    def _on_change(self, zeroconf, service_type, name, state_change) -> None:
        # 這個回呼跑在 zeroconf 自己的執行緒，不能在裡面做會阻塞的查詢，所以丟給 worker
        self._q.put((service_type, name, state_change))

    def _resolve_loop(self) -> None:
        from zeroconf import ServiceStateChange
        while self._running:
            try:
                stype, name, change = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            if name == self.instance:
                continue                            # 自己的宣告不算同伴
            if change == ServiceStateChange.Removed:
                with self._lock:
                    self._peers.pop(name, None)
                continue
            try:
                zc = self._zc
                info = zc.get_service_info(stype, name, timeout=3000) if zc else None
                if not info:
                    continue
                props = {k.decode(): (v.decode() if v else "") for k, v in info.properties.items()}
                addrs = info.parsed_addresses()
                peer = Peer(name, normalize(props["mac"]), props.get("state", "holding"),
                            addrs[0] if addrs else "", int(props.get("ts", "0")), props.get("role", "client"))
                with self._lock:
                    self._peers[name] = peer
            except Exception:
                continue                            # 對方資料不完整或網路瞬斷，等下一次事件

    def peers(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def holders(self) -> dict[str, Peer]:
        """目前已宣告占用（holding）的 MAC → 對方。"""
        return {p.mac: p for p in self.peers() if p.state == "holding"}

    # ------------------------------------------------------------------ 佔用
    def claim(self, mac: str, wait_s: float = 1.5) -> bool:
        """嘗試取得 mac 的使用權；成功回傳 True（呼叫者接著換 MAC，換完 announce holding）。失敗會撤回宣告。"""
        m = normalize(mac)
        prev = self._current()                       # 輸了要還原成原本「我正在用的 MAC」的宣告，不能讓別人以為它空了
        my_ts = int(self._clock() * 1000)
        self.announce(m, "claiming", ts=my_ts)
        time.sleep(wait_s + self._rand(0.0, 0.5))
        rivals = [p for p in self.peers() if p.mac == m]
        if decide_claim(my_ts, self.instance, rivals):
            return True
        if prev:
            self.announce(*prev)
        else:
            self.withdraw()
        return False
