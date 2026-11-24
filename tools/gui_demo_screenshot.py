"""用假資料（RFC 7042 文件保留 MAC、RFC 5737 文件保留 IP）開啟視窗並擷取畫面，產生文件用的示範截圖。

為什麼：文件與 README 的截圖不能用真實裝置的畫面（會露出 MAC、IP、電腦名稱）。這支程式不連任何網卡或伺服器，
用一個假的服務物件餵資料給視窗，所以截圖可以安全公開，也能重複產生。
用法：python tools/gui_demo_screenshot.py            輸出 docs/gui_client.png（組員端）與 docs/gui_main.png（伺服器／單機版面）
需要 Windows 桌面環境（用 PowerShell 的 CopyFromScreen 擷取視窗範圍）。
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from automac.config import Settings  # noqa: E402
from automac.events import EventBus  # noqa: E402
from automac.gui.app import App  # noqa: E402
from automac.policy import MacStatus  # noqa: E402
from automac.service import Status  # noqa: E402

GB = 10 ** 9


class DemoService:
    """假服務：只提供視窗會呼叫的那幾個方法。所有數字都是示範用。"""

    def __init__(self):
        self.bus = EventBus()
        self.hub = None
        self.switcher = object()
        self.bus.emit("info", "已在區網宣告：目前使用 00-00-5E-00-53-01")
        self.bus.emit("probe", "探針（idle_interval）：24.5 Mbps，其他流量 0.0，有效 → healthy")

    def status(self):
        return Status(adapter="乙太網路", mac="00005E005301", ip="192.0.2.50", rx=0.1, tx=0.0, quiet=True, state="監看中",
                      last_probe=SimpleNamespace(mbps=24.5), last_verdict=SimpleNamespace(kind="healthy"),
                      last_probe_ts=time.time(), auto_mode=True, admin=True, hub="已與伺服器同步（3 秒前）")

    def pool_statuses(self, max_age_s: float = 30.0, force: bool = False):
        tomorrow = time.time() + 6 * 3600
        return [MacStatus("00005E005301", "實驗室電腦", "current", "本機使用中", None, int(1.4 * GB), 38 * 10 ** 6, "LAB-PC-01"),
                MacStatus("00005E005302", "", "free", "", None, 0, 0, ""),
                MacStatus("00005E005303", "", "free", "", None, 0, 0, ""),
                MacStatus("00005E005304", "組員電腦 B", "in_use", "192.0.2.61（伺服器登記）", None, int(0.8 * GB), 790 * 10 ** 6, "LAB-PC-02"),
                MacStatus("00005E005305", "", "throttled", "已知被限速", tomorrow, 0, 0, ""),
                MacStatus("00005E005306", "Pi 監視器", "fixed", "不參與輪流", None, 0, 0, "")]

    def switch_now(self, mac=None, force=False): ...
    def probe_now(self): ...
    def resume(self): ...
    def set_auto(self, on): ...
    def stop(self): ...


def shoot(role: str, out: Path) -> None:
    td = tempfile.TemporaryDirectory()
    s = Settings(role=role, auto_mode=True, hub_token="demo-not-a-secret" if role == "client" else "")
    svc = DemoService()
    app = App(s, Path(td.name) / "config.json", svc, svc.bus)
    app.geometry("+80+40")
    app.attributes("-topmost", True)

    def capture():
        app.update_idletasks()
        x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
        ps = ("Add-Type -AssemblyName System.Drawing; "
              f"$b = New-Object Drawing.Bitmap {w},{h}; $g = [Drawing.Graphics]::FromImage($b); "
              f"$g.CopyFromScreen({x},{y},0,0,$b.Size); $b.Save('{out}'); $g.Dispose(); $b.Dispose()")
        subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        print("已儲存", out, f"({w}x{h})")
        app.destroy()

    app.after(2500, capture)
    app.mainloop()
    td.cleanup()


if __name__ == "__main__":
    docs = ROOT / "docs"
    shoot("client", docs / "gui_client.png")
    shoot("hub", docs / "gui_main.png")
