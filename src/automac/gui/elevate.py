"""以系統管理員身分重新啟動自己（換 MAC 需要）。會跳出 Windows 的 UAC 確認，由使用者按「是」。"""
from __future__ import annotations

import ctypes
import subprocess
import sys


def relaunch_as_admin(extra_args: str = "") -> bool:
    """成功送出要求回傳 True（呼叫者接著應關閉目前視窗）；使用者在 UAC 按「否」回傳 False。"""
    if getattr(sys, "frozen", False):                       # PyInstaller 打包後：直接重啟 exe
        exe, params = sys.executable, extra_args
    else:
        exe, params = sys.executable, subprocess.list2cmdline(["-m", "automac", "gui"]) + (" " + extra_args if extra_args else "")
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)  # type: ignore[attr-defined]
    return rc > 32
