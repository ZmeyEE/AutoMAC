"""切換前後的外掛命令。

用途舉例：伺服器換 MAC 前先暫停會寫入共用資料夾的服務、換完後通知別的裝置、記錄到外部系統。
命令可從環境變數取得 AUTOMAC_OLD_MAC、AUTOMAC_NEW_MAC、AUTOMAC_NEW_IP（切換前沒有 NEW_IP）。

安全：命令由使用者自己寫在自己的設定檔裡，等同使用者手動執行；失敗只記錄，不中斷切換
（切換前命令失敗也不擋，因為「斷線被限速」比「某個輔助命令沒跑」更糟，使用者可從事件看到失敗）。
"""
from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Sequence


@dataclass(frozen=True)
class HookResult:
    command: str
    returncode: int
    output: str


def run_hooks(commands: Sequence[str], env_extra: dict[str, str], timeout: float = 30.0,
              runner: Callable = subprocess.run) -> list[HookResult]:
    env = {**os.environ, **env_extra}
    flags = 0x08000000 if sys.platform == "win32" else 0
    out: list[HookResult] = []
    for cmd in commands:
        try:
            cp = runner(cmd, shell=True, env=env, capture_output=True, text=True, timeout=timeout,
                        encoding="utf-8", errors="replace", creationflags=flags)
            out.append(HookResult(cmd, cp.returncode, (cp.stdout + cp.stderr).strip()[:300]))
        except subprocess.TimeoutExpired:
            out.append(HookResult(cmd, -1, f"逾時（{timeout:.0f} 秒）"))
        except OSError as e:
            out.append(HookResult(cmd, -2, str(e)))
    return out
