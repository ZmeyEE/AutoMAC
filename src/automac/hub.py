"""伺服器登記處（hub）與組員端（client）：MAC 池只存在伺服器，組員端同步並向伺服器要 MAC。

為什麼要有這一層（2026-11-24 使用者要求）
    原本每台電腦各自一份 MAC 清單、各自掃描、各自搶（對等式）。實驗室實際情況是「伺服器是登記處」，
    組員只要「同步清單、要一個沒人用的 MAC」。集中後：
      - 清單只在伺服器維護（新增、刪除、標籤、固定占用），組員端不能改，不會各台不一致
      - 由伺服器掃描區網並記錄誰租了哪個 MAC，一次決定分配，兩台同時要 MAC 不會拿到同一個
      - 某台發現某 MAC 被限速，回報後全員都避開（限速是 MAC 的屬性，不是哪台電腦的）
    原有的 mDNS 宣告與鄰居掃描保留當第二道防線與「連不上伺服器時」的備援（用本機快取的清單）。

整體流程
    組員端 ── GET  /v1/pool      ─→ 伺服器回傳池內每個 MAC 的狀態（從這台電腦的角度）
              POST /v1/allocate  ─→ 伺服器挑一個沒人用的 MAC 並登記「預約」，回傳給組員端去換
              POST /v1/report    ─→ 換好後回報「我現在用 X」（租約）；或回報「X 被限速」
    伺服器自己也是一台會換 MAC 的電腦：它直接呼叫 Hub，不經過網路（LocalPort）。

租約（lease）：每台電腦最多一筆，記它目前持有（holding）或已預約（pending）的 MAC。
    holding 要定期回報（組員端約每 30 秒），超過 lease_ttl_s 沒回報就視為離線，MAC 釋放。
    pending 是「剛分配、還沒換好」，超過 pending_ttl_s 沒變成 holding 就作廢（例如換失敗）。

安全（校園網上其他人也連得到這個埠；方案與文獻見 tlsutil.py、research/參考文獻.md）
    傳輸：TLS 1.3（只允許 1.3），前向保密；組員端先比對伺服器憑證指紋（釘選）才送任何資料。
    驗證：所有請求必須帶 token（X-AutoMAC-Token，256 位元隨機值，TLS 內傳送，常數時間比對）；
          同一來源連續驗證失敗 5 次就鎖 60 秒，回 429，避免暴力猜測與被拿來當 CPU 消耗攻擊。
    握手在處理執行緒內做（有 10 秒逾時），不是在接受連線的主執行緒，慢速或亂送資料的連線卡不住整個服務。
"""
from __future__ import annotations

import hmac
import http.client
import json
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

from .config import Settings, StateStore, next_quota_reset
from .macaddr import fmt, normalize
from .policy import MacStatus, classify, rank_candidates
from .scanner import Neighbor
from . import tlsutil
from .ledger import Ledger
from .devices import DeviceLabels

API = "/v1"
TOKEN_HEADER = "X-AutoMAC-Token"
KIND_WHY = {"current": "這台正在用", "fixed": "固定占用", "in_use": "他人使用中", "throttled": "已知被限速"}


class HubError(RuntimeError):
    """伺服器明確拒絕或沒有可用 MAC（訊息可直接給使用者看）。"""


class HubUnreachable(HubError):
    """連不上伺服器（網路中斷、伺服器關機、換 MAC 中）。組員端據此改用本機快取備援。"""


class HubPinError(HubUnreachable):
    """伺服器憑證指紋不符（換了憑證或有人冒充）。算「連不上」：不送 token、退回本機備援，但訊息要讓使用者看到。"""


# --------------------------------------------------------------------------- 登記處（純邏輯，可直接測試）
@dataclass
class Lease:
    mac: str
    host: str
    ip: str
    ts: float
    state: str        # holding | pending


class Hub:
    """MAC 池的唯一權威：狀態判斷、分配、租約、限速紀錄。不碰網路與系統（掃描函式與時鐘由外部傳入）。"""

    def __init__(self, settings: Settings, state: StateStore, scan_fn: Callable[[], list[Neighbor]] | None = None,
                 wall: Callable[[], float] = time.time, scan_ttl_s: float = 20.0, ledger: Ledger | None = None,
                 scan_fresh_s: float = 30.0, devices: DeviceLabels | None = None):
        self.s, self.state, self.scan_fn, self.wall = settings, state, scan_fn, wall
        self.ledger = ledger                         # 用量帳本；None = 不記用量
        self.devices = devices or DeviceLabels(None)  # 設備標籤（電腦名稱 → 標籤）
        self.scan_ttl_s = scan_ttl_s
        self.scan_fresh_s = scan_fresh_s     # 分配時掃描結果的最大年齡；超過才重掃。
        # shortcut: 30 秒內才上線、又不跑本工具（所以租約看不到）的裝置，分配時可能還看不到；它們多半長期開機（Pi），風險小
        self._lock = threading.RLock()
        self._leases: dict[str, Lease] = {}          # client_id → 持有中的租約（holding）
        self._pending: dict[str, Lease] = {}         # client_id → 已預約但還沒換好的（換失敗時原本的租約不能丟）
        self._scan: tuple[float, dict[str, str]] = (0.0, {})
        self._released: dict[tuple[str, str], tuple[float, str]] = {}   # (電腦, 剛離開的 MAC) → (時間, 當時的 IP)
        self._scan_lock = threading.Lock()           # 掃描要數秒，同時只做一個，其他請求等結果

    # ---- 內部
    def _expire(self, now: float) -> None:
        for d, ttl in ((self._leases, self.s.lease_ttl_s), (self._pending, self.s.pending_ttl_s)):
            for cid in [c for c, l in d.items() if now - l.ts > ttl]:
                del d[cid]

    def _do_scan(self, block: bool) -> dict[str, str]:
        """真的掃一次並更新快取。block=False（背景更新）遇到別人正在掃就直接放棄。掃描失敗沿用上次結果。"""
        if not self._scan_lock.acquire(blocking=block):
            return self._scan[1]
        try:
            try:
                res = {n.mac: f"{n.ip}（鄰居掃描）" for n in self.scan_fn()}   # type: ignore[misc]
            except Exception:
                res = self._scan[1]
            self._scan = (self.wall(), res)
            return res
        finally:
            self._scan_lock.release()

    def _scanned(self, fresh: bool) -> dict[str, str]:
        """鄰居掃描結果（MAC → 說明）。
        fresh=True（分配時，呼叫者願意等幾秒）一定重掃；一般檢視只回傳快取，過期就在背景更新，
        不能讓組員端的同步請求卡在掃描上（實測整網段掃描要數秒，會超過組員端的逾時而被誤判成連不上伺服器）。"""
        if not self.scan_fn:
            return {}
        if fresh:
            t, cached = self._scan
            if t > 0 and self.wall() - t < self.scan_fresh_s:          # t>0：真的掃過（空結果也算，區網可能真的沒別人）
                return cached                   # 背景每 scan_ttl_s 更新一次，分配時不必再等數秒的整網段掃描
            return self._do_scan(True)
        t, cached = self._scan
        if self.wall() - t >= self.scan_ttl_s:
            threading.Thread(target=self._do_scan, args=(False,), name="automac-hub-scan", daemon=True).start()
        return cached

    def _in_use(self, exclude: str, scanned: dict[str, str]) -> dict[str, str]:
        out = dict(scanned)
        now = self.wall()
        # 發問的這台電腦剛離開的 MAC：區網鄰居表（ARP）要約一分鐘才會忘記它，這段時間掃描仍看到「舊 IP 在用」，
        # 但那就是這台電腦自己剛釋放的，不該擋它換回去（實測：B5→B7 後立刻換回 B5 被誤判成他人使用中）。
        # 只在掃描到的 IP 與它離開時的 IP 相同、且在 120 秒內才豁免；別台電腦拿走的話 IP 會不同，仍會被擋。
        for (cid, m), (ts, ip) in list(self._released.items()):
            if now - ts > 120:
                self._released.pop((cid, m), None)
            elif cid == exclude and ip and out.get(m, "").startswith(ip):
                out.pop(m, None)
        with self._lock:
            self._expire(now)
            for d, how in ((self._leases, "伺服器登記"), (self._pending, "已預約")):
                for cid, l in d.items():
                    if cid != exclude:
                        out[l.mac] = f"{l.host}（{how}）"
        return out

    def start_background_scan(self, stop: threading.Event) -> threading.Thread | None:
        """背景定期掃描區網，讓分配請求直接用快取。stop 被設定時結束。"""
        if not self.scan_fn:
            return None

        def loop():
            while not stop.is_set():
                self._do_scan(True)
                stop.wait(self.scan_ttl_s)
        th = threading.Thread(target=loop, name="automac-hub-bgscan", daemon=True)
        th.start()
        return th

    # ---- 查詢
    def view(self, client_id: str, mac: str, fresh: bool = False) -> dict:
        """池內每個 MAC 從 client 角度看的狀態。mac 是這台電腦目前的 MAC（會標成 current，不算被占用）。"""
        now = self.wall()
        sts = classify(self.s, self.state, mac, self._in_use(client_id, self._scanned(fresh)), now)
        used = self.ledger.today(now) if self.ledger else {}
        with self._lock:
            self._expire(now)
            holders = {l.mac: cid for cid, l in self._leases.items()}     # 持有中的租約：MAC → 電腦名稱（含發問的這台自己）
        rows = []
        for x, e in zip(sts, self.s.pool):
            h = holders.get(x.mac, "")
            # 顯示的標籤：有人持有就顯示「那台電腦的標籤」（沒自訂就是電腦名稱）；沒人持有的固定占用才顯示 MAC 自己的標籤
            label = (self.devices.get(h) or h) if h else (e.label if e.fixed else "")
            rows.append({"mac": x.mac, "label": label, "plabel": e.label, "holder": h, "fixed": e.fixed, "kind": x.kind,
                         "detail": x.detail, "until": x.until, "rx": used.get(x.mac, (0, 0))[0],
                         "tx": used.get(x.mac, (0, 0))[1]})
        return {
            "ts": now,
            "pool": rows,
            "throttled": {m: u for m, u in self.state.throttled_until.items() if u > now},
        }

    def statuses(self, client_id: str, mac: str, fresh: bool = False) -> list[MacStatus]:
        return [MacStatus(p["mac"], p["label"], p["kind"], p["detail"], p["until"], p["rx"], p["tx"], p["holder"])
                for p in self.view(client_id, mac, fresh)["pool"]]

    # ---- 分配
    def allocate(self, client_id: str, host: str, mac: str, want: str | None = None) -> str:
        """替 client 挑（或確認）一個沒人用的 MAC 並預約。want=None 由政策挑；否則檢查指定的那個。
        不能分配回 HubError（訊息寫明原因）。分配與預約在同一把鎖內完成，兩台同時要不會拿到同一個。"""
        scanned = self._scanned(True)                # 掃描要數秒，放在鎖外；下面「讀租約→挑→預約」才是一個不可分割的動作
        with self._lock:
            sts = classify(self.s, self.state, mac, self._in_use(client_id, scanned), self.wall())
            if want:
                w = normalize(want)
                st = next((x for x in sts if x.mac == w), None)
                if st is None:
                    raise HubError(f"{fmt(w)} 不在 MAC 池內")
                if st.kind != "free":
                    why = KIND_WHY[st.kind] + (f"（{st.detail}）" if st.detail and st.kind != "fixed" else "")
                    raise HubError(f"{fmt(w)} 目前不可用：{why}")
                pick = w
            else:
                usage = {m: rx + tx for m, (rx, tx) in self.ledger.today(self.wall()).items()} if self.ledger else None
                cands = rank_candidates(sts, mac, usage)          # 優先今天用得最少的
                if not cands:
                    raise HubError("池內沒有可用的 MAC（都在使用中、被限速或固定占用）")
                pick = cands[0]
            self._pending[client_id] = Lease(pick, host, "", self.wall(), "pending")
            return pick

    def report(self, client_id: str, host: str, mac: str, ip: str = "", throttled: bool = False) -> None:
        """組員端回報。throttled=False：我目前持有 mac（刷新租約）；True：mac 被限速（全員之後都避開）。"""
        now = self.wall()
        if throttled:
            self.state.mark_throttled(mac, next_quota_reset(now, self.s.quota_reset_hour))
            return
        with self._lock:
            prev = self._leases.get(client_id)
            if prev and prev.mac != normalize(mac):
                self._released[(client_id, prev.mac)] = (now, prev.ip)
            self._leases[client_id] = Lease(normalize(mac), host, ip, now, "holding")
            self._pending.pop(client_id, None)       # 已經換好（或回報了現況），預約完成

    def release(self, client_id: str, mac: str) -> bool:
        """組員端關機或停止服務時主動交還租約：它持有的 MAC 立刻空出，不必等 lease_ttl_s（預設 180 秒）逾時。
        只在租約（或預約）的 MAC 與參數相同時才移除，避免晚到的舊請求把剛換好的新租約刪掉。回傳是否真的移除了租約。
        shortcut: 空出的是「租約」；若那台電腦的 IP 還留在伺服器的鄰居表（停機後約一分鐘內），掃描仍會把這個 MAC 視為使用中，
        所以對其他電腦實際可用的時間是租約與鄰居表兩者較晚的那個。"""
        m = normalize(mac)
        with self._lock:
            pend = self._pending.get(client_id)
            if pend and pend.mac == m:
                del self._pending[client_id]
            lease = self._leases.get(client_id)
            if lease and lease.mac == m:
                del self._leases[client_id]
                self._released[(client_id, m)] = (self.wall(), lease.ip)
                return True
        return False

    def set_device_label(self, client_id: str, label: str) -> None:
        """設定某台電腦的標籤（跟著電腦走，它換到哪個 MAC 就顯示在哪個 MAC 上）。"""
        self.devices.set(client_id, label)

    def add_usage(self, client_id: str, mac: str, rx: int, tx: int) -> None:
        """組員端回報一段區間的用量（位元組，歸給當時用的 MAC）。數字不合理丟 ValueError。"""
        if self.ledger:
            self.ledger.add(mac, rx, tx, self.wall())

    def leases(self) -> list[Lease]:
        with self._lock:
            self._expire(self.wall())
            return list(self._leases.values())


# --------------------------------------------------------------------------- HTTPS 伺服端
class AuthThrottle:
    """同一來源連續驗證失敗太多次就暫時封鎖。時鐘可注入以便測試。
    shortcut: 以來源 IP 計算，同一台 NAT 後面的多人會共用額度；實驗室同網段直連，影響很小。"""

    def __init__(self, max_fail: int = 5, lock_s: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self.max_fail, self.lock_s, self.clock = max_fail, lock_s, clock
        self._fails: dict[str, int] = {}
        self._until: dict[str, float] = {}
        self._lock = threading.Lock()

    def blocked_for(self, ip: str) -> float:
        """還要封鎖幾秒；沒被封回傳 0。"""
        with self._lock:
            left = self._until.get(ip, 0.0) - self.clock()
            if left <= 0:
                self._until.pop(ip, None)
                return 0.0
            return left

    def fail(self, ip: str) -> None:
        with self._lock:
            n = self._fails.get(ip, 0) + 1
            self._fails[ip] = n
            if n >= self.max_fail:
                self._until[ip] = self.clock() + self.lock_s
                self._fails[ip] = 0

    def ok(self, ip: str) -> None:
        with self._lock:
            self._fails.pop(ip, None)


class _TlsHTTPServer(ThreadingHTTPServer):
    """接受連線後包成 TLS，但握手延到處理執行緒（見 Handler.setup）。"""
    daemon_threads = True

    def __init__(self, addr, handler, ctx):
        self._ctx = ctx
        super().__init__(addr, handler)

    def get_request(self):
        sock, addr = super().get_request()
        return self._ctx.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), addr

    def handle_error(self, request, client_address):      # 握手失敗（掃描器、舊版 TLS、明文連線）很常見，不印出來
        pass


class HubServer:
    """把 Hub 包成 HTTPS/JSON 服務（標準函式庫）。port=0 讓系統選空埠（測試用）。"""

    def __init__(self, hub: Hub, token: str, cert_path, key_path, port: int = 47601, host: str = "0.0.0.0",
                 throttle: AuthThrottle | None = None):
        if len(token) < 16:
            raise ValueError("hub_token 太短或空白（至少 16 字元；`automac hub init` 會產生 256 位元的）")
        self.hub, self.token = hub, token
        self.throttle = throttle or AuthThrottle()
        hub_ref, tok, thr = hub, token, self.throttle

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                self.request.settimeout(10)
                self.request.do_handshake()               # 失敗會丟例外，連線直接結束
                super().setup()

            def log_message(self, format, *args):         # 預設會逐行印到 stderr，服務模式下沒人看
                pass

            def _send(self, code: int, obj: dict) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                ip = self.client_address[0]
                left = thr.blocked_for(ip)
                if left > 0:
                    self._send(429, {"error": f"驗證失敗次數過多，請 {int(left) + 1} 秒後再試"})
                    return False
                got = self.headers.get(TOKEN_HEADER, "")
                if hmac.compare_digest(got.encode(), tok.encode()):
                    thr.ok(ip)
                    return True
                thr.fail(ip)
                self._send(401, {"error": "token 不正確"})
                return False

            def do_GET(self):
                if not self._authed():
                    return
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                try:
                    if u.path == API + "/pool":
                        self._send(200, hub_ref.view(q.get("client", [""])[0], q.get("mac", [""])[0]))
                    else:
                        self._send(404, {"error": "未知路徑"})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

            def do_POST(self):
                if not self._authed():
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n > 65536:                         # 正常請求只有幾百位元組
                        self._send(413, {"error": "請求太大"})
                        return
                    b = json.loads(self.rfile.read(n) or b"{}")
                    cid, host, mac = b.get("client", ""), b.get("host", ""), b.get("mac", "")
                    if not cid:
                        self._send(400, {"error": "缺少 client"})
                    elif self.path == API + "/allocate":
                        self._send(200, {"mac": hub_ref.allocate(cid, host, mac, b.get("want"))})
                    elif self.path == API + "/usage":
                        hub_ref.add_usage(cid, mac, int(b.get("rx", 0)), int(b.get("tx", 0)))
                        self._send(200, {"ok": True})
                    elif self.path == API + "/report":
                        hub_ref.report(cid, host, mac, b.get("ip", ""), bool(b.get("throttled")))
                        self._send(200, {"ok": True})
                    elif self.path == API + "/release":
                        self._send(200, {"ok": True, "released": hub_ref.release(cid, mac)})
                    else:
                        self._send(404, {"error": "未知路徑"})
                except HubError as e:
                    self._send(409, {"error": str(e)})
                except (ValueError, KeyError) as e:
                    self._send(400, {"error": str(e)})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

        self._httpd = _TlsHTTPServer((host, port), Handler, tlsutil.server_context(cert_path, key_path))
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="automac-hub", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


# --------------------------------------------------------------------------- 組員端與伺服器本機共用的介面
class HubClient:
    """組員端：透過 HTTPS 呼叫伺服器，連線後先比對憑證指紋（pin）才送請求。
    url_fn 每次呼叫才取網址，可回傳一個或多個（依序嘗試，連得上就用；伺服器換 IP 後名稱或 mDNS 會指到新位址）；
    網址的 http/https 前綴會被忽略，一律用 TLS。"""

    def __init__(self, url_fn: Callable[[], str], token: str, client_id: str, pin: str,
                 timeout: float = 5.0, alloc_timeout: float = 25.0):
        """timeout：一般請求；alloc_timeout：分配請求（伺服器要先重掃區網，數秒）。"""
        self.alloc_timeout = alloc_timeout
        self.url_fn, self.token, self.client_id, self.pin, self.timeout = url_fn, token, client_id, pin, timeout
        self.host = client_id

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> dict:
        bases = self.url_fn()
        bases = [b for b in ([bases] if isinstance(bases, str) else list(bases)) if b]
        if not bases:
            raise HubUnreachable("找不到伺服器（沒設定網址，區網上也沒看到伺服器的宣告）")
        last: HubUnreachable | None = None
        for base in bases:
            try:
                return self._call_one(base, method, path, body, timeout)
            except HubUnreachable as e:           # 連不上或指紋不符：換下一個網址試
                last = e
        assert last is not None
        raise last

    def _call_one(self, base: str, method: str, path: str, body: dict | None, timeout: float | None) -> dict:
        if not self.pin:
            raise HubPinError("沒有設定伺服器憑證指紋（hub_cert_sha256）：請向管理者索取新的組員設定檔")
        u = urllib.parse.urlparse(base if "//" in base else "//" + base)
        host, port = u.hostname or "", u.port or 47601
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {TOKEN_HEADER: self.token, "Content-Type": "application/json"}
        conn = None
        try:
            conn = tlsutil.connect_pinned(host, port, self.pin, timeout or self.timeout)
            conn.request(method, path, body=data, headers=headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.status != 200:                                   # 伺服器有回應但拒絕：訊息要給使用者看
                try:
                    msg = json.loads(raw).get("error", "")
                except Exception:
                    msg = ""
                raise HubError(msg or f"伺服器回應 {resp.status}")
            return json.loads(raw)
        except tlsutil.PinMismatch as e:
            raise HubPinError(str(e)) from e
        except (OSError, http.client.HTTPException, ValueError) as e:  # 含 ssl.SSLError、逾時、連線被拒
            if isinstance(e, HubError):
                raise
            raise HubUnreachable(f"連不上伺服器 {host}:{port}：{getattr(e, 'reason', e)}") from e
        finally:
            if conn is not None:
                conn.close()

    def fetch(self, mac: str) -> dict:
        q = urllib.parse.urlencode({"client": self.client_id, "mac": mac})
        return self._call("GET", f"{API}/pool?{q}")

    def allocate(self, mac: str, want: str | None = None) -> str:
        return self._call("POST", f"{API}/allocate",
                          {"client": self.client_id, "host": self.host, "mac": mac, "want": want},
                          timeout=self.alloc_timeout)["mac"]

    def usage(self, mac: str, rx: int, tx: int) -> None:
        self._call("POST", f"{API}/usage", {"client": self.client_id, "host": self.host, "mac": mac, "rx": rx, "tx": tx})

    def report(self, mac: str, ip: str = "", throttled: bool = False) -> None:
        self._call("POST", f"{API}/report",
                   {"client": self.client_id, "host": self.host, "mac": mac, "ip": ip, "throttled": throttled})

    def release(self, mac: str) -> bool:
        """交還租約（服務停止、關機時）。逾時短，因為關機流程不能被它拖住；舊版伺服器沒有這個路徑會回 404（HubError），
        呼叫端忽略即可，租約逾時後一樣會空出。"""
        r = self._call("POST", f"{API}/release", {"client": self.client_id, "host": self.host, "mac": mac}, timeout=3.0)
        return bool(r.get("released"))


class LocalPort:
    """伺服器自己的組員端：同樣的介面，直接呼叫 Hub（不經網路，伺服器換 MAC 時也不會斷）。"""

    def __init__(self, hub: Hub, client_id: str):
        self.hub, self.client_id, self.host = hub, client_id, client_id

    def fetch(self, mac: str) -> dict:
        return self.hub.view(self.client_id, mac)

    def allocate(self, mac: str, want: str | None = None) -> str:
        return self.hub.allocate(self.client_id, self.host, mac, want)

    def report(self, mac: str, ip: str = "", throttled: bool = False) -> None:
        self.hub.report(self.client_id, self.host, mac, ip, throttled)

    def usage(self, mac: str, rx: int, tx: int) -> None:
        self.hub.add_usage(self.client_id, mac, rx, tx)

    def release(self, mac: str) -> bool:
        return self.hub.release(self.client_id, mac)


__all__ = ["Hub", "HubServer", "HubClient", "LocalPort", "HubError", "HubUnreachable", "HubPinError", "Lease",
           "AuthThrottle"]
