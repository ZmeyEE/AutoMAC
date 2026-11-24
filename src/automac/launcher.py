"""組員雙擊 exe 的流程：確保背景服務已安裝並在跑，然後開視窗當檢視器。

    雙擊 → 讀設定（第一次會從旁邊的 automac-client.json 加入伺服器）→ 沒裝服務就安裝、版本不同就升級、沒在跑就啟動
         → 開視窗連本機 API。關掉視窗不影響服務。
任何一步失敗都不讓使用者卡住：跳出說明並退回「視窗內嵌服務」的模式（和以前一樣，但關視窗就停）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config
from .localapi import LocalClient, TOKEN_FILE
from .winshell import is_admin


def _message(title: str, text: str) -> None:
    try:
        import tkinter
        from tkinter import messagebox
        r = tkinter.Tk()
        r.withdraw()
        messagebox.showwarning(title, text)
        r.destroy()
    except Exception:
        pass


def wait_for_service(cdir: Path, port: int, timeout: float = 25.0) -> LocalClient | None:
    """等服務的 token 檔出現且 API 回應；成功回傳連好的 LocalClient，逾時回傳 None。"""
    end = time.time() + timeout
    while time.time() < end:
        try:
            tok = (cdir / TOKEN_FILE).read_text(encoding="ascii").strip()
        except OSError:
            tok = ""
        if tok:
            c = LocalClient(tok, port)
            if c.refresh():
                return c
            c.stop()
        time.sleep(1.0)
    return None


def launch_gui(config_path: str | None = None) -> int:
    from .gui.app import App, build_app
    path = Path(config_path) if config_path else config.default_config_path()
    s = config.load(path)
    frozen = getattr(sys, "frozen", False)
    if s.role != "client" or not s.local_api or not frozen:
        return _embedded(config_path)                       # 單機、伺服器、開發環境：視窗內嵌服務
    from .tray import bring_to_front, find_main_window
    hwnd = find_main_window()
    if hwnd:                                                # 視窗已經開著（再次雙擊 exe）：帶到最前面，不再開第二個
        bring_to_front(hwnd)
        return 0
    if not is_admin():
        _message("需要系統管理員", "AutoMAC 需要系統管理員權限才能安裝背景服務與換 MAC。\n請在 automac.exe 上按右鍵，選「以系統管理員身分執行」。")
        return _embedded(config_path)
    cdir = path.parent
    try:
        from . import svcinstall
        svcinstall.ensure_service(Path(sys.executable), cdir)
    except Exception as e:
        _message("背景服務安裝失敗", f"{e}\n\n仍可開啟視窗使用，但關閉視窗後偵測會停止。")
        return _embedded(config_path)
    client = wait_for_service(cdir, s.local_port)
    if client is None:
        _message("背景服務沒有回應", "背景服務沒有在 25 秒內啟動。仍可開啟視窗使用，但關閉視窗後偵測會停止。\n"
                 f"詳細紀錄在 {cdir / 'automac.log'}")
        return _embedded(config_path)
    app = App(s, path, client, client.bus)
    app.mainloop()
    return 0


def _embedded(config_path: str | None) -> int:
    from .gui.app import build_app
    build_app(config_path).mainloop()
    return 0


__all__ = ["launch_gui", "wait_for_service"]
