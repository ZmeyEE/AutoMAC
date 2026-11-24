"""傳輸加密工具：TLS 1.3、自簽憑證、憑證釘選。只用標準函式庫 `ssl`（背後是 OpenSSL），不增加相依套件。

威脅模型：校園網路上其他人（同網段竊聽、假冒伺服器、冒充組員、暴力猜 token）。
    竊聽 → TLS 1.3 加密：金鑰交換用臨時 ECDHE（前向保密，將來伺服器金鑰外洩也解不開舊流量），
            資料用 AEAD（AES-256-GCM 或 ChaCha20-Poly1305，由 OpenSSL 協商）。只允許 TLS 1.3，不退讓到舊版。
    假冒伺服器 → 憑證釘選：組員設定檔記下伺服器憑證的 SHA-256 指紋，握手後、送出任何資料（含 token）之前先比對，
            不符就中止。因此即使有人用 mDNS 假冒伺服器、或在中間攔截，也拿不到 token。
    冒充組員、猜 token → token 是 256 位元隨機值（無法暴力猜），在 TLS 內傳送；伺服器端另有失敗次數限制（hub.py）。

為什麼不用公開 CA 或本機根憑證：封閉的小群體，沒有網域名稱可申請憑證；裝本機根憑證要改每台電腦的信任設定。
自簽憑證加釘選是封閉群體的常見做法（Sivakumaran 等，IEEE Commun. Surveys & Tutorials 2019；見 research/參考文獻.md）。
為什麼不用雙向 TLS（用戶端憑證）：管理每台電腦的憑證麻煩且容易出錯（Parsovs，NDSS 2014），改用 TLS 內的 token。

憑證用 openssl 命令產生（Git for Windows 內建，伺服器與開發機都已有），不另外安裝。
shortcut: 憑證有效 10 年、沒有撤銷機制，換憑證要重新發給每位組員新的設定檔；組員多到不好管理時，改成自建小型 CA 簽發。
"""
from __future__ import annotations

import hashlib
import http.client
import os
import shutil
import socket
import ssl
import subprocess
from pathlib import Path

_GIT_OPENSSL = [r"C:\Program Files\Git\usr\bin\openssl.exe", r"C:\Program Files\Git\mingw64\bin\openssl.exe",
                r"C:\Program Files (x86)\Git\usr\bin\openssl.exe"]


class TlsError(RuntimeError):
    """憑證產生、載入或釘選失敗（訊息可直接給使用者看）。"""


class PinMismatch(TlsError):
    """對方憑證的指紋與設定的不同：可能是伺服器換了憑證，也可能有人冒充。"""


def find_openssl() -> str | None:
    """PATH 上的 openssl，或 Git for Windows 內建的。找不到回傳 None。"""
    return shutil.which("openssl") or next((p for p in _GIT_OPENSSL if os.path.isfile(p)), None)


def generate_cert(cert_path: Path, key_path: Path, names: list[str], days: int = 3650) -> str:
    """產生自簽憑證（ECDSA P-384、SHA-384）與私鑰，回傳憑證指紋（SHA-256，小寫十六進位）。
    names 是伺服器的電腦名稱與 IP（寫進 SAN）。私鑰存檔後限制只有系統與管理員可讀（盡力而為）。
    已存在就不覆蓋（換憑證要刪檔重來，因為換了就要重新發設定檔給所有組員）。"""
    if cert_path.exists() and key_path.exists():
        return fingerprint(cert_path)
    exe = find_openssl()
    if not exe:
        raise TlsError("找不到 openssl（需要 Git for Windows 或其他 openssl）：請安裝後重試，或自行準備 hub-cert.pem 與 hub-key.pem")
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    san = ",".join(f"IP:{n}" if n.replace(".", "").isdigit() else f"DNS:{n}" for n in names) or "DNS:automac-hub"
    cmd = [exe, "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:secp384r1", "-sha384", "-nodes",
           "-keyout", str(key_path), "-out", str(cert_path), "-days", str(days), "-subj", "/CN=AutoMAC Hub",
           "-addext", f"subjectAltName={san}"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0 or not cert_path.exists() or not key_path.exists():
        raise TlsError(f"openssl 產生憑證失敗：{(r.stderr or r.stdout).strip()[:200]}")
    _restrict_key(key_path)
    return fingerprint(cert_path)


def _restrict_key(key_path: Path) -> None:
    """私鑰只留給目前使用者、系統與管理員（移除繼承的其他讀取權限）。失敗不影響使用，只是少一層保護。"""
    if os.name != "nt":
        os.chmod(key_path, 0o600)
        return
    try:
        user = os.environ.get("USERNAME", "")
        grants = ["*S-1-5-18:F", "*S-1-5-32-544:F"] + ([f"{user}:F"] if user else [])
        args = ["icacls", str(key_path), "/inheritance:r"] + [a for g in grants for a in ("/grant:r", g)]
        subprocess.run(args, capture_output=True, timeout=20)
    except Exception:
        pass


def fingerprint(cert_path: Path) -> str:
    """憑證（DER）的 SHA-256，小寫十六進位，無冒號。"""
    der = ssl.PEM_cert_to_DER_cert(Path(cert_path).read_text(encoding="ascii"))
    return hashlib.sha256(der).hexdigest()


def normalize_pin(pin: str) -> str:
    """接受 'AA:BB:..'、'aabb..'、有空白等寫法，統一成小寫無冒號；長度不是 64 個十六進位字元就丟 ValueError。"""
    p = pin.replace(":", "").replace(" ", "").lower()
    if len(p) != 64 or any(c not in "0123456789abcdef" for c in p):
        raise ValueError("憑證指紋需為 SHA-256（64 個十六進位字元）")
    return p


def server_context(cert_path: Path, key_path: Path) -> ssl.SSLContext:
    """伺服器端：只允許 TLS 1.3。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    try:
        ctx.load_cert_chain(str(cert_path), str(key_path))
    except (OSError, ssl.SSLError) as e:
        raise TlsError(f"無法載入憑證或私鑰（{cert_path}）：{e}") from e
    return ctx


def client_context() -> ssl.SSLContext:
    """組員端：只允許 TLS 1.3。不驗證 CA 與主機名稱（自簽憑證沒有 CA），改在 connect_pinned 用指紋驗證。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_3
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def connect_pinned(host: str, port: int, pin: str, timeout: float) -> http.client.HTTPSConnection:
    """建立 TLS 連線並先比對憑證指紋；符合才回傳連線（之後才送 HTTP 請求與 token）。
    不符合立刻關閉並丟 PinMismatch，沒有任何應用資料被送出。

    只解析 IPv4：伺服器只聽 IPv4，但 .local 名稱（mDNS）同時有 IPv6 的連結本地位址，標準做法會先試 IPv6，
    封包被丟掉就等到整個逾時（實測每個請求平白多 5 到 21 秒，視窗「切換到所選」因此卡約 30 秒）。
    連線階段另用較短的逾時（最多 4 秒），連上之後才改回完整逾時給請求用（分配請求要等伺服器掃描）。"""
    want = normalize_pin(pin)
    try:
        ip = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
    except (socket.gaierror, IndexError) as e:
        raise OSError(f"找不到主機 {host}：{e}") from e
    conn = http.client.HTTPSConnection(ip, port, timeout=min(timeout, 4.0), context=client_context())
    try:
        conn.connect()
        conn.sock.settimeout(timeout)                                # type: ignore[union-attr]
        der = conn.sock.getpeercert(binary_form=True)                # type: ignore[union-attr]
        got = hashlib.sha256(der).hexdigest() if der else ""
        if got != want:
            raise PinMismatch(f"伺服器憑證指紋不符（收到 {got[:12]}…，預期 {want[:12]}…）：可能伺服器換了憑證，"
                              "請向管理者索取新的設定檔；也可能有人冒充伺服器")
    except BaseException:
        conn.close()
        raise
    return conn


__all__ = ["TlsError", "PinMismatch", "find_openssl", "generate_cert", "fingerprint", "normalize_pin",
           "server_context", "client_context", "connect_pinned"]
