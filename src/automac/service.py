"""背景服務：把取樣、偵測、探針、切換、宣告整合成一個每秒運轉一次的迴圈，並發出事件。
命令列與視窗只是這個服務的「操作面板」，判斷邏輯都在下層模組，這裡只負責串接與排程。

執行緒模型（刻意簡單）
    主迴圈執行緒  每秒 tick 一次：取樣 → 餵偵測器 → 問偵測器要不要探測 → 把探針或切換丟給工作執行緒
    工作執行緒    只有一個（ThreadPoolExecutor(1)）：探針與切換排隊執行，不會同時換 MAC 又探測
    偵測器不是執行緒安全的，所以所有碰它的地方都在 self._dlock 內。
    測試時 synchronous=True，工作直接在 tick 內執行，不需要真的開執行緒或等時間。

自動切換的保護（為什麼不是「偵測到就換」）
    1. auto_mode 關閉時只通知，不動網卡（新安裝預設關閉）
    2. dry_run：只報告「本來會切換到哪個 MAC」，不真的換
    3. Governor：兩次切換最短間隔、每小時上限；超過就停止輪流並通知（可能整池都慢）
    4. 嘗試失敗也要等 min_switch_gap_s 才能再試，避免失敗時每 10 秒折騰一次網卡
"""
from __future__ import annotations

import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

from . import config as cfgmod, nic as nicmod, tlsutil
from .config import PoolEntry, Settings, StateStore
from .detector import Detector, ProbeOutcome, Verdict
from .events import EventBus
from .ledger import Ledger
from .devices import DeviceLabels
from .hub import Hub, HubClient, HubError, HubServer, HubUnreachable, LocalPort
from .macaddr import fmt
from .policy import MacStatus, rank_candidates
from .probe import ProbeResult, run_probe
from .scanner import scan as scan_subnet
from .switcher import SwitchResult, Switcher
from .traffic import TrafficSampler
from .winshell import is_admin


@dataclass
class Status:
    """給視窗與命令列顯示的快照。"""
    adapter: str = ""
    mac: str = ""
    ip: str = ""
    rx: float = 0.0
    tx: float = 0.0
    quiet: bool = False
    state: str = "啟動中"
    strikes: int = 0
    last_probe: ProbeResult | None = None
    last_probe_ts: float | None = None
    last_verdict: Verdict | None = None
    auto_mode: bool = False
    dry_run: bool = False
    admin: bool = False
    halted: str = ""             # 非空代表自動輪流已停止（原因）
    peers: int = 0
    hub: str = ""                # 與伺服器的連線說明（單機模式為空）
    switching: bool = False      # 正在切換 MAC：視窗據此停用按鈕並顯示「切換中」，避免重複點擊排出一堆工作


class Service:
    def __init__(self, settings: Settings, state: StateStore | None = None, nic: nicmod.NicBackend | None = None,
                 bus: EventBus | None = None, dry_run: bool = False, synchronous: bool = False,
                 sampler: TrafficSampler | None = None, probe_fn: Callable = run_probe,
                 scan_fn: Callable | None = None, presence=None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep, config_path=None, client_id: str | None = None):
        """config_path：組員端同步到新清單時存回哪個設定檔（None = 不存，測試用）。
        client_id：在伺服器登記的名字，預設用電腦名稱。"""
        self.s, self.dry_run, self.sync = settings, dry_run, synchronous
        self.config_path, self.client_id = config_path, client_id or socket.gethostname()
        self.hub: Hub | None = None                  # 只有 role == "hub" 才有
        self.hub_server: HubServer | None = None
        self.allocator = None                        # LocalPort（伺服器）或 HubClient（組員端）；單機為 None
        self._hub_view: tuple[float, dict] | None = None
        self._hub_err = ""                           # 非空 = 上次同步失敗的原因
        self._hub_thread: threading.Thread | None = None
        self._last_join = -1e9
        self._join_warned = False
        self._last_sample_t = 0.0                    # 上次成功讀到網卡流量的時間（看門狗用）
        self._monitor_down = False
        self._last_redetect = -1e9
        self._switching = False
        self.ledger: Ledger | None = None            # 單機與伺服器才有；組員端把用量回報給伺服器
        self._usage_base: tuple[int, int] | None = None
        self._usage_mac = ""
        self._usage_pending: dict[str, list[int]] = {}
        self._last_usage = 0.0
        self.state = state or StateStore()
        self.nic = nic or nicmod.WindowsNic()
        self.bus = bus or EventBus()
        self.clock, self.wall, self._probe_fn, self._sleep = clock, wall, probe_fn, sleep
        self.detector = Detector(settings)
        self._dlock = threading.RLock()
        self._exec = None if synchronous else ThreadPoolExecutor(max_workers=1, thread_name_prefix="automac-work")
        self._busy = False
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._status = Status(auto_mode=settings.auto_mode, dry_run=dry_run)
        self._last_attempt = -1e9
        self._net_down_reported = False
        self._halted = ""
        self._pool_cache: tuple[float, list[MacStatus]] = (0.0, [])
        self.adapter = None
        self.sampler = sampler
        self.presence = presence
        self._scan_fn = scan_fn
        self.switcher: Switcher | None = None

    # ------------------------------------------------------------------ 生命週期
    def start(self) -> None:
        """偵測網卡、建立各元件、啟動主迴圈。網卡找不到會丟 NicError（由呼叫者顯示）。"""
        self.adapter = self.nic.detect(self.s.iface)
        if self.sampler is None:
            self.sampler = TrafficSampler(self.adapter.name, self.s.sample_interval_s)
        if self._scan_fn is None:
            self._scan_fn = lambda: scan_subnet(self.adapter.name, own_mac=self.nic.current_mac(self.adapter.name))
        if self.presence is None and self.s.use_presence:
            self.presence = self._make_presence()
        if self.s.role != "client":
            self.ledger = Ledger(self.config_dir() / "usage.json", self.s.quota_reset_hour, clock=self.wall)
        self._setup_hub_role()
        self.switcher = Switcher(self.s, self.state, self.nic, self.adapter.name, scan_fn=self._scan_fn,
                                 presence=self.presence, bus=self.bus, clock=self.wall, sleep=self._sleep,
                                 allocator=self.allocator, pre_switch=self._settle_usage)
        self.switcher.announce_async = not self.sync      # mDNS 重新綁定約要 1 到 2 秒，放背景（實測：視窗按鈕因此多停用 2 秒）
        if self.s.role == "hub":
            self.sync_once()                         # 伺服器自己先登記進租約（區網掃描看不到自己）
        admin = is_admin()
        self._status.admin = admin
        self._status.adapter = self.adapter.name
        self._status.mac = self.adapter.mac
        self._status.ip = self.nic.current_ip(self.adapter.name) or ""
        self.bus.emit("info", f"啟動：網卡 {self.adapter.name}，MAC {fmt(self.adapter.mac)}，IP {self._status.ip}"
                              f"，自動切換={'開' if self.s.auto_mode else '關'}{'，試運行' if self.dry_run else ''}")
        if self.s.auto_mode and not self.dry_run and not admin:
            self.bus.emit("warn", "沒有系統管理員權限：只能監看，無法真的切換 MAC（請以系統管理員執行）")
        if self.s.validate():
            self.bus.emit("warn", "設定有問題：" + "；".join(self.s.validate()))
        if self.sync:
            return
        self._thread = threading.Thread(target=self._loop, name="automac-main", daemon=True)
        self._thread.start()
        if self.allocator:
            self._hub_thread = threading.Thread(target=self._hub_loop, name="automac-hub-sync", daemon=True)
            self._hub_thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        if self._exec:
            self._exec.shutdown(wait=False, cancel_futures=True)
        if self.hub_server:
            try:
                self.hub_server.stop()
            except Exception:
                pass
        if self.presence:
            try:
                self.presence.stop()
            except Exception:
                pass
        self.bus.emit("info", "已停止")

    def _make_presence(self):
        """建立 mDNS 宣告；沒裝 zeroconf 或啟動失敗就降級成只靠鄰居掃描，不影響其他功能。"""
        try:
            from .presence import Presence
            p = Presence(self.s.mdns_service, role="hub" if self.s.role == "hub" else "client")
            threading.Thread(target=self._start_presence, args=(p,), name="automac-mdns-start", daemon=True).start()
            return p
        except Exception as e:
            self.bus.emit("warn", f"mDNS 宣告無法使用，改為只靠鄰居掃描：{e}")
            return None

    def _start_presence(self, p) -> None:
        try:
            ip = self.nic.current_ip(self.adapter.name)
            if ip:
                p.start(ip)
                p.announce(self.nic.current_mac(self.adapter.name), "holding")
                self.bus.emit("info", f"已在區網宣告：目前使用 {fmt(self.nic.current_mac(self.adapter.name))}")
        except Exception as e:
            self.bus.emit("warn", f"mDNS 宣告啟動失敗，改為只靠鄰居掃描：{e}")

    # ------------------------------------------------------------------ 伺服器角色
    def _setup_hub_role(self) -> None:
        """依 role 建立伺服器（登記處＋HTTP）或組員端（同步與分配）。單機模式什麼都不做。"""
        if self.s.role == "hub":
            self.hub = Hub(self.s, self.state, self._scan_fn, wall=self.wall, ledger=self.ledger,
                           devices=DeviceLabels(self.config_dir() / "devices.json"))
            if not self.sync:
                self.hub.start_background_scan(self._stop)
            self.allocator = LocalPort(self.hub, self.client_id)
            if not self.s.hub_token:
                self.bus.emit("error", "hub_token 空白，組員端無法連線：請設定後重新啟動")
                return
            cert, key = self.cert_paths()
            try:
                self.hub_server = HubServer(self.hub, self.s.hub_token, cert, key, self.s.hub_port)
                self.hub_server.start()
                self.bus.emit("info", f"伺服器 API 已啟動（埠 {self.hub_server.port}，TLS 1.3），組員端可同步清單並要 MAC")
            except (OSError, ValueError, tlsutil.TlsError) as e:
                self.bus.emit("error", f"伺服器 API 啟動失敗（埠被占用、憑證不存在或 token 太短）：{e}")
        elif self.s.role == "client":
            self.allocator = HubClient(self._hub_url, self.s.hub_token, self.client_id, self.s.hub_cert_sha256)

    def config_dir(self):
        """設定檔所在資料夾：憑證、用量帳本、日誌都放這裡。"""
        from pathlib import Path
        return Path(self.config_path).parent if self.config_path else cfgmod.default_config_path().parent

    def cert_paths(self):
        """伺服器憑證與私鑰的位置：設定檔指定的優先，否則放在設定檔旁邊。"""
        from pathlib import Path
        base = self.config_dir()
        return (Path(self.s.hub_cert_file) if self.s.hub_cert_file else base / "hub-cert.pem",
                Path(self.s.hub_key_file) if self.s.hub_key_file else base / "hub-key.pem")

    def _hub_url(self) -> list[str]:
        """伺服器網址清單：設定檔指定的（通常是 .local 名稱，由系統的 mDNS 解析，不受程式防火牆規則影響）優先，
        區網上 role=hub 的宣告（直接給 IP）備用。伺服器換 IP 後兩者都會指到新位址。"""
        urls = [self.s.hub_url.rstrip("/")] if self.s.hub_url else []
        if self.presence:
            urls += [f"https://{p.ip}:{self.s.hub_port}" for p in self.presence.peers() if p.role == "hub" and p.ip]
        return urls

    def _hub_loop(self) -> None:
        self._stop.wait(2.0)                       # 讓網卡與宣告先就緒
        while not self._stop.is_set():
            self.sync_once()
            self.maybe_join()
            self.track_usage()
            if self._stop.wait(self.s.hub_sync_s):
                return

    # ------------------------------------------------------------------ 流量監測看門狗
    def _watch_monitor(self, now: float, got_sample: bool) -> None:
        """流量監測的可靠性：讀不到網卡計數器（網卡被停用、拔線重插後換了名稱、驅動重置）超過 10 秒就明確警告、
        狀態列標示「流量監測中斷」（不能默默失效，否則偵測與用量都是假的），並每 30 秒重新偵測網卡嘗試自己恢復。
        切換 MAC 期間網卡本來就會短暫消失，不算故障。"""
        if got_sample or self._switching:
            self._last_sample_t = now
            if self._monitor_down and got_sample:
                self._monitor_down = False
                self.bus.emit("info", "流量監測已恢復")
            return
        if self._last_sample_t == 0.0:
            self._last_sample_t = now
            return
        if now - self._last_sample_t < 10:
            return
        if not self._monitor_down:
            self._monitor_down = True
            self.bus.emit("warn", "流量監測中斷：讀不到網卡的流量計數器（網卡被停用或換了名稱？）。偵測與用量記錄暫停，嘗試重新偵測網卡")
        if now - self._last_redetect >= 30:
            self._last_redetect = now
            self._redetect_adapter()

    def _redetect_adapter(self) -> None:
        """重新找網卡；名稱變了就換掉取樣器與切換器使用的網卡名稱。找不到就維持原狀，下個週期再試。"""
        try:
            a = self.nic.detect(self.s.iface)
        except nicmod.NicError:
            return
        if a.name != self.adapter.name and self.switcher is not None:
            self.bus.emit("info", f"網卡改用 {a.name}（原本的 {self.adapter.name} 讀不到）")
            self.adapter = a
            self.switcher.adapter = a.name
            self.sampler = TrafficSampler(a.name, self.s.sample_interval_s)
            self._usage_base = None                  # 新網卡的計數器與舊的無關，重設用量基準

    # ------------------------------------------------------------------ 切換互斥
    def _begin_switch(self) -> bool:
        """搶「正在切換」旗標；已有切換進行中回傳 False。手動、自動、入網配發共用，同時間只會有一個在動網卡。"""
        with self._dlock:
            if self._switching:
                return False
            self._switching = True
            return True

    def _end_switch(self) -> None:
        with self._dlock:
            self._switching = False

    def _settle_usage(self, old_mac: str) -> None:
        """動網卡之前先把累積的流量結算給舊 MAC（網卡重啟時計數器可能歸零，之後就算不回來了）。"""
        self._collect_usage(owner=old_mac)

    def sync_once(self) -> bool:
        """與伺服器同步一次：組員端抓清單並換掉本機清單，然後回報目前持有的 MAC（刷新租約）。
        伺服器自己只回報。連不上時保留本機快取、只在狀態變化時發一次警告。回傳是否成功。"""
        if not self.allocator or self.switcher is None or self._switching:
            return True                       # 切換中網路本來就會斷，不同步也不警告（切換完會立刻同步）
        mac, ip = self.switcher.current_mac(), self.nic.current_ip(self.adapter.name) or ""
        try:
            self.allocator.report(mac, ip)                    # 先回報（刷新租約），再抓清單：抓到的視圖才包含自己，標籤立刻對
            if self.s.role == "client":
                self._apply_view(self.allocator.fetch(mac))
        except HubError as e:
            if not self._hub_err:
                self.bus.emit("warn", f"與伺服器同步失敗，改用本機快取的清單：{e}" if isinstance(e, HubUnreachable)
                              else f"伺服器拒絕：{e}")
            self._hub_err = str(e)
            return False
        if self._hub_err:
            self.bus.emit("info", "已恢復與伺服器同步")
        self._hub_err = ""
        return True

    # ------------------------------------------------------------------ 用量帳本
    def track_usage(self) -> None:
        """累計這段時間的網卡流量並回報（組員端回報伺服器、伺服器與單機寫本機帳本）。見 ledger.py 的限制說明。"""
        if self.sampler is None or self.switcher is None:
            return
        self._collect_usage()
        self._flush_usage()

    def _collect_usage(self, owner: str | None = None) -> None:
        """把「上次到現在」的網卡累計流量差值記進待送清單，歸給 owner（預設是上次記錄時用的 MAC）。
        網卡計數器歸零（差值為負，例如網卡被重設）就只重設基準，不記。"""
        assert self.sampler is not None and self.switcher is not None
        _, rx, tx = self.sampler.cumulative()
        base, self._usage_base = self._usage_base, (rx, tx)
        if base is None:
            self._usage_mac = self._usage_mac or self.switcher.current_mac()
            return
        drx, dtx = rx - base[0], tx - base[1]
        if drx < 0 or dtx < 0:
            return
        mac = owner or self._usage_mac or self.switcher.current_mac()
        p = self._usage_pending.setdefault(mac, [0, 0])
        p[0] += drx
        p[1] += dtx
        if not owner:
            self._usage_mac = self.switcher.current_mac()

    def _flush_usage(self) -> None:
        """送出待送用量；失敗（伺服器連不上）就留著，下次累加後再送，不會掉資料。"""
        for mac, (rx, tx) in list(self._usage_pending.items()):
            if rx == 0 and tx == 0:
                del self._usage_pending[mac]
                continue
            try:
                if self.allocator:
                    self.allocator.usage(mac, rx, tx)
                elif self.ledger:
                    self.ledger.add(mac, rx, tx)
                del self._usage_pending[mac]
            except HubError:
                break
            except ValueError as e:                       # 伺服器判定數字不合理：丟掉這筆，免得一直重送
                self.bus.emit("warn", f"用量回報被拒絕，已丟棄：{e}")
                del self._usage_pending[mac]

    def _attach_usage(self, sts: list[MacStatus]) -> list[MacStatus]:
        """單機模式：把本機帳本的今日用量補進狀態清單。"""
        if not self.ledger:
            return sts
        from dataclasses import replace
        used = self.ledger.today()
        return [replace(s, used_rx=used.get(s.mac, (0, 0))[0], used_tx=used.get(s.mac, (0, 0))[1]) for s in sts]

    def maybe_join(self) -> bool:
        """入網配發：組員端同步到清單後，若這台電腦目前的 MAC 不在池內（陌生電腦、沒申請的 MAC，
        這種 MAC 多半連不上外網，偵測器只會看到「網路中斷」而不會觸發切換），就主動向伺服器要一個並切換。
        條件：角色是組員端、auto_join 開著、不是試運行、已同步到非空清單、有系統管理員權限。
        失敗（伺服器說沒有可用、換 MAC 失敗）每 join_retry_s 秒再試。回傳這次是否發出了配發。
        shortcut: 換過去後不會在離開時還原原 MAC（原 MAC 本來就是網卡硬體位址，移除設定檔的登錄值即可還原）；要還原就加「退出時還原」。"""
        if (self.s.role != "client" or not self.s.auto_join or self.dry_run or self.switcher is None
                or not self.s.pool or self._hub_view is None):
            return False
        mac = self.switcher.current_mac()
        if any(e.mac == mac for e in self.s.pool):
            return False
        now = self.wall()
        if now - self._last_join < self.s.join_retry_s or self._busy or self._switching:
            return False
        if not self._status.admin:
            if not self._join_warned:
                self._join_warned = True
                self.bus.emit("warn", f"這台電腦的 MAC {fmt(mac)} 不在池內，需要系統管理員權限才能自動向伺服器要一個（請以系統管理員執行）")
            return False
        if not self._begin_switch():
            return False
        self._last_join = now
        with self._dlock:
            self._busy = True
        self.bus.emit("info", f"這台電腦的 MAC {fmt(mac)} 不在池內，向伺服器要一個沒人用的並切換")
        self._submit(self._do_join, now)
        return True

    def _do_join(self, now: float) -> None:
        try:
            assert self.switcher is not None
            res = self.switcher.auto_switch("join")
            self._after_switch(res, now)
            self._end_switch()
            if res.ok:
                self.sync_once()                       # 立刻回報新 MAC，伺服器的租約馬上更新
        finally:
            self._end_switch()
            with self._dlock:
                self._busy = False

    def _apply_view(self, view: dict) -> None:
        """採用伺服器的清單（含刪除）與限速紀錄；有變動才寫回設定檔。"""
        new = [PoolEntry(p["mac"], p.get("plabel", p["label"]), p["fixed"]) for p in view["pool"]]   # 快取存 MAC 自己的標籤，不存「目前是誰在用」
        changed = [(e.mac, e.label, e.fixed) for e in new] != [(e.mac, e.label, e.fixed) for e in self.s.pool]
        self.s.pool = new
        for m, u in view.get("throttled", {}).items():     # 備援用：伺服器連不上時也不會選到已知被限速的
            if not self.state.throttled_until_of(m, self.wall()):
                self.state.mark_throttled(m, u)
        self._hub_view = (self.wall(), view)
        if changed:
            if self.config_path:
                try:
                    cfgmod.save(self.s, self.config_path)
                except OSError as e:
                    self.bus.emit("warn", f"清單已同步但無法存檔：{e}")
            self.bus.emit("info", f"已從伺服器同步清單（{len(new)} 個 MAC）")

    # ------------------------------------------------------------------ 主迴圈
    def _loop(self) -> None:
        while not self._stop.wait(self.s.sample_interval_s):
            try:
                self.tick(self.clock())
            except Exception as e:      # 主迴圈任何例外都不能讓服務死掉；記下來下一秒繼續
                self.bus.emit("error", f"主迴圈錯誤：{type(e).__name__}: {e}")

    def tick(self, now: float) -> None:
        """一個週期。公開出來是為了測試可以直接驅動，不必開執行緒。"""
        assert self.sampler is not None
        smp = self.sampler.step(now)
        self._watch_monitor(now, smp is not None)
        with self._dlock:
            if smp:
                self.detector.on_sample(smp.t, smp.rx)
                self._status.rx, self._status.tx = smp.rx, smp.tx
            self._status.quiet = self.detector.is_quiet(now)
            if self.s.role == "standalone" and now - self._last_usage >= 30:
                self._last_usage = now
                self.track_usage()
            self._status.strikes = self.detector.strikes
            reason = None if (self._busy or self._switching) else self.detector.want_probe(now)
            if reason:
                self._busy = True
        self._refresh_state()
        if reason:
            self._submit(self._do_probe, reason, now)

    def _submit(self, fn, *args) -> Future | None:
        if self._exec is None:
            fn(*args)
            return None
        return self._exec.submit(self._guard, fn, *args)

    def _guard(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as e:
            with self._dlock:
                self._busy = False
            self.bus.emit("error", f"{getattr(fn, '__name__', 'task')} 失敗：{type(e).__name__}: {e}")

    # ------------------------------------------------------------------ 探針與判斷
    def _do_probe(self, reason: str, now: float) -> None:
        try:
            r: ProbeResult = self._probe_fn(self.s, self.sampler)
            t = self.clock() if not self.sync else now
            with self._dlock:
                verdict = self.detector.on_probe(t, ProbeOutcome(r.ok, r.valid, r.mbps))
                self._status.last_probe, self._status.last_probe_ts = r, self.wall()
                self._status.last_verdict = verdict
            self.bus.emit("probe", f"探針（{reason}）：{r.mbps:.2f} Mbps，其他流量 {r.other_mbps:.1f}，"
                                   f"{'有效' if r.valid else '無效'}{'，' + r.error if r.error else ''} → {verdict.kind}",
                          mbps=r.mbps, valid=r.valid, verdict=verdict.kind)
            if verdict.kind == "net_down":
                if not self._net_down_reported:
                    self._net_down_reported = True
                    self.bus.emit("warn", "探針連不上：網路可能中斷（不視為限速，不會切換 MAC）")
            else:
                self._net_down_reported = False
            if verdict.kind == "throttled":
                self._busy = True
                self._on_throttled(t)
        finally:
            with self._dlock:
                self._busy = False

    def _on_throttled(self, now: float) -> None:
        assert self.switcher is not None
        old = self.switcher.current_mac()
        self.bus.emit("verdict", f"判定 {fmt(old)} 被限速：{self._status.last_verdict.detail if self._status.last_verdict else ''}")
        self.switcher.mark_throttled(old)
        if not self.s.auto_mode:
            self.bus.emit("warn", "自動切換已關閉：請手動切換 MAC")
            return
        if self._halted:
            self.bus.emit("warn", f"自動輪流已停止：{self._halted}")
            return
        if now - self._last_attempt < self.s.min_switch_gap_s:
            return
        ok, code, msg = self.switcher.governor.allow(self.wall())
        if not ok:
            if code == "rate":
                self._halted = msg
                self.bus.emit("error", msg)
            else:
                self.bus.emit("info", msg)
            return
        self._last_attempt = now
        if self.dry_run:
            cands = rank_candidates(self.switcher.statuses(), old)
            self.bus.emit("switch", f"（試運行）本來會從 {fmt(old)} 切換到 {fmt(cands[0]) if cands else '（沒有可用 MAC）'}")
            return
        if not self._begin_switch():
            return
        try:
            res = self.switcher.auto_switch("throttled")
        finally:
            self._end_switch()
        self._after_switch(res, now)

    def _after_switch(self, res: SwitchResult, now: float) -> None:
        if res.ok:
            try:
                self._collect_usage(owner=res.old_mac)      # 換 MAC 前累積的流量歸給舊的，之後的歸新的
                self._usage_mac = res.new_mac
            except Exception:
                pass
            with self._dlock:
                self.detector.reset_after_switch(now if self.sync else self.clock())   # 用換完後的時間，退避才從此刻算起
            self._status.mac, self._status.ip = res.new_mac, res.ip
        else:
            self.bus.emit("error", f"自動切換失敗：{res.detail}")

    # ------------------------------------------------------------------ 給 GUI / CLI 的操作
    def resume(self) -> None:
        """使用者確認處理完（例如整池恢復後）才解除「自動輪流已停止」。"""
        self._halted = ""
        self.bus.emit("info", "已解除自動輪流停止狀態")

    def set_auto(self, on: bool) -> None:
        """切換自動模式並存回設定檔（背景服務重開機後維持使用者的選擇）。"""
        self.s.auto_mode = on
        self._status.auto_mode = on
        self.bus.emit("info", f"自動切換已{'開啟' if on else '關閉'}")
        if self.config_path:
            try:
                cfgmod.save(self.s, self.config_path)
            except OSError as e:
                self.bus.emit("warn", f"自動切換設定無法存檔：{e}")

    def switch_now(self, mac: str | None = None, force: bool = False) -> Future | SwitchResult | None:
        """手動切換：指定 MAC，或不指定由政策挑。手動不受 Governor 限制。"""
        assert self.switcher is not None
        if not self._begin_switch():           # 已有切換在進行：直接告知，不排隊（重複點擊曾排出多個工作，後面的只會報「已經是這個 MAC」）
            self.bus.emit("warn", "已有切換進行中，請稍候（約 10 到 20 秒），不需要重複點擊")
            return SwitchResult(False, "", detail="切換進行中")

        def work():
            try:
                res = (self.switcher.switch_to(mac, "manual", force) if mac else self.switcher.auto_switch("manual"))
                self._after_switch(res, self.clock())
                return res
            except Exception as e:
                self.bus.emit("error", f"手動切換失敗：{e}")
                return SwitchResult(False, "", detail=str(e))
            finally:
                self._end_switch()
        if self._exec is None:
            return work()
        return self._exec.submit(work)

    def probe_now(self) -> None:
        """使用者按「立即檢查」：不管流量與排程，直接探一次（結果仍照一般規則處理）。"""
        with self._dlock:
            if self._busy:
                return
            self._busy = True
        self._submit(self._do_probe, "manual", self.clock())

    def pool_statuses(self, max_age_s: float = 30.0, force: bool = False) -> list[MacStatus]:
        """池內每個 MAC 的狀態（含鄰居掃描與宣告）。掃描要數秒，所以有快取。"""
        assert self.switcher is not None
        t, cached = self._pool_cache
        if not force and cached and self.wall() - t < max_age_s:
            return cached
        if self.hub:                                # 伺服器：直接用登記處的視角（含各台的租約）
            st = self.hub.statuses(self.client_id, self.switcher.current_mac(), fresh=force)
        elif self.s.role == "client":               # 組員端：伺服器看得到全貌，優先用它；連不上才退回本機掃描
            if force or self._hub_view is None:
                self.sync_once()
            st = self._view_statuses() or self.switcher.statuses()
        else:
            st = self._attach_usage(self.switcher.statuses())
        self._pool_cache = (self.wall(), st)
        return st

    def _view_statuses(self) -> list[MacStatus] | None:
        """伺服器最近一次回傳的清單轉成 MacStatus；太舊（超過 3 次同步週期）視為過期不用。"""
        if not self._hub_view or self._hub_err or self.wall() - self._hub_view[0] > 3 * self.s.hub_sync_s + 5:
            return None
        return [MacStatus(p["mac"], p["label"], p["kind"], p["detail"], p["until"], p.get("rx", 0), p.get("tx", 0),
                          p.get("holder", "")) for p in self._hub_view[1]["pool"]]

    def status(self) -> Status:
        s = self._status
        s.auto_mode, s.halted = self.s.auto_mode, self._halted
        s.switching = self._switching
        s.hub = self._hub_text()
        s.peers = len(self.presence.peers()) if self.presence and getattr(self.presence, "running", False) else 0
        return s

    def _hub_text(self) -> str:
        if self.s.role == "hub":
            return f"伺服器（組員 API 埠 {self.hub_server.port}）" if self.hub_server else "伺服器（API 未啟動）"
        if self.s.role != "client":
            return ""
        if self._hub_err:
            return "連不上伺服器，使用本機快取的清單"
        if self._hub_view:
            return f"已與伺服器同步（{int(self.wall() - self._hub_view[0])} 秒前）"
        return "尚未與伺服器同步"

    def _refresh_state(self) -> None:
        s = self._status
        if self._monitor_down:
            s.state = "流量監測中斷：讀不到網卡計數器（正在嘗試恢復）"
        elif self._switching:
            s.state = "切換 MAC 中（約 10 到 20 秒，網路會短暫中斷，請勿重複點擊）"
        elif self._busy:
            s.state = "探測／切換中"
        elif self._halted:
            s.state = "已停止輪流"
        elif not self.s.auto_mode:
            s.state = "監看中（自動切換關閉）"
        elif self.dry_run:
            s.state = "監看中（試運行）"
        else:
            s.state = "監看中"
        if not s.quiet and s.rx >= self.s.gate_mbps:
            s.state += "｜流量大，不探測"


__all__ = ["Service", "Status"]
