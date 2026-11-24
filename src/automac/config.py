"""設定檔（使用者可改）與狀態檔（程式自己記）。

兩個檔案分開的原因：設定檔含真實 MAC，只放使用者電腦、不進 GitHub；
狀態檔記「哪個 MAC 何時被限速」這種會一直變的資料，壞了刪掉也不影響設定。

位置（固定路徑，不依賴目前工作資料夾，修掉舊工具用相對路徑的問題）：
    Windows：%APPDATA%\\AutoMAC\\config.json、state.json
    Linux：已安裝成系統服務時是 /var/lib/automac/（只有 root 讀得到，內含 token）；否則 ~/.config/automac/
可用環境變數 AUTOMAC_CONFIG 指定設定檔路徑；狀態檔永遠在設定檔旁邊。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .macaddr import check_assignable, normalize

APP_NAME = "AutoMAC"


# --------------------------------------------------------------------------- 路徑
LINUX_SYSTEM_DIR = Path("/var/lib/automac")     # Linux 背景服務的資料夾（svc_linux.install 建立，權限 0700）


def config_dir() -> Path:
    base = os.environ.get("APPDATA")
    if base:
        return Path(base) / APP_NAME
    if sys.platform.startswith("linux") and LINUX_SYSTEM_DIR.is_dir():
        return LINUX_SYSTEM_DIR              # 裝了系統服務：root 手動下指令與服務讀同一份設定
    return Path.home() / ".config" / "automac"


def default_config_path() -> Path:
    """優先序：環境變數 AUTOMAC_CONFIG → exe 旁邊的 config.json（可攜模式，伺服器用）→ %APPDATA%\\AutoMAC。"""
    env = os.environ.get("AUTOMAC_CONFIG")
    if env:
        return Path(env)
    if getattr(sys, "frozen", False):
        beside = Path(sys.executable).parent / "config.json"
        if beside.exists():
            return beside
        return _programdata_config()
    return config_dir() / "config.json"


def _role_of(p: Path) -> str:
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("role", "")
    except (OSError, ValueError, AttributeError):
        return ""


def _programdata_config() -> Path:
    """exe 的設定檔位置：C:\\ProgramData\\AutoMAC\\config.json。背景服務以 SYSTEM 執行，看不到使用者的 %APPDATA%；
    放在 ProgramData 服務與視窗才讀同一份。舊版組員端（%APPDATA%，角色是 client）已有設定而新位置還沒有時，搬一份過來（只搬一次，不刪舊的）；單機或伺服器的設定不搬。"""
    new = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / APP_NAME / "config.json"
    old = config_dir() / "config.json"
    try:
        migrate = not new.exists() and old.exists() and _role_of(old) == "client"
    except OSError:                       # 舊位置不能讀（例如 Linux 的 /var/lib/automac 只有 root 讀得到）：當作沒有可搬的
        migrate = False
    if migrate:
        try:
            import shutil
            new.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(old, new)
        except OSError:
            pass
    return new


# --------------------------------------------------------------------------- 設定
@dataclass
class PoolEntry:
    """MAC 池中的一筆。fixed=True 表示被不跑本工具的裝置長期占用（例如 Pi），永遠不選它。"""
    mac: str
    label: str = ""
    fixed: bool = False

    def __post_init__(self) -> None:
        self.mac = normalize(self.mac)


@dataclass
class Settings:
    """所有可調參數。預設值都有實測依據（見 docs/開發日誌.md），改之前先看那裡。"""

    # --- 基本
    iface: str = ""                      # 網卡名稱；空字串 = 自動找第一張有線且已連線的網卡
    pool: list[PoolEntry] = field(default_factory=list)
    auto_mode: bool = False              # 預設關：新安裝先只監看，確認無誤再打開

    # --- 角色（見 hub.py）：standalone 單機自己管清單；hub 實驗室伺服器，唯一能改清單；client 組員端，清單從伺服器同步
    role: str = "standalone"
    hub_url: str = ""                    # 組員端用：伺服器網址，例如 https://LAB-SERVER.local:47601；空白 = 從區網宣告自動找
    hub_cert_sha256: str = ""            # 組員端用：伺服器憑證指紋（釘選）；由伺服器匯出的組員設定檔帶入，沒有就不連線
    hub_cert_file: str = ""              # 伺服器用：憑證檔；空白 = 設定檔旁的 hub-cert.pem（hub init 產生）
    hub_key_file: str = ""               # 伺服器用：私鑰檔；空白 = 設定檔旁的 hub-key.pem（權限已限制，不可外傳）
    hub_port: int = 47601
    hub_token: str = ""                  # 伺服器與組員端共用的密語；hub 角色沒設就不開 API
    hub_sync_s: float = 30.0             # 組員端多久同步一次清單並回報租約
    lease_ttl_s: float = 180.0           # 持有中的租約多久沒回報就視為離線
    pending_ttl_s: float = 120.0         # 預約了但沒換成功的租約多久作廢
    auto_join: bool = True               # 組員端：這台電腦目前的 MAC 不在池內（陌生電腦、沒申請的 MAC）時，自動向伺服器要一個並切換
    join_retry_s: float = 60.0           # 入網配發失敗後多久再試
    local_api: bool = True               # 背景服務是否開放本機控制介面給視窗用（localapi.py）
    local_port: int = 47602              # 本機控制介面埠（只綁 127.0.0.1）
    daily_quota_gb: float = 10.0         # 每個 MAC 每日流量上限（使用者描述的規則，未經校方證實）；只用來顯示百分比，不會因此排除 MAC

    # --- 偵測門檻（依 2026-11-23 實測：健康約 95 Mbps，限速約 1 Mbps）
    gate_mbps: float = 3.0               # 網卡流量低於此值才算「安靜」，可以探測
    probe_low_mbps: float = 2.0          # 探針低於此值算偏慢（限速時約 0.9 Mbps、健康時 256 KB 為 7 到 29 Mbps）
    quiet_window_s: float = 10.0         # 判斷安靜用的視窗（取中位數）
    recent_window_s: float = 3.0         # 最近幾秒也必須安靜，避免剛好在下載尾端探測
    idle_probe_interval_s: float = 180.0  # 完全沒在用網路時，每幾秒一個探針
    confirm_strikes: int = 2             # 連續幾次有效且偏慢才判定限速
    strike_gap_s: float = 5.0            # 兩次確認探針的最短間隔
    strike_ttl_s: float = 60.0           # 第一次偏慢後多久內沒再確認就作廢
    invalid_retry_s: float = 10.0        # 探針無效（探測期間有別的流量）後多久再試
    flat_low_range: tuple[float, float] = (0.5, 1.8)   # 限速時的特徵：貼在約 1 Mbps
    flat_low_cv: float = 0.25            # 平線的起伏上限
    # --- 探針
    probe_bytes: int = 262144            # 256 KB：限速時約 2 秒，健康時不到 0.5 秒
    probe_timeout_s: float = 15.0
    probe_urls: list[str] = field(default_factory=lambda: ["https://speed.cloudflare.com/__down"])
    # --- 切換
    min_switch_gap_s: float = 60.0       # 兩次切換最短間隔（給 DHCP 與驗證探針時間）
    max_switches_per_hour: int = 4       # 超過代表可能整池都慢（或校園網整體壅塞），停止輪流並通知
    quota_reset_hour: int = 0            # 被限速的 MAC 假設在每天這個整點恢復（規則未經校方證實，可調）
    adapter_wait_s: float = 45.0         # 換 MAC 後等網卡與 DHCP 就緒的上限（取代舊工具固定 sleep）
    scan_before_switch: bool = True      # 切換前先掃描區網鄰居表
    use_presence: bool = True            # 用 mDNS 互相宣告誰在用哪個 MAC
    claim_wait_s: float = 1.5            # 宣告「我要用這個 MAC」後等多久看有沒有衝突
    # --- 其他
    sample_interval_s: float = 1.0
    allow_hooks: bool = False            # 安全：預設不執行 hooks。命令用 shell 執行，設定檔等同可執行程式，要用請自己明確開啟
    hooks_before: list[str] = field(default_factory=list)  # 切換前執行的命令（例：暫停某服務）；需 allow_hooks
    hooks_after: list[str] = field(default_factory=list)   # 切換後執行的命令；環境變數 AUTOMAC_OLD_MAC / NEW_MAC / NEW_IP
    mdns_service: str = "_automac._tcp.local."

    # ---- 存取工具
    def pool_macs(self) -> list[str]:
        return [e.mac for e in self.pool]

    def find(self, mac: str) -> PoolEntry | None:
        m = normalize(mac)
        return next((e for e in self.pool if e.mac == m), None)

    def add_mac(self, mac: str, label: str = "", fixed: bool = False) -> bool:
        """新增；MAC 無效丟 ValueError，已存在回傳 False（不覆蓋）。"""
        m = check_assignable(mac)
        if self.find(m):
            return False
        self.pool.append(PoolEntry(m, label, fixed))
        return True

    def set_label(self, mac: str, label: str) -> bool:
        """改標籤；池內沒有這個 MAC 回傳 False。"""
        e = self.find(mac)
        if e is None:
            return False
        e.label = label.strip()
        return True

    @property
    def is_client(self) -> bool:
        """組員端：清單由伺服器管理，本機不可新增、刪除、改標籤、匯入匯出。"""
        return self.role == "client"

    def remove_mac(self, mac: str) -> bool:
        m = normalize(mac)
        n = len(self.pool)
        self.pool = [e for e in self.pool if e.mac != m]
        return len(self.pool) != n

    def validate(self) -> list[str]:
        """回傳問題清單（空 = 沒問題）。不丟例外，讓視窗能一次顯示全部問題。"""
        problems: list[str] = []
        seen: set[str] = set()
        for e in self.pool:
            try:
                check_assignable(e.mac)
            except ValueError as ex:
                problems.append(str(ex))
            if e.mac in seen:
                problems.append(f"MAC 重複：{e.mac}")
            seen.add(e.mac)
        if self.probe_low_mbps >= self.gate_mbps:
            problems.append("probe_low_mbps 應小於 gate_mbps，否則門檻互相矛盾")
        if self.confirm_strikes < 1:
            problems.append("confirm_strikes 至少為 1")
        if not self.probe_urls:
            problems.append("probe_urls 不可為空")
        if not 0 <= self.quota_reset_hour <= 23:
            problems.append("quota_reset_hour 需在 0 到 23")
        if self.role not in ("standalone", "hub", "client"):
            problems.append(f"role 只能是 standalone、hub 或 client，目前是 {self.role!r}")
        if self.role in ("hub", "client") and len(self.hub_token) < 16:
            problems.append("hub_token 空白或太短（至少 16 字元）：伺服器不會開放 API，組員端也連不上")
        if self.role == "client" and not self.hub_cert_sha256:
            problems.append("hub_cert_sha256 空白：組員端不知道要信任哪張伺服器憑證，不會連線（請向管理者索取組員設定檔）")
        return problems

    # ---- 序列化
    def to_dict(self) -> dict:
        d = asdict(self)
        d["flat_low_range"] = list(self.flat_low_range)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Settings":
        """未知欄位忽略（向前相容），缺少的欄位用預設值（向後相容）。"""
        known = {f.name for f in fields(cls)}
        kw = {k: v for k, v in d.items() if k in known and k != "pool"}
        if "flat_low_range" in kw:
            kw["flat_low_range"] = tuple(kw["flat_low_range"])
        s = cls(**kw)
        s.pool = [PoolEntry(**p) if isinstance(p, dict) else PoolEntry(p) for p in d.get("pool", [])]
        return s

    @classmethod
    def from_legacy(cls, d: dict) -> "Settings":
        """讀舊工具的 mac_settings.json（mac_list 是 MAC 字串陣列，speed_threshold 是單位 Mbps 的門檻）。"""
        s = cls(auto_mode=bool(d.get("is_auto_mode", False)))
        for m in d.get("mac_list", []):
            s.add_mac(m)
        return s


def load(path: str | Path | None = None) -> Settings:
    """讀設定檔；檔案不存在回傳預設值（第一次執行）；檔案壞掉丟例外並指出位置，不默默吞掉。"""
    p = Path(path) if path else default_config_path()
    if not p.exists():
        side = _sidecar()
        if side:                                   # 第一次執行：沿用伺服器管理者給的組員設定檔，免手動設定
            raw = json.loads(side.read_text(encoding="utf-8"))
            # 安全：組員設定檔只能帶連線資訊。別人遞來的檔案若夾帶 hooks、probe_urls 等欄位，一律忽略，否則等於遠端執行命令
            s = Settings.from_dict({k: v for k, v in raw.items() if k in CLIENT_FILE_KEYS})
            save(s, p)
            return s
        return Settings()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"設定檔格式錯誤：{p}（{e}）。可刪除該檔重來，或用 --config 指定別的檔案") from e
    if "mac_list" in raw and "pool" not in raw:  # 舊版格式自動轉換
        return Settings.from_legacy(raw)
    return Settings.from_dict(raw)


def _sidecar() -> Path | None:
    """程式旁邊（打包成 exe 時是 exe 所在資料夾，否則是目前資料夾）的 automac-client.json。"""
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    f = base / "automac-client.json"
    return f if f.is_file() else None


CLIENT_FILE_KEYS = ("role", "hub_url", "hub_port", "hub_token", "hub_cert_sha256", "use_presence")   # 組員設定檔允許的欄位（白名單）


def client_file_dict(s: Settings) -> dict:
    """給組員的設定檔內容：只含連線資訊，不含 MAC 清單（清單連上伺服器才同步）。"""
    return {"role": "client", "hub_url": s.hub_url, "hub_port": s.hub_port, "hub_token": s.hub_token,
            "hub_cert_sha256": s.hub_cert_sha256, "use_presence": s.use_presence}


def _atomic_write(p: Path, text: str) -> None:
    """先寫暫存檔再取代，避免寫到一半斷電留下壞檔。"""
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=p.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, p)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def save(settings: Settings, path: str | Path | None = None) -> Path:
    p = Path(path) if path else default_config_path()
    _atomic_write(p, json.dumps(settings.to_dict(), ensure_ascii=False, indent=2))
    return p


# --------------------------------------------------------------------------- 狀態
def next_quota_reset(now: float, hour: int) -> float:
    """下一個本地時間 hour:00 的 epoch 秒；被限速的 MAC 在此之前不再選它。"""
    lt = time.localtime(now)
    target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 0, 0, 0, 0, -1))
    return target if target > now else target + 86400


class StateStore:
    """程式自己記的狀態：哪些 MAC 被限速到何時、最近的切換時間。執行緒安全。"""

    def __init__(self, path: str | Path | None = None):
        # path 直接就是狀態檔路徑；不給就放在設定檔旁邊
        self.path = Path(path) if path else default_config_path().with_name("state.json")
        self._lock = threading.Lock()
        self.throttled_until: dict[str, float] = {}
        self.switches: list[float] = []
        self._load()

    def _load(self) -> None:
        try:
            d = json.loads(self.path.read_text(encoding="utf-8"))
            self.throttled_until = {normalize(k): float(v) for k, v in d.get("throttled_until", {}).items()}
            self.switches = [float(x) for x in d.get("switches", [])]
        except (OSError, ValueError):
            pass  # 沒有或壞掉就當空狀態，這是可再生的資料

    def _save(self) -> None:
        _atomic_write(self.path, json.dumps({"throttled_until": self.throttled_until, "switches": self.switches[-50:]},
                                            indent=1))

    def mark_throttled(self, mac: str, until: float) -> None:
        with self._lock:
            self.throttled_until[normalize(mac)] = until
            self._save()

    def clear_throttled(self, mac: str) -> None:
        with self._lock:
            self.throttled_until.pop(normalize(mac), None)
            self._save()

    def throttled_until_of(self, mac: str, now: float) -> float | None:
        """回傳限速到期時間；沒被限速或已過期回傳 None。"""
        with self._lock:
            t = self.throttled_until.get(normalize(mac))
            return t if t and t > now else None

    def record_switch(self, now: float) -> None:
        with self._lock:
            self.switches.append(now)
            self._save()

    def switches_within(self, now: float, seconds: float) -> int:
        with self._lock:
            return sum(1 for t in self.switches if now - t <= seconds)

    def last_switch(self) -> float | None:
        with self._lock:
            return self.switches[-1] if self.switches else None
