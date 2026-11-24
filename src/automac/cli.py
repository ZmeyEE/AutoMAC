"""命令列介面。只負責解析參數與列印，所有判斷與動作都交給下層模組（可從別的程式直接 import 使用）。

常用：
    automac status                 目前網卡、MAC、IP、池內各 MAC 的狀態
    automac scan                   掃描區網鄰居
    automac probe                  立即探測一次
    automac device list|label <電腦名稱> <標籤>   設備標籤（跟著電腦走）
    automac usage [--days N]       各 MAC 的每日用量（用量帳本）
    automac pool list|add|remove|fixed|label|import|export   （組員端只能 list／export，清單由伺服器管理）
    automac hub init|client-file <檔>   伺服器：啟用伺服器角色並產生 token／匯出給組員的設定檔
    automac join <檔>                    組員：套用伺服器給的設定檔
    automac switch [MAC] [--force] 手動切換（不給 MAC 就由政策挑）；需要系統管理員
    automac run [--dry-run] [--auto|--no-auto] [--duration 秒]   背景監看（伺服器可用）
    automac gui                    開啟視窗（內嵌服務）
    automac launch                 組員雙擊 exe：安裝／啟動背景服務與系統匣圖示並開視窗檢視
    automac tray                   系統匣圖示（登入時自動啟動）
    automac install-service|uninstall-service   背景服務的安裝與移除（Windows：排程工作；Linux：systemd 單元，需 root）
    automac live                   Linux 無視窗版：讀背景服務的即時狀態與最近事件
    automac auto on|off            Linux 無視窗版：開關「被限速時自動切換」
    automac throttle <MAC>         手動把某個 MAC 標成限速（偵測還沒判定、或手動切走舊的時）
    automac unthrottle <MAC>       清除某個 MAC 的限速標記（偵測誤判，或限速提早結束時）

Linux 組員端沒有視窗與系統匣：用 `sudo automac install-service` 裝成 systemd 服務，之後用 `automac live` 或 journalctl 看狀況。
"""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import secrets
import sys
import time
from pathlib import Path

from . import __version__, config, nic as nicmod
from .config import Settings, StateStore
from .events import Event, EventBus
from .macaddr import fmt, normalize
from .probe import run_probe
from .scanner import scan as scan_subnet
from .service import Service
from .switcher import Switcher
from .traffic import TrafficSampler
from .winshell import is_admin

KIND_LABEL = {"current": "本機使用中", "fixed": "固定占用", "in_use": "他人使用中", "throttled": "已知限速", "free": "可用"}


def _load(args) -> tuple[Settings, Path]:
    path = Path(args.config) if args.config else config.default_config_path()
    return config.load(path), path


def _labels(s: Settings) -> dict[str, str]:
    return {e.mac: e.label for e in s.pool}


def _print_event(ev: Event) -> None:
    print(ev.line(), flush=True)


def _setup_file_log(bus: EventBus) -> None:
    """把事件同時寫進 %APPDATA%\\AutoMAC\\automac.log（1 MB 輪替 3 份）。"""
    try:
        d = config.default_config_path().parent      # 日誌放在設定檔旁邊（伺服器用 SYSTEM 排程執行時才不會跑到系統帳號的目錄）
        d.mkdir(parents=True, exist_ok=True)
        h = logging.handlers.RotatingFileHandler(d / "automac.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        lg = logging.getLogger("automac")
        lg.setLevel(logging.INFO)
        lg.addHandler(h)
        bus.subscribe(lambda ev: lg.info(ev.line()))
    except OSError:
        pass


# ---------------------------------------------------------------------- 指令
def cmd_status(args) -> int:
    s, path = _load(args)
    n = nicmod.default_backend()
    a = n.detect(s.iface)
    print(f"設定檔：{path}{'（尚未建立，使用預設值）' if not path.exists() else ''}")
    print(f"網卡：{a.name}{'（' + a.speed + '）' if a.speed else ''}  MAC：{fmt(a.mac)}  IP：{n.current_ip(a.name)}  閘道：{n.gateway(a.name)}")
    print(f"系統管理員：{'是' if is_admin() else '否（只能監看，無法換 MAC）'}   自動切換：{'開' if s.auto_mode else '關'}")
    for p in s.validate():
        print("設定問題：", p)
    if not s.pool:
        print("池內沒有 MAC，請先 `automac pool add` 或 `automac pool import 舊設定檔`")
        return 0
    st = StateStore()
    from .policy import classify
    in_use = {}
    if not args.no_scan:
        for x in scan_subnet(a.name, own_mac=a.mac):
            in_use[x.mac] = f"{x.ip}（鄰居掃描）"
    print("\n池內 MAC：")
    for m in classify(s, st, a.mac, in_use, time.time()):
        until = f"，約 {time.strftime('%m/%d %H:%M', time.localtime(m.until))} 恢復" if m.until else ""
        print(f"  {fmt(m.mac)}  {m.label or '-':16} {KIND_LABEL[m.kind]}{('：' + m.detail) if m.detail and m.kind not in ('current', 'fixed') else ''}{until}")
    return 0


def cmd_scan(args) -> int:
    s, _ = _load(args)
    n = nicmod.default_backend()
    a = n.detect(s.iface)
    t = time.perf_counter()
    res = scan_subnet(a.name, own_mac=a.mac)
    labels = _labels(s)
    print(f"掃描完成（{time.perf_counter() - t:.1f} 秒），網卡 {a.name}，共 {len(res)} 台：")
    for x in res:
        tag = f"  ← 池內 {labels.get(x.mac) or ''}" if x.mac in labels else ""
        print(f"  {x.ip:16} {fmt(x.mac)}{tag}")
    return 0


def cmd_probe(args) -> int:
    s, _ = _load(args)
    a = nicmod.default_backend().detect(s.iface)
    sm = TrafficSampler(a.name, s.sample_interval_s)
    sm.step()
    time.sleep(1.5)
    sm.step()
    r = run_probe(s, sm)
    print(f"ok={r.ok} 有效={r.valid} {r.mbps:.2f} Mbps（{r.seconds * 1000:.0f} ms）其他流量 {r.other_mbps:.2f} Mbps {r.error}")
    print(f"判斷：{'偏慢（低於 ' + str(s.probe_low_mbps) + ' Mbps）' if r.ok and r.valid and r.mbps < s.probe_low_mbps else '正常或不採用'}")
    return 0 if r.ok else 2


def cmd_pool(args) -> int:
    s, path = _load(args)
    act = args.action
    if s.is_client and act not in ("list", "export"):
        print("這台是組員端：MAC 清單由伺服器管理，請在伺服器上新增、刪除或修改（組員端會自動同步）。", file=sys.stderr)
        return 1
    if act == "list":
        for e in s.pool:
            print(f"{fmt(e.mac)}  {e.label or '-':16} {'固定占用' if e.fixed else ''}")
        print(f"共 {len(s.pool)} 個")
        return 0
    if act == "add":
        ok = s.add_mac(args.mac, args.label or "", args.fixed)
        print("已新增" if ok else "已存在，未變更")
    elif act == "remove":
        print("已刪除" if s.remove_mac(args.mac) else "池內沒有這個 MAC")
    elif act == "label":
        print("已更新標籤" if s.set_label(args.mac, args.text) else "池內沒有這個 MAC")
    elif act == "fixed":
        e = s.find(args.mac)
        if not e:
            print("池內沒有這個 MAC")
            return 1
        e.fixed = args.value == "on"
        print(f"{fmt(e.mac)} 固定占用 = {e.fixed}")
    elif act == "import":
        raw = json.loads(Path(args.file).read_text(encoding="utf-8"))
        imp = Settings.from_legacy(raw) if "mac_list" in raw else Settings.from_dict(raw)
        added = sum(s.add_mac(e.mac, e.label, e.fixed) for e in imp.pool)
        print(f"匯入 {added} 個新 MAC（略過重複 {len(imp.pool) - added} 個）")
    elif act == "export":
        Path(args.file).write_text(json.dumps({"pool": [vars(e) for e in s.pool]}, ensure_ascii=False, indent=2),
                                   encoding="utf-8")
        print("已匯出到", args.file)
        return 0
    config.save(s, path)
    return 0


def cmd_hub(args) -> int:
    """伺服器管理：init 設定角色與 token；client-file 匯出給組員的設定檔（放在組員的 exe 旁就免手動設定）。"""
    s, path = _load(args)
    if args.action == "init":
        import socket
        from . import tlsutil
        s.role = "hub"
        if len(s.hub_token) < 16 or args.new_token:        # 舊 token 若曾在明文通道出現過就要換
            s.hub_token = secrets.token_urlsafe(32)          # 256 位元
        cert = Path(s.hub_cert_file) if s.hub_cert_file else path.parent / "hub-cert.pem"
        key = Path(s.hub_key_file) if s.hub_key_file else path.parent / "hub-key.pem"
        try:
            ip = nicmod.default_backend().current_ip(nicmod.default_backend().detect(s.iface).name)
        except Exception:
            ip = None
        names = [n for n in (socket.gethostname(), socket.gethostname() + ".local", ip) if n]
        try:
            fp = tlsutil.generate_cert(cert, key, names)
        except tlsutil.TlsError as e:
            print(f"錯誤：{e}", file=sys.stderr)
            return 1
        s.hub_cert_sha256 = fp
        config.save(s, path)
        print(f"憑證：{cert}（私鑰 {key} 已限制權限，不可外傳）\n指紋 SHA-256：{fp}")
        print(f"已設定為伺服器（埠 {s.hub_port}，TLS 1.3）。token 存在 {path}；請勿公開。")
        print("下一步：automac hub client-file automac-client.json，把這個檔放在組員的 automac.exe 旁邊。")
        return 0
    if s.role != "hub":
        print("這台還不是伺服器，請先執行 `automac hub init`。", file=sys.stderr)
        return 1
    import socket
    d = config.client_file_dict(s)
    if not d["hub_url"]:                          # 預設用伺服器的 .local 名稱：系統內建的 mDNS 解析，伺服器換 IP 也找得到
        d["hub_url"] = f"https://{socket.gethostname()}.local:{s.hub_port}"
    Path(args.file).write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"已匯出組員設定檔：{args.file}（含 token，只給實驗室成員，不要上傳）")
    return 0


def cmd_join(args) -> int:
    """組員端：套用伺服器給的設定檔。保留本機其他設定，只改角色與連線資訊；清單連上伺服器後才同步。"""
    s, path = _load(args)
    d = json.loads(Path(args.file).read_text(encoding="utf-8"))
    if d.get("role") != "client" or not d.get("hub_token"):
        print("這不是組員設定檔（需要 role=client 與 hub_token）", file=sys.stderr)
        return 1
    if not d.get("hub_cert_sha256"):
        print("組員設定檔缺少 hub_cert_sha256（伺服器憑證指紋），請向管理者重新索取", file=sys.stderr)
        return 1
    for k in config.CLIENT_FILE_KEYS:
        if k in d:
            setattr(s, k, d[k])
    config.save(s, path)
    print(f"已加入伺服器（{s.hub_url or '自動從區網尋找'}），設定存在 {path}")
    return 0


def cmd_device(args) -> int:
    """設備標籤（跟著電腦走）：label 設定、list 列出。伺服器上的服務會自動重讀。"""
    from .devices import DeviceLabels
    s, path = _load(args)
    dev = DeviceLabels(path.parent / "devices.json")
    if args.action == "list":
        for k, v in sorted(dev.all().items()):
            print(f"{k:24} {v}")
        print(f"共 {len(dev.all())} 台")
        return 0
    dev.set(args.name, args.text)
    print(f"電腦 {args.name} 的標籤已{'設為「' + args.text.strip() + '」' if args.text.strip() else '清除（改顯示電腦名稱）'}")
    return 0


def cmd_usage(args) -> int:
    """列出各 MAC 的每日用量（讀伺服器或單機的帳本檔；只含跑本工具的電腦的流量）。"""
    from .ledger import Ledger, fmt_bytes
    s, path = _load(args)
    led = Ledger(path.parent / "usage.json", s.quota_reset_hour)
    hist = led.history(args.days)
    if not hist:
        print("帳本沒有資料（組員端要連上伺服器、伺服器要有跑服務一段時間才會有）")
        return 0
    labels = _labels(s)
    quota = s.daily_quota_gb * 1e9
    for day, rows in hist.items():
        print(f"== {day}")
        for mac, (rx, tx) in sorted(rows.items(), key=lambda kv: -(kv[1][0] + kv[1][1])):
            print(f"  {fmt(mac)}  {labels.get(mac) or '-':16} 下載 {fmt_bytes(rx):>10}  上傳 {fmt_bytes(tx):>10}  "
                  f"約 {(rx + tx) / quota * 100:5.1f}% / {s.daily_quota_gb:g} GB")
    return 0


def _switch_via_service(args, s: Settings, path: Path) -> int | None:
    """背景服務在跑時，請它去切換（和視窗同一條路），不另起程序動網卡。服務沒在跑回傳 None，由呼叫端自己切換。
    為什麼：服務有自己的切換旗標、取樣器與宣告，另一個程序同時換 MAC，兩邊會互相打架（網卡被換兩次、用量基準錯亂）。
    等待方式：輪詢事件直到出現「已切換為」（成功）或服務的切換旗標放下（失敗），並把過程事件印出來。"""
    import urllib.error
    call = _local_caller(s, path, quiet=True)
    if call is None:
        return None
    try:
        seq = call("/v1/status")["seq"]
    except (urllib.error.URLError, OSError, ValueError, KeyError):
        return None                                           # 服務沒在跑或 token 不符：自己切換
    r = call("/v1/switch", {"mac": normalize(args.mac) if args.mac else None, "force": bool(args.force)})
    if not r.get("accepted"):
        print(f"失敗：{r.get('detail') or '服務拒絕了這次切換'}", file=sys.stderr)
        return 1
    t0, ok = time.time(), False
    while time.time() - t0 < s.adapter_wait_s + 60:
        time.sleep(0.5)
        try:
            for e in call(f"/v1/events?since={seq}")["events"]:
                seq = max(seq, e["seq"])
                print(f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} [{e['kind']}] {e['message']}", flush=True)
                ok = ok or (e["kind"] == "switch" and e["message"].startswith("已切換為"))
            if ok or (time.time() - t0 > 2 and not call("/v1/status")["status"].get("switching")):
                break
        except (urllib.error.URLError, OSError, ValueError, KeyError):
            time.sleep(1)                                     # 換 MAC 的瞬間本機連線理論上不會斷，保險起見重試
    return 0 if ok else 1


def cmd_switch(args) -> int:
    s, path = _load(args)
    via = _switch_via_service(args, s, path)
    if via is not None:
        return via
    bus = EventBus()
    bus.subscribe(_print_event)
    n = nicmod.default_backend()
    a = n.detect(s.iface)
    presence = None
    if s.use_presence:
        try:
            from .presence import Presence
            presence = Presence(s.mdns_service)
            ip = n.current_ip(a.name)
            if ip:
                presence.start(ip)
                presence.announce(a.mac, "holding")
                time.sleep(2.5)                 # 給同伴一點時間回應，才看得到對方的宣告
        except Exception as e:
            bus.emit("warn", f"mDNS 不可用，改為只靠鄰居掃描：{e}")
            presence = None
    allocator = None
    if s.is_client and s.hub_url:                 # 組員端有指定伺服器網址才走伺服器；否則退回本機判斷
        import socket
        from .hub import HubClient
        allocator = HubClient(lambda: [s.hub_url.rstrip("/")], s.hub_token, socket.gethostname(), s.hub_cert_sha256)
    sw = Switcher(s, StateStore(), n, a.name, scan_fn=lambda: scan_subnet(a.name, own_mac=n.current_mac(a.name)),
                  presence=presence, bus=bus, allocator=allocator)
    try:
        res = sw.switch_to(args.mac, "manual", args.force) if args.mac else sw.auto_switch("manual")
    finally:
        if presence:
            presence.stop()
    print(res.detail if res.ok else f"失敗：{res.detail}")
    return 0 if res.ok else 1


def service_lock_name(config_dir: Path) -> str:
    """背景服務的單一執行個體鎖名稱：依設定資料夾區分（同一份設定只能有一個服務；測試用的暫存資料夾互不干擾）。"""
    import hashlib
    return "Global\\AutoMAC-Service-" + hashlib.sha1(str(config_dir.resolve()).lower().encode("utf-8")).hexdigest()[:12]


def cmd_run(args) -> int:
    """背景服務進入點。同一份設定已經有服務在跑就直接結束（排程工作重複觸發、使用者手動又執行一次，都不會多出一份）。"""
    from .winshell import acquire_single_instance, release_single_instance
    s, path = _load(args)
    lock = acquire_single_instance(service_lock_name(path.parent))
    if lock is None:
        print("已經有 AutoMAC 背景服務在執行，這一份不再啟動")
        return 0
    try:
        return _run(args, s, path)
    finally:
        release_single_instance(lock)


def _stop_on_sigterm() -> None:
    """把 SIGTERM 當成 Ctrl+C 處理，讓服務走正常的收尾流程（停止取樣、通知伺服器釋放租約、取消 mDNS 宣告）。
    為什麼：systemd 在關機、重開機、停止服務時送的是 SIGTERM，預設行為是行程立刻結束、什麼都不收尾，
    租約就只能等 lease_ttl_s 逾時，Pi 關機後它的 MAC 要三分鐘才會空出來。只能在主執行緒設定處理常式。"""
    import signal

    def _term(signum, frame):
        raise KeyboardInterrupt

    try:
        signal.signal(signal.SIGTERM, _term)
    except (ValueError, OSError, AttributeError):
        pass


def _run(args, s, path) -> int:
    if args.auto is not None:
        s.auto_mode = args.auto
    bus = EventBus()
    bus.subscribe(_print_event)
    _setup_file_log(bus)
    _lower_priority()
    svc = Service(s, StateStore(), bus=bus, dry_run=args.dry_run, config_path=path)
    end = time.time() + args.duration if args.duration else float("inf")
    _stop_on_sigterm()
    while True:                                   # 開機時網卡可能還沒好：不要結束，等到找得到網卡為止（排程工作只會重試 5 次）
        try:
            svc.start()
            break
        except nicmod.NicError as e:
            bus.emit("warn", f"網卡還沒就緒，30 秒後重試：{e}")
            if time.time() >= end:
                return 1
            try:
                time.sleep(30)
            except KeyboardInterrupt:             # 等網卡時收到停止訊號：服務還沒起來，沒有東西要收尾
                return 0
    api = None
    if s.local_api:
        from .localapi import LocalServer, load_or_create_token
        try:
            api = LocalServer(svc, load_or_create_token(path.parent), s.local_port)
            api.start()
            bus.emit("info", f"本機控制介面已啟動（127.0.0.1:{api.port}），視窗只是它的檢視器，關掉視窗不影響服務")
        except OSError as e:
            bus.emit("warn", f"本機控制介面無法啟動（埠 {s.local_port} 被占用？）：{e}")
    try:
        while time.time() < end:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    if api:
        api.stop()
    svc.stop()
    return 0


def _lower_priority() -> None:
    """背景服務用「低於正常」的優先權，不跟使用者在做的事搶 CPU。"""
    try:
        import psutil
        psutil.Process().nice(10 if sys.platform != "win32" else psutil.BELOW_NORMAL_PRIORITY_CLASS)   # Linux：nice 10
    except Exception:
        pass


def cmd_tray(args) -> int:
    """系統匣圖示（排程工作在使用者登入時執行）。"""
    from . import tray
    s, path = _load(args)
    return tray.run(path.parent, s.local_port)


def cmd_launch(args) -> int:
    from .launcher import launch_gui
    return launch_gui(args.config)


def cmd_install_service(args) -> int:
    from . import svcinstall
    s, path = _load(args)
    if sys.platform.startswith("linux"):
        from . import svc_linux
        import os
        pp = (os.environ.get("PYTHONPATH") or "").split(os.pathsep)[0]       # 安裝腳本的 automac 指令會設成 /opt/automac/lib
        unit = svc_linux.install(path, pythonpath=pp or None)
        print(f"背景服務已安裝並啟動：{unit}")
        print(f"查看狀態：automac live    日誌：journalctl -u {svc_linux.UNIT} -f")
        return 0
    if not getattr(sys, "frozen", False):
        print("只有打包後的 automac.exe 能安裝背景服務", file=sys.stderr)
        return 1
    dst = svcinstall.install(Path(sys.executable), path.parent)
    print(f"背景服務已安裝並啟動：{dst}")
    return 0


def cmd_uninstall_service(args) -> int:
    s, path = _load(args)
    if sys.platform.startswith("linux"):
        from . import svc_linux
        svc_linux.uninstall(path, purge=args.purge)
        print("AutoMAC 背景服務已移除" + ("（含本機設定）" if args.purge else f"（設定與紀錄保留在 {path.parent}，要清掉請加 --purge）"))
        return 0
    from . import svcinstall
    svcinstall.uninstall(path.parent, purge=args.purge)
    msg = ("AutoMAC 已完整移除（含本機設定）" if args.purge else
           "背景服務與系統匣圖示已移除（設定與紀錄保留在 " + str(path.parent) + "，要清掉請加 --purge 或刪除該資料夾）")
    print(msg)
    if getattr(sys, "frozen", False):                 # 視窗版 exe 沒有主控台，用訊息框告知
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "AutoMAC", 0x40)  # type: ignore[attr-defined]
        except Exception:
            pass
    return 0


def _local_caller(s: Settings, path: Path, quiet: bool = False):
    """回傳 call(路徑, 內容=None)：帶 token 呼叫本機背景服務的控制介面；讀不到 token 檔就印出原因（quiet 則不印）並回傳 None。
    GET 不給內容、POST 給字典。token 檔只有 root 讀得到，所以組員端的指令要用 sudo。"""
    from .localapi import LOCAL_HOST, TOKEN_FILE, TOKEN_HEADER
    import urllib.request
    try:
        tok = (path.parent / TOKEN_FILE).read_text(encoding="ascii").strip()
    except OSError:
        if not quiet:
            print(f"讀不到 {path.parent / TOKEN_FILE}：背景服務還沒執行過，或需要用 sudo 執行。", file=sys.stderr)
        return None

    def call(p: str, body: dict | None = None):
        req = urllib.request.Request(f"http://{LOCAL_HOST}:{s.local_port}{p}", headers={TOKEN_HEADER: tok},
                                     method="GET" if body is None else "POST",
                                     data=None if body is None else json.dumps(body).encode("utf-8"))
        with urllib.request.urlopen(req, timeout=3) as r:
            return json.loads(r.read().decode("utf-8"))
    return call


def cmd_auto(args) -> int:
    """無視窗版的「被限速時自動切換」開關（視窗版是勾選方塊）：透過本機控制介面通知背景服務，服務會存回設定檔，重開機後維持。"""
    import urllib.error
    s, path = _load(args)
    call = _local_caller(s, path)
    if call is None:
        return 1
    try:
        call("/v1/auto", {"on": args.value == "on"})
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"背景服務沒有回應（埠 {s.local_port}）：{e}", file=sys.stderr)
        return 1
    print(f"已通知背景服務：被限速時自動切換 = {'開' if args.value == 'on' else '關'}")
    return 0


def cmd_live(args) -> int:
    """無視窗版的即時檢視（Linux 組員端用）：問背景服務現在的狀態與最近事件。服務沒在跑就明說。"""
    import urllib.error
    s, path = _load(args)
    call = _local_caller(s, path)
    if call is None:
        return 1
    try:
        st, ev = call("/v1/status")["status"], call("/v1/events?since=0")
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"背景服務沒有回應（埠 {s.local_port}）：{e}", file=sys.stderr)
        print("檢查：systemctl status automac-client", file=sys.stderr)
        return 1
    mac = st.get("mac") or ""
    print(f"狀態：{st.get('state')}   網卡：{st.get('adapter')}   MAC：{fmt(mac) if len(mac) == 12 else mac}   IP：{st.get('ip')}")
    print(f"流量：下載 {st.get('rx', 0):.2f} Mbps  上傳 {st.get('tx', 0):.2f} Mbps   自動切換：{'開' if st.get('auto_mode') else '關'}   伺服器：{st.get('hub') or '-'}")
    for e in ev.get("events", [])[-args.lines:]:
        print(f"{time.strftime('%H:%M:%S', time.localtime(e.get('ts', 0)))} [{e.get('kind')}] {e.get('message')}")
    return 0


def cmd_unthrottle(args) -> int:
    """清除某個 MAC 的限速標記（偵測誤判、或限速提早結束時用）。背景服務在跑就請它清：組員端會連伺服器清掉全員共用的那份，
    各台電腦約 30 秒內跟著清；服務沒在跑就只清這台電腦的狀態檔（伺服器上的標記沒動，要說清楚）。"""
    import urllib.error
    s, path = _load(args)
    mac = normalize(args.mac)
    call = _local_caller(s, path, quiet=True)
    if call is not None:
        try:
            r = call("/v1/unthrottle", {"mac": mac})
            print(f"已清除 {fmt(mac)} 的限速標記" if r.get("cleared") else f"{fmt(mac)} 原本就沒有限速標記")
            return 0
        except (urllib.error.URLError, OSError, ValueError):
            pass
    st = StateStore(path.with_name("state.json"))
    had = st.throttled_until_of(mac, time.time()) is not None
    st.clear_throttled(mac)
    print(f"背景服務沒在跑，只清了這台電腦的狀態檔（{'原本有標記' if had else '原本就沒有標記'}）；伺服器上的標記沒有動。")
    return 0


def cmd_throttle(args) -> int:
    """手動把某個 MAC 標成限速：偵測還沒判定、或手動切走舊的 MAC 時用。需要背景服務在跑（要連伺服器通知全員）。"""
    import urllib.error
    s, path = _load(args)
    mac = normalize(args.mac)
    call = _local_caller(s, path)
    if call is None:
        return 1
    try:
        call("/v1/throttle", {"mac": mac})
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"背景服務沒有回應（埠 {s.local_port}）：{e}", file=sys.stderr)
        return 1
    print(f"已標記 {fmt(mac)} 為限速（到下次重置時間；取消用 automac unthrottle {fmt(mac)}）")
    return 0


def cmd_gui(args) -> int:
    from .gui import main as gui_main
    if getattr(sys, "frozen", False):                # 視窗已經開著就帶到最前面，不再開第二個（伺服器的接手模式會停背景服務，兩個視窗會互相打架）
        from .tray import bring_to_front, find_main_window
        hwnd = find_main_window()
        if hwnd:
            bring_to_front(hwnd)
            return 0
    if not args.takeover:
        return gui_main(args.config)
    from . import takeover                       # 伺服器：先停背景服務（避免兩份同名服務衝突），關視窗後恢復
    takeover.stop_background()
    try:
        return gui_main(args.config)
    finally:
        takeover.restart_background()


# ---------------------------------------------------------------------- 入口
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="automac", description="校園網 MAC 限速偵測與自動切換")
    p.add_argument("--config", help="設定檔路徑（預設 %%APPDATA%%\\AutoMAC\\config.json，或環境變數 AUTOMAC_CONFIG）")
    p.add_argument("--version", action="version", version=f"automac {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)
    st = sub.add_parser("status", help="目前狀態與池內各 MAC")
    st.add_argument("--no-scan", action="store_true", help="不掃描鄰居（較快）")
    st.set_defaults(fn=cmd_status)
    sub.add_parser("scan", help="掃描區網鄰居").set_defaults(fn=cmd_scan)
    sub.add_parser("probe", help="立即探測一次").set_defaults(fn=cmd_probe)
    po = sub.add_parser("pool", help="管理 MAC 池")
    ps = po.add_subparsers(dest="action", required=True)
    ps.add_parser("list")
    a = ps.add_parser("add")
    a.add_argument("mac")
    a.add_argument("label", nargs="?", default="")
    a.add_argument("--fixed", action="store_true", help="標記為被其他裝置長期占用")
    r = ps.add_parser("remove")
    r.add_argument("mac")
    lb = ps.add_parser("label")
    lb.add_argument("mac")
    lb.add_argument("text", help="新的標籤（空字串 = 清除）")
    f = ps.add_parser("fixed")
    f.add_argument("mac")
    f.add_argument("value", choices=["on", "off"])
    for name in ("import", "export"):
        x = ps.add_parser(name)
        x.add_argument("file")
    po.set_defaults(fn=cmd_pool)
    hb = sub.add_parser("hub", help="伺服器管理")
    hs = hb.add_subparsers(dest="action", required=True)
    hi = hs.add_parser("init", help="啟用伺服器角色、產生憑證與 token")
    hi.add_argument("--new-token", action="store_true", help="重新產生 token（舊的組員設定檔會失效）")
    cf = hs.add_parser("client-file", help="匯出給組員的設定檔")
    cf.add_argument("file")
    hb.set_defaults(fn=cmd_hub)
    jn = sub.add_parser("join", help="組員端：套用伺服器給的設定檔")
    jn.add_argument("file")
    jn.set_defaults(fn=cmd_join)
    dv = sub.add_parser("device", help="設備標籤（標籤跟著電腦走）")
    ds = dv.add_subparsers(dest="action", required=True)
    ds.add_parser("list")
    dl = ds.add_parser("label")
    dl.add_argument("name", help="電腦名稱（視窗「標籤」欄預設顯示的就是它）")
    dl.add_argument("text", help="標籤；空字串 = 清除")
    dv.set_defaults(fn=cmd_device)
    us = sub.add_parser("usage", help="各 MAC 的每日用量")
    us.add_argument("--days", type=int, default=7)
    us.set_defaults(fn=cmd_usage)
    sw = sub.add_parser("switch", help="手動切換 MAC（需系統管理員）")
    sw.add_argument("mac", nargs="?")
    sw.add_argument("--force", action="store_true", help="即使偵測到別人在用也切換")
    sw.set_defaults(fn=cmd_switch)
    ru = sub.add_parser("run", help="背景監看與自動切換")
    ru.add_argument("--dry-run", action="store_true", help="只報告，不真的換 MAC")
    g = ru.add_mutually_exclusive_group()
    g.add_argument("--auto", dest="auto", action="store_true", default=None)
    g.add_argument("--no-auto", dest="auto", action="store_false")
    ru.add_argument("--duration", type=float, default=0, help="執行秒數（0 = 直到 Ctrl+C）")
    ru.set_defaults(fn=cmd_run)
    lv = sub.add_parser("live", help="讀背景服務的即時狀態與最近事件（無視窗）")
    lv.add_argument("--lines", type=int, default=15, help="顯示最近幾筆事件")
    lv.set_defaults(fn=cmd_live)
    th = sub.add_parser("throttle", help="手動把某個 MAC 標成限速")
    th.add_argument("mac")
    th.set_defaults(fn=cmd_throttle)
    ut = sub.add_parser("unthrottle", help="清除某個 MAC 的限速標記（偵測誤判時用）")
    ut.add_argument("mac")
    ut.set_defaults(fn=cmd_unthrottle)
    au = sub.add_parser("auto", help="開關「被限速時自動切換」（無視窗版；需背景服務在跑）")
    au.add_argument("value", choices=["on", "off"])
    au.set_defaults(fn=cmd_auto)
    sub.add_parser("tray", help="系統匣圖示（右下角），登入時自動啟動").set_defaults(fn=cmd_tray)
    sub.add_parser("launch", help="組員雙擊 exe 的流程：確保背景服務在跑，開視窗檢視").set_defaults(fn=cmd_launch)
    sub.add_parser("install-service", help="安裝背景服務（排程工作，開機自動啟動）").set_defaults(fn=cmd_install_service)
    un = sub.add_parser("uninstall-service", help="移除背景服務與系統匣圖示")
    un.add_argument("--purge", action="store_true", help="連本機設定資料夾一起刪除")
    un.set_defaults(fn=cmd_uninstall_service)
    gu = sub.add_parser("gui", help="開啟視窗")
    gu.add_argument("--takeover", action="store_true", help="開視窗前先停掉背景服務，關閉後恢復（伺服器用）")
    gu.set_defaults(fn=cmd_gui)
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):          # 繁中 Windows 主控台預設 cp950，遇到無法編碼的符號不要崩潰
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (nicmod.NicError, ValueError) as e:
        print(f"錯誤：{e}", file=sys.stderr)
        return 1
    except PermissionError as e:                       # Linux：設定檔在 /var/lib/automac（只有 root 讀得到，內含 token）
        print(f"錯誤：沒有權限讀寫 {e.filename}。請改用 sudo 執行。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
