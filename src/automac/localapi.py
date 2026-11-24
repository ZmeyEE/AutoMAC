"""本機控制介面：背景服務對「同一台電腦上的視窗」開放的小 API，讓視窗變成純粹的檢視器。

為什麼（2026-11-24 使用者要求）：組員端必須能在背景持續運作，不能因為不小心關掉視窗就停止偵測與用量記錄。
所以拆成兩個程序：
    背景服務（automac run，開機自動啟動，SYSTEM 身分）  ← 真正做偵測、切換、同步、記錄的人
    視窗（automac.exe 雙擊）                              ← 只透過這個 API 看狀態、下指令；關掉視窗不影響服務
視窗用的 Service 介面（status、pool_statuses、switch_now、probe_now、resume、set_auto、bus）由 LocalClient 實作，
視窗程式碼完全不用知道它在跟背景服務講話，還是自己內嵌了一個服務（開發、單機、伺服器接手模式）。

安全：只綁 127.0.0.1；每個請求要帶 token（存在設定檔旁的 local.token，只有系統管理員與 SYSTEM 讀得到），
常數時間比對；token 放在自訂標頭，瀏覽器的網頁做不出這種跨站請求（要先過 CORS 預檢，我們不回應預檢）。
shortcut: 沒加密（本機迴圈位址，同機的系統管理員本來就能做任何事）；若之後要開放給遠端管理，要改成 TLS。
"""
from __future__ import annotations

import dataclasses
import hmac
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from pathlib import Path

from .config import _atomic_write
from .events import Event, EventBus
from .policy import MacStatus
from .switcher import SwitchResult

LOCAL_HOST = "127.0.0.1"
TOKEN_HEADER = "X-AutoMAC-Local-Token"
TOKEN_FILE = "local.token"


def load_or_create_token(config_dir: Path) -> str:
    """服務啟動時呼叫：沒有 token 檔就產生（256 位元），並限制權限只給系統與管理員。"""
    p = config_dir / TOKEN_FILE
    try:
        tok = p.read_text(encoding="ascii").strip()
        if len(tok) >= 32:
            return tok
    except OSError:
        pass
    tok = secrets.token_urlsafe(32)
    config_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write(p, tok)
    try:
        from .tlsutil import _restrict_key
        _restrict_key(p)
    except Exception:
        pass
    return tok


def _jsonable(o):
    return dataclasses.asdict(o) if dataclasses.is_dataclass(o) else o


# --------------------------------------------------------------------------- 服務端
class LocalServer:
    """把一個 Service 包成本機 HTTP/JSON 控制介面。port=0 讓系統選空埠（測試用）。"""

    def __init__(self, service, token: str, port: int = 47602):
        self.svc, self.token = service, token
        svc, tok = service, token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def _send(self, code: int, obj) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _authed(self) -> bool:
                host = (self.headers.get("Host") or "").split(":")[0]
                if host not in (LOCAL_HOST, "localhost"):                       # 擋 DNS 重綁定
                    self._send(403, {"error": "host 不允許"})
                    return False
                if hmac.compare_digest((self.headers.get(TOKEN_HEADER) or "").encode(), tok.encode()):
                    return True
                self._send(401, {"error": "token 不正確"})
                return False

            def do_GET(self):
                if not self._authed():
                    return
                path, _, query = self.path.partition("?")
                q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
                try:
                    if path == "/v1/status":
                        self._send(200, {"status": _jsonable(svc.status()), "seq": svc.bus.last_seq()})
                    elif path == "/v1/pool":
                        sts = svc.pool_statuses(max_age_s=8.0, force=q.get("force") == "1")
                        self._send(200, [_jsonable(s) for s in sts])
                    elif path == "/v1/events":
                        evs = svc.bus.since(int(q.get("since", "0")))
                        self._send(200, {"events": [{"seq": e.seq, "ts": e.ts, "kind": e.kind, "message": e.message}
                                                    for e in evs], "last": svc.bus.last_seq()})
                    elif path == "/v1/info":
                        self._send(200, {"pid": os.getpid(), "role": svc.s.role, "version": _version()})
                    else:
                        self._send(404, {"error": "未知路徑"})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

            def do_POST(self):
                if not self._authed():
                    return
                try:
                    n = int(self.headers.get("Content-Length", "0"))
                    if n > 4096:
                        self._send(413, {"error": "請求太大"})
                        return
                    b = json.loads(self.rfile.read(n) or b"{}")
                    if self.path == "/v1/switch":
                        r = svc.switch_now(b.get("mac") or None, bool(b.get("force")))
                        if isinstance(r, SwitchResult):                         # 同步結果 = 被拒絕（切換進行中）
                            self._send(200, {"accepted": False, "detail": r.detail})
                        else:
                            self._send(200, {"accepted": True, "detail": ""})
                    elif self.path == "/v1/probe":
                        svc.probe_now()
                        self._send(200, {"ok": True})
                    elif self.path == "/v1/resume":
                        svc.resume()
                        self._send(200, {"ok": True})
                    elif self.path == "/v1/auto":
                        svc.set_auto(bool(b.get("on")))
                        self._send(200, {"ok": True})
                    elif self.path == "/v1/unthrottle":
                        self._send(200, {"ok": True, "cleared": svc.clear_throttle(b.get("mac", ""))})
                    else:
                        self._send(404, {"error": "未知路徑"})
                except (ValueError, KeyError) as e:
                    self._send(400, {"error": str(e)})
                except Exception as e:
                    self._send(500, {"error": f"{type(e).__name__}: {e}"})

        self._httpd = ThreadingHTTPServer((LOCAL_HOST, port), Handler)
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="automac-local-api", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


def _version() -> str:
    from . import __version__
    return __version__


# --------------------------------------------------------------------------- 視窗端
class LocalClient:
    """視窗用的 Service 替身：實作視窗會呼叫的那幾個方法，背後透過本機 API 跟背景服務講話。
    背景執行緒每秒更新狀態、每秒取新事件放進自己的 EventBus（視窗照舊訂閱 bus）。
    服務沒回應時 status() 回傳明確的「背景服務沒有回應」狀態，而不是讓視窗卡住或假裝正常。"""

    def __init__(self, token: str, port: int = 47602, poll_s: float = 1.0, timeout: float = 3.0):
        from .service import Status
        self._Status = Status
        self.token, self.port, self.poll_s, self.timeout = token, port, poll_s, timeout
        self.bus = EventBus()
        self.hub = None                          # 視窗的「編輯標籤」等伺服器功能只在內嵌模式有
        self.switcher = object()                 # 視窗用「switcher is None」判斷服務是否就緒
        self._status = Status(state="連線背景服務中…")
        self._connected = False
        self._seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="automac-local-poll", daemon=True)
        self._thread.start()

    # ---- HTTP
    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        req = urllib.request.Request(f"http://{LOCAL_HOST}:{self.port}{path}", method=method,
                                     data=json.dumps(body).encode("utf-8") if body is not None else None,
                                     headers={TOKEN_HEADER: self.token, "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    # ---- 背景輪詢
    def _loop(self) -> None:
        while not self._stop.is_set():
            self.refresh()
            self._stop.wait(self.poll_s)

    def refresh(self) -> bool:
        """更新狀態與事件一次。回傳是否連得上。"""
        try:
            d = self._call("GET", "/v1/status")
            ev = self._call("GET", f"/v1/events?since={self._seq}")
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            if self._connected or not self._status.state.startswith("背景服務"):
                self.bus.emit("warn", f"背景服務沒有回應：{getattr(e, 'reason', e)}")
            self._connected = False
            with self._lock:
                self._status = self._Status(state="背景服務沒有回應（可能正在重啟）")
            return False
        st = d["status"]
        st["last_probe"] = SimpleNamespace(**st["last_probe"]) if st.get("last_probe") else None
        st["last_verdict"] = SimpleNamespace(**st["last_verdict"]) if st.get("last_verdict") else None
        with self._lock:
            self._status = self._Status(**{k: v for k, v in st.items() if k in self._Status.__dataclass_fields__})
        if not self._connected:
            self._connected = True
        for e in ev["events"]:
            self.bus.emit(e["kind"], e["message"])          # 視窗事件紀錄顯示服務端的事件（時間戳用視窗收到的時間）
        self._seq = ev["last"]
        return True

    # ---- Service 介面
    @property
    def connected(self) -> bool:
        return self._connected

    def status(self):
        with self._lock:
            return self._status

    def pool_statuses(self, max_age_s: float = 30.0, force: bool = False) -> list[MacStatus]:
        # 強制更新時服務端要先跟伺服器同步一次，可能要數秒，逾時給長一點（一般狀態輪詢仍用短逾時，服務掛掉才不會卡住視窗）
        rows = self._call("GET", "/v1/pool?force=1" if force else "/v1/pool", timeout=20 if force else None)
        return [MacStatus(**r) for r in rows]

    def switch_now(self, mac: str | None = None, force: bool = False):
        r = self._call("POST", "/v1/switch", {"mac": mac, "force": force})
        if not r["accepted"]:
            self.bus.emit("warn", "已有切換進行中，請稍候（約 10 到 20 秒），不需要重複點擊")
        return r

    def probe_now(self) -> None:
        self._call("POST", "/v1/probe", {})

    def resume(self) -> None:
        self._call("POST", "/v1/resume", {})

    def set_auto(self, on: bool) -> None:
        self._call("POST", "/v1/auto", {"on": on})

    def clear_throttle(self, mac: str) -> bool:
        return bool(self._call("POST", "/v1/unthrottle", {"mac": mac}).get("cleared"))

    def stop(self) -> None:
        """關視窗：只停掉自己的輪詢，背景服務繼續跑。"""
        self._stop.set()


__all__ = ["LocalServer", "LocalClient", "load_or_create_token", "LOCAL_HOST", "TOKEN_FILE"]
