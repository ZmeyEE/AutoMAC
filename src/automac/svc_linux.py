"""Linux 組員端背景服務的安裝與移除（systemd 單元）。Windows 版見 svcinstall.py。

為什麼這樣設計（2026-11-24 使用者要求 Linux 客戶端，不需要視窗）
    - 服務是 systemd 單元（開機啟動、root 身分、失敗 10 秒後重啟）。Linux 沒有系統匣，狀態用 `automac live` 或
      `journalctl -u automac-client` 看；所以這裡不需要複製執行檔、不需要排程工作，也沒有「使用者工作階段」的問題。
    - 換 MAC 與清空鄰居表需要 CAP_NET_ADMIN；單元把能力限制在 CAP_NET_ADMIN 與 CAP_NET_RAW（ICMP/ARP），
      其他沙盒選項（NoNewPrivileges、PrivateTmp、ProtectHome、ProtectSystem=full）降低被攻破後的影響。
    - 設定、token、狀態都放在 /var/lib/automac（權限 0700）；單元用環境變數 AUTOMAC_CONFIG 指到它，
      root 手動執行 `automac status` 時 config.config_dir() 偵測到這個資料夾，會讀同一份。
    - install / uninstall 的外部指令（systemctl）與目標資料夾都可以注入，測試不需要真的 root，也不會動到系統。

shortcut: 只支援 systemd（Debian、Ubuntu、Raspberry Pi OS、Fedora 都是）；用 OpenRC 或 runit 的發行版要自己寫服務檔，
  單元內容可從 unit_text() 的輸出改寫。
shortcut: 沒有開機後等網路就緒的精細處理：單元只設 After=network-online.target；網卡還沒好時 cli._run 會每 30 秒重試。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

UNIT = "automac-client.service"
UNIT_DIR = "/etc/systemd/system"
_BAD = set('"\n\r%$\\')          # 單元檔的特殊字元：引號、換行、% 與 $（systemd 會展開）、反斜線；路徑含這些就拒絕，避免注入設定


def _safe(value: str, what: str) -> str:
    if not value or any(c in _BAD for c in value):
        raise RuntimeError(f"{what}含有不能放進 systemd 單元的字元：{value!r}")
    return value


def unit_text(python: str, config_path: Path, pythonpath: str = "") -> str:
    """產生單元檔內容（純函式，方便測試與人工檢查）。
    python：執行服務的直譯器（通常是 /opt/automac/venv/bin/python）；pythonpath：automac 套件所在的資料夾，
    直譯器本身已經找得到套件時（pip install 過）可留空。"""
    env = [f'"AUTOMAC_CONFIG={_safe(Path(config_path).as_posix(), "設定檔路徑")}"', '"PYTHONUNBUFFERED=1"']
    if pythonpath:
        env.append(f'"PYTHONPATH={_safe(pythonpath, "套件路徑")}"')
    return f"""[Unit]
Description=AutoMAC client (campus MAC auto-switch)
Documentation=https://github.com/ZmeyEE/AutoMAC
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment={' '.join(env)}
ExecStart={_safe(python, "直譯器路徑")} -m automac run
Restart=on-failure
RestartSec=10
Nice=10
# 權限最小化：只留換 MAC 與送 ARP 所需的能力
NoNewPrivileges=yes
CapabilityBoundingSet=CAP_NET_ADMIN CAP_NET_RAW
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX AF_NETLINK AF_PACKET

[Install]
WantedBy=multi-user.target
"""


def _systemctl(run, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cp = run(["systemctl", *args], capture_output=True, text=True, timeout=60)
    if check and cp.returncode != 0:
        raise RuntimeError(f"systemctl {' '.join(args)} 失敗：{(cp.stderr or cp.stdout or '').strip()[:300]}")
    return cp


def install(config_path: Path, python: str | None = None, unit_dir: str = UNIT_DIR, run=None,
            start: bool = True, is_root: bool | None = None, pythonpath: str | None = None) -> Path:
    """安裝（或升級）並啟動服務。回傳單元檔路徑。任何一步失敗丟 RuntimeError（訊息可直接給使用者看）。
    重複執行安全：單元檔直接覆寫、systemctl enable 本來就是冪等的。
    pythonpath：套件所在資料夾，寫進單元的 PYTHONPATH。不給就用本檔案往上兩層推算。
    為什麼要能指定：`python -m automac` 會優先載入「目前資料夾」裡的 automac，在解壓縮的資料夾裡執行時，
    推算出來的是那個暫存資料夾，資料夾一刪或根本沒搬進去，服務就 No module named automac（2026-11-24 在真實 systemd 上踩到）。"""
    run = run or subprocess.run
    root = is_root if is_root is not None else (getattr(os, "geteuid", lambda: -1)() == 0)
    if not root:
        raise RuntimeError("安裝背景服務需要 root：請用 sudo 執行")
    config_path = Path(config_path)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(config_path.parent, 0o700)           # 內含 token 與憑證指紋
    except OSError:
        pass
    pkg_root = pythonpath or Path(__file__).resolve().parent.parent.as_posix()
    py = python or sys.executable
    unit = Path(unit_dir) / UNIT
    unit.parent.mkdir(parents=True, exist_ok=True)
    unit.write_text(unit_text(py, config_path, pkg_root), encoding="utf-8")
    os.chmod(unit, 0o644)
    _systemctl(run, "daemon-reload")
    _systemctl(run, "enable", UNIT)
    if start:
        _systemctl(run, "restart", UNIT)               # restart：已在跑的舊版也會換成新版
    return unit


def uninstall(config_path: Path, purge: bool = False, unit_dir: str = UNIT_DIR, run=None) -> None:
    """停止並移除服務。purge=True 連 /var/lib/automac 一起刪；只在資料夾確實是系統資料夾時才整個刪，避免誤刪使用者的目錄。
    本機設定刪掉不會遺失紀錄：MAC 清單、標籤與用量都在伺服器上。"""
    run = run or subprocess.run
    _systemctl(run, "disable", "--now", UNIT, check=False)
    try:
        (Path(unit_dir) / UNIT).unlink()
    except OSError:
        pass
    _systemctl(run, "daemon-reload", check=False)
    if purge:
        d = Path(config_path).parent
        try:
            same = d.resolve() == config.LINUX_SYSTEM_DIR.resolve()
        except OSError:
            same = False
        if same:
            shutil.rmtree(d, ignore_errors=True)


__all__ = ["install", "uninstall", "unit_text", "UNIT"]
