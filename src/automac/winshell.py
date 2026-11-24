"""呼叫 PowerShell 的唯一入口（系統邊界）。想移植到 Linux，只要換掉 nic / scanner 對這裡的呼叫。

為什麼用 -EncodedCommand：腳本裡的引號、中文網卡名稱（例如「乙太網路」）經命令列傳遞容易被吃掉或亂碼，
先轉成 UTF-16LE 的 Base64 就不會被任何一層重新解析。
"""
from __future__ import annotations

import base64
import ctypes
import json
import subprocess
import sys

CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_PRELUDE = "[Console]::OutputEncoding=[Text.Encoding]::UTF8; $ProgressPreference='SilentlyContinue'; "


class PowerShellError(RuntimeError):
    pass


def ps_quote(s: str) -> str:
    """PowerShell 單引號字串：內部單引號重複一次。所有外部輸入（網卡名稱、MAC）都要經過這裡。"""
    return "'" + str(s).replace("'", "''") + "'"


def run_ps(script: str, timeout: float = 30.0, check: bool = False) -> subprocess.CompletedProcess:
    enc = base64.b64encode((_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    cp = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", enc],
                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                        timeout=timeout, creationflags=CREATE_NO_WINDOW)
    if check and cp.returncode != 0:
        raise PowerShellError((cp.stderr or cp.stdout or f"結束碼 {cp.returncode}").strip()[:500])
    return cp


def ps_json(script: str, timeout: float = 30.0) -> list[dict]:
    """執行會輸出物件的腳本，回傳字典串列（單一物件也包成串列；沒輸出回傳空串列）。"""
    cp = run_ps(f"@({script}) | ConvertTo-Json -Compress -Depth 4", timeout)
    out = cp.stdout.strip()
    if cp.returncode != 0 or not out:
        return []
    data = json.loads(out)
    return data if isinstance(data, list) else [data]


def acquire_single_instance(name: str):
    """搶一個具名互斥鎖，確保同一件事只有一個程序在做（背景服務不能同時跑兩份：兩份會各自偵測、換 MAC、搶 mDNS 名稱）。
    成功回傳控制代碼（呼叫者留著直到程序結束；程序結束時系統自動釋放），已經有人持有回傳 None。非 Windows 一律成功。
    用 Global\\ 前綴：背景服務在 SYSTEM 工作階段，使用者在自己的工作階段手動執行時也要看得到它。"""
    if sys.platform != "win32":
        return True
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = ctypes.c_void_p
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    ctypes.set_last_error(0)
    h = k.CreateMutexW(None, False, name)
    err = ctypes.get_last_error()
    # 183 ERROR_ALREADY_EXISTS：同身分的程序已持有。5 ERROR_ACCESS_DENIED：別的身分（背景服務是 SYSTEM）建的鎖，
    # 我們沒權限開它，同樣代表「已經有人持有」。實測：只處理 183 時，手動再執行一次 run 會被當成沒人持有，多出第二份服務。
    if err == 183 or (not h and err == 5):
        if h:
            k.CloseHandle.argtypes = [ctypes.c_void_p]
            k.CloseHandle(h)
        return None
    return h or True


def release_single_instance(handle) -> None:
    if sys.platform == "win32" and isinstance(handle, int) and handle:
        k = ctypes.WinDLL("kernel32")
        k.CloseHandle.argtypes = [ctypes.c_void_p]
        k.CloseHandle(handle)


def is_admin() -> bool:
    """是否以系統管理員執行。換 MAC 需要；非 Windows 一律 False。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        return False
