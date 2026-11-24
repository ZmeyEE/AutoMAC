"""接手背景服務：伺服器上已有一個背景的 AutoMAC（排程工作）在跑，使用者開視窗時，視窗自己也會建一份服務，
兩份同時跑會搶同一個 mDNS 名稱與 API 埠。所以開視窗前先停掉背景那份，關視窗後再叫排程工作重新啟動。

為什麼不能只用 `schtasks /end`：它只結束排程工作的外層命令列，底下的 python／exe 會變孤兒繼續跑（2026-11-24 實測踩過），
所以要連同命令列符合的程序一起結束（exe 只結束帶 run 參數的背景服務，視窗程式不動）。自己（與 PyInstaller 單檔 exe 的外層啟動器，名稱相同）要排除，不然會把自己殺掉。
需要系統管理員（背景服務是 SYSTEM 身分）。
"""
from __future__ import annotations

import os
import subprocess

TASK = "AutoMAC"
_FIND = ("Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*' -and $_.CommandLine -match '-m automac')"
         " -or ($_.Name -eq 'automac.exe' -and $_.CommandLine -match ' run( |$)') } | ForEach-Object { $_.ProcessId }")


def stop_background(task: str = TASK) -> list[int]:
    """停掉排程工作與殘留的背景程序，回傳被結束的 PID。找不到排程工作也不算錯（可能沒安裝背景服務）。"""
    subprocess.run(["schtasks", "/end", "/tn", task], capture_output=True)
    me = {os.getpid(), os.getppid()}
    out = subprocess.run(["powershell", "-NoProfile", "-Command", _FIND], capture_output=True, text=True).stdout
    killed = []
    for line in out.split():
        if line.isdigit() and int(line) not in me:
            subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True)
            killed.append(int(line))
    return killed


_FIND_TRAY = ("Get-CimInstance Win32_Process | Where-Object { $_.Name -eq 'automac.exe' -and $_.CommandLine -match ' tray( |$)' }"
              " | ForEach-Object { $_.ProcessId }")


def stop_tray(task: str = "AutoMAC Tray") -> list[int]:
    """停掉系統匣圖示程序（升級或移除前需要，因為執行中的 exe 不能被覆蓋或刪除）。"""
    subprocess.run(["schtasks", "/end", "/tn", task], capture_output=True)
    me = {os.getpid(), os.getppid()}
    out = subprocess.run(["powershell", "-NoProfile", "-Command", _FIND_TRAY], capture_output=True, text=True).stdout
    killed = []
    for line in out.split():
        if line.isdigit() and int(line) not in me:
            subprocess.run(["taskkill", "/F", "/PID", line], capture_output=True)
            killed.append(int(line))
    return killed


def restart_background(task: str = TASK) -> None:
    """叫排程工作重新啟動背景服務。沒安裝就什麼都不做。"""
    subprocess.run(["schtasks", "/run", "/tn", task], capture_output=True)


__all__ = ["stop_background", "stop_tray", "restart_background"]
