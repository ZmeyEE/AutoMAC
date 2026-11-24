r"""組員端背景服務的安裝、升級與移除（Windows 排程工作）。

為什麼這樣設計（2026-11-24 使用者要求：客戶端要能背景執行，不小心關掉視窗不能停）
    - 服務是「開機自動啟動、SYSTEM 身分」的排程工作，不需要有人登入，也不依賴任何視窗。
    - 安裝時把 exe 複製到設定資料夾（C:\ProgramData\AutoMAC\automac.exe）再從那裡執行：
      組員解壓縮的資料夾之後可以刪掉、搬走，服務不受影響；新版 exe 雙擊時偵測到內容不同就自動升級（停舊、換檔、重啟）。
    - 排程工作直接執行 exe（不經 cmd /c）：視窗程式經 cmd 啟動時 cmd 會立刻結束，排程工作顯示「就緒」而 exe 變孤兒，
      當掉也不會被重啟（2026-11-24 在伺服器上踩過）。失敗自動重試 5 次，不設執行時間上限。
    - 設定資料夾限制為系統管理員與 SYSTEM 可讀寫（內含 token 與憑證指紋）。
    - 防火牆：為安裝後的 exe 加一條 UDP 5353（mDNS）規則，限本機子網；找伺服器主要靠 .local 名稱（系統內建解析，
      不受程式防火牆規則影響），這條規則是備援用的區網宣告。
需要系統管理員權限（exe 的 manifest 已要求）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from .winshell import ps_quote, run_ps

TASK = "AutoMAC Client"
TRAY_TASK = "AutoMAC Tray"                 # 登入時啟動系統匣圖示（使用者工作階段；背景服務在 SYSTEM 工作階段，沒有桌面可放圖示）
FW_RULE = "AutoMAC client mDNS (installed)"


def installed_exe(config_dir: Path) -> Path:
    return config_dir / "automac.exe"


def _hash(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def same_file(a: Path, b: Path) -> bool:
    """兩個檔案內容相同（先比大小，再比雜湊）。"""
    try:
        return a.stat().st_size == b.stat().st_size and _hash(a) == _hash(b)
    except OSError:
        return False


def task_exists(task: str = TASK) -> bool:
    return subprocess.run(["schtasks", "/query", "/tn", task], capture_output=True,
                          creationflags=0x08000000).returncode == 0


def _stop_service() -> None:
    """停掉服務：結束排程工作與所有「automac.exe run」程序（排除自己；視窗程式不受影響）。"""
    from .takeover import stop_background, stop_tray
    stop_background(TASK)
    stop_tray(TRAY_TASK)


def _restrict_dir(d: Path) -> None:
    """設定資料夾只留系統與管理員（含子檔案繼承）。失敗不影響功能，只是少一層保護。"""
    try:
        subprocess.run(["icacls", str(d), "/inheritance:r", "/grant:r", "*S-1-5-18:(OI)(CI)F", "/grant:r",
                        "*S-1-5-32-544:(OI)(CI)F"], capture_output=True, timeout=30, creationflags=0x08000000)
    except Exception:
        pass


def install(src_exe: Path, config_dir: Path, start: bool = True) -> Path:
    """安裝或升級服務：停舊、複製 exe、登記排程工作與防火牆規則、啟動。回傳安裝後的 exe 路徑。
    任何一步失敗丟 RuntimeError（訊息可直接給使用者看）。"""
    config_dir.mkdir(parents=True, exist_ok=True)
    _restrict_dir(config_dir)
    dst = installed_exe(config_dir)
    _stop_service()
    if src_exe.resolve() != dst.resolve():
        for i in range(10):                                   # 剛結束的程序可能還鎖著檔案，等一下再試
            try:
                shutil.copy2(src_exe, dst)
                break
            except PermissionError:
                if i == 9:
                    raise RuntimeError(f"無法更新 {dst}（檔案被占用）：請重新開機後再試")
                time.sleep(1)
    script = f"""
$a = New-ScheduledTaskAction -Execute {ps_quote(str(dst))} -Argument 'run' -WorkingDirectory {ps_quote(str(config_dir))}
$t = New-ScheduledTaskTrigger -AtStartup
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest -LogonType ServiceAccount
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName {ps_quote(TASK)} -Action $a -Trigger $t -Principal $p -Settings $s -Force | Out-Null
$u = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$ta = New-ScheduledTaskAction -Execute {ps_quote(str(dst))} -Argument 'tray' -WorkingDirectory {ps_quote(str(config_dir))}
$tt = New-ScheduledTaskTrigger -AtLogOn -User $u
$tp = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Highest
$ts = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName {ps_quote(TRAY_TASK)} -Action $ta -Trigger $tt -Principal $tp -Settings $ts -Force | Out-Null
Remove-NetFirewallRule -DisplayName {ps_quote(FW_RULE)} -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName {ps_quote(FW_RULE)} -Direction Inbound -Protocol UDP -LocalPort 5353 -Program {ps_quote(str(dst))} -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
"""
    from .winshell import PowerShellError
    try:
        run_ps(script, timeout=60, check=True)
    except PowerShellError as e:
        raise RuntimeError(f"登記背景服務失敗：{e}") from e
    if start:
        start_service()
        start_tray()
    return dst


def start_service() -> None:
    subprocess.run(["schtasks", "/run", "/tn", TASK], capture_output=True, creationflags=0x08000000)


def start_tray() -> None:
    """叫排程工作在使用者工作階段啟動系統匣圖示（已經在跑就不重複啟動）。"""
    from .tray import tray_running
    if not tray_running():
        subprocess.run(["schtasks", "/run", "/tn", TRAY_TASK], capture_output=True, creationflags=0x08000000)


def _kill_installed_processes(config_dir: Path) -> None:
    """結束所有「從安裝位置的 exe 執行」的程序（系統匣、從系統匣開的視窗），排除自己與外層啟動器；
    不然檔案被鎖住刪不掉。從別處（例如解壓縮的資料夾）執行的視窗不受影響。"""
    try:
        import psutil
        dst = str(installed_exe(config_dir)).lower()
        me = {os.getpid(), os.getppid()}
        for p in psutil.process_iter(["pid", "exe"]):
            try:
                if p.info["pid"] not in me and (p.info["exe"] or "").lower() == dst:
                    p.kill()
            except psutil.Error:
                pass
    except Exception:
        pass


def uninstall(config_dir: Path, purge: bool = False) -> None:
    """移除：停止服務與系統匣、刪排程工作與防火牆規則、刪掉安裝的 exe。
    purge=True 連設定資料夾（ProgramData 底下的 AutoMAC）一起刪：呼叫者自己可能就是從那裡執行的 exe，
    所以交給一個獨立的 cmd 在幾秒後刪（等呼叫者結束）；只在資料夾確實是 ProgramData 底下的 AutoMAC 時才整個刪，避免誤刪使用者的資料夾。
    預設 purge=False（保留設定與紀錄，讓命令列使用者之後可重新安裝）；視窗與系統匣的「解除安裝」用 purge=True。
    MAC 清單、標籤與用量都在伺服器上，所以刪掉本機資料不會遺失任何紀錄。"""
    _stop_service()
    run_ps(f"Unregister-ScheduledTask -TaskName {ps_quote(TASK)} -Confirm:$false -ErrorAction SilentlyContinue; "
           f"Unregister-ScheduledTask -TaskName {ps_quote(TRAY_TASK)} -Confirm:$false -ErrorAction SilentlyContinue; "
           f"Remove-NetFirewallRule -DisplayName {ps_quote(FW_RULE)} -ErrorAction SilentlyContinue", timeout=60)
    _kill_installed_processes(config_dir)
    try:
        installed_exe(config_dir).unlink()
    except OSError:
        pass
    if purge:
        pd = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "AutoMAC"
        try:
            is_pd = config_dir.resolve() == pd.resolve()
        except OSError:
            is_pd = False
        if is_pd:
            subprocess.Popen(["cmd", "/c", f'ping -n 4 127.0.0.1 >nul & rmdir /s /q "{config_dir}"'],
                             creationflags=0x08000000 | 0x00000008, close_fds=True)


def ensure_service(running_exe: Path, config_dir: Path) -> bool:
    """雙擊 exe 時呼叫：沒裝就裝、版本不同就升級、沒在跑就啟動。回傳是否做了安裝或升級。"""
    dst = installed_exe(config_dir)
    if not dst.exists() or not same_file(running_exe, dst) or not task_exists() or not task_exists(TRAY_TASK):
        install(running_exe, config_dir)
        return True
    start_tray()                                   # 圖示被誤關了：雙擊 exe 時順便叫回來
    return False


__all__ = ["install", "uninstall", "ensure_service", "start_service", "start_tray", "task_exists", "same_file",
           "installed_exe", "TASK", "TRAY_TASK"]
