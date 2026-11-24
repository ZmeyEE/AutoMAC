"""把 Linux 組員端打包成 AutoMAC-Linux.tar.gz（純 Python 程式 + 安裝腳本，沒有編譯產物）。

用法：
    python tools/make_linux_pkg.py --out AutoMAC-Linux.tar.gz [--client-json automac-client.json]
--client-json 是實驗室伺服器匯出的組員設定檔（automac hub client-file 產生，內含 token）；不給就打出不含設定檔的包，
使用者安裝前要自己把檔案放進解壓縮的資料夾。

為什麼用 Python 的 tarfile 而不是 tar 指令：在 Windows 上打包時要保證 .sh 是 LF 換行、安裝腳本有執行權限、
檔案擁有者是 root、設定檔權限 0600，這些用 tarfile 可以逐檔指定，不受打包機器的檔案系統影響。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "AutoMAC-Linux"
SKIP_DIRS = {"__pycache__"}


def _add(tar: tarfile.TarFile, arcname: str, data: bytes, mode: int) -> None:
    ti = tarfile.TarInfo(f"{TOP}/{arcname}")
    ti.size, ti.mode, ti.uid, ti.gid, ti.uname, ti.gname, ti.mtime = len(data), mode, 0, 0, "root", "root", 1_700_000_000
    tar.addfile(ti, io.BytesIO(data))


def build(out: Path, client_json: Path | None) -> str:
    with tarfile.open(out, "w:gz") as tar:
        for name in ("install.sh", "uninstall.sh", "使用說明.txt"):
            data = (ROOT / "linux" / name).read_bytes().replace(b"\r\n", b"\n")        # 腳本與說明一律 LF
            _add(tar, name, data, 0o755 if name.endswith(".sh") else 0o644)
        pkg = ROOT / "src" / "automac"
        for p in sorted(pkg.rglob("*")):
            if p.is_file() and not (set(p.relative_to(pkg).parts) & SKIP_DIRS) and p.suffix != ".pyc":
                _add(tar, f"automac/{p.relative_to(pkg).as_posix()}", p.read_bytes(), 0o644)
        if client_json:
            _add(tar, "automac-client.json", client_json.read_bytes(), 0o600)
    return hashlib.sha256(out.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--client-json")
    a = ap.parse_args()
    cj = Path(a.client_json) if a.client_json else None
    if cj and not cj.is_file():
        print(f"找不到組員設定檔：{cj}", file=sys.stderr)
        return 1
    out = Path(a.out)
    digest = build(out, cj)
    print(f"已產生 {out}（{out.stat().st_size / 1024:.0f} KB）  SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
