"""設備標籤：標籤跟著「設備」走，不是跟著 MAC 走。

為什麼（2026-11-24 使用者實測回報）：同一台電腦會在池內的 MAC 之間切換，原本標籤綁在 MAC 上，
電腦換到別的 MAC 後，標籤還留在原本那個（現在沒人用的）MAC 上，看起來像「那個 MAC 還是這台電腦的」。
現在：伺服器看得到每個 MAC 目前被哪台電腦持有（租約），就把「該電腦的標籤」顯示在那個 MAC 上；
電腦沒有自訂標籤時顯示電腦名稱。MAC 本身的標籤只保留給「固定占用」的（Pi 這類不跑本工具、永遠用同一個 MAC 的裝置）。

存檔：devices.json（設定檔旁邊），{電腦名稱: 標籤}，原子寫入；檔案被別的程序（命令列）改過會自動重讀。
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from .config import _atomic_write


class DeviceLabels:
    def __init__(self, path: str | Path | None):
        self.path = Path(path) if path else None
        self._lock = threading.Lock()
        self._d: dict[str, str] = {}
        self._mtime = -1.0
        self._load()

    def _load(self) -> None:
        if not self.path:
            return
        try:
            self._mtime = self.path.stat().st_mtime
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self._d = {str(k): str(v) for k, v in raw.items() if str(v).strip()}
        except (OSError, ValueError, AttributeError):
            self._d = {}                                   # 沒有或壞掉：沒有任何自訂標籤（可再生）

    def _fresh(self) -> None:
        """檔案被別的程序改過就重讀（例如命令列 automac device label）。"""
        if not self.path:
            return
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if m != self._mtime:
            self._load()

    def get(self, client_id: str) -> str:
        with self._lock:
            self._fresh()
            return self._d.get(client_id, "")

    def set(self, client_id: str, label: str) -> None:
        """設定電腦的標籤；空字串 = 清除（改顯示電腦名稱）。"""
        label = label.strip()
        with self._lock:
            self._fresh()
            if label:
                self._d[client_id] = label
            else:
                self._d.pop(client_id, None)
            if self.path:
                _atomic_write(self.path, json.dumps(self._d, ensure_ascii=False, indent=1))
                self._mtime = self.path.stat().st_mtime

    def all(self) -> dict[str, str]:
        with self._lock:
            self._fresh()
            return dict(self._d)


__all__ = ["DeviceLabels"]
