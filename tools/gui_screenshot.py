"""開啟視窗（試運行）、等幾秒讓狀態填滿、擷取視窗畫面存成 PNG 後關閉。用於驗證版面。
用法：python tools/gui_screenshot.py 輸出.png [等待秒數]"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from automac.gui.app import build_app  # noqa: E402

out = Path(sys.argv[1]).resolve()
wait_ms = int(float(sys.argv[2]) * 1000) if len(sys.argv) > 2 else 9000
app = build_app(dry_run=True)


def shoot():
    app.update_idletasks()
    x, y, w, h = app.winfo_rootx(), app.winfo_rooty(), app.winfo_width(), app.winfo_height()
    ps = ("Add-Type -AssemblyName System.Drawing; "
          f"$b = New-Object Drawing.Bitmap {w},{h}; $g = [Drawing.Graphics]::FromImage($b); "
          f"$g.CopyFromScreen({x},{y},0,0,$b.Size); $b.Save('{out}'); $g.Dispose(); $b.Dispose()")
    subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
    print("已儲存", out, f"({w}x{h})")
    app._close()


app.attributes("-topmost", True)
app.after(wait_ms, shoot)
app.mainloop()
