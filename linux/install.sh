#!/bin/bash
# 用途：安裝 AutoMAC 的 Linux 組員端（背景服務，沒有視窗）。
# 用法（在解壓縮後的資料夾裡）：sudo bash install.sh
# 做的事（全部在這個腳本裡，沒有別的隱藏步驟）：
#   1. 檢查環境：root、systemd、ip 指令、Python 3.11 以上、python3-venv
#   2. 把程式複製到 /opt/automac/lib，建立 /opt/automac/venv 並用 pip 安裝 psutil 與 zeroconf（需要連網）
#   3. 建立指令 /usr/local/bin/automac
#   4. 套用同資料夾的 automac-client.json（只收連線欄位）到 /var/lib/automac/config.json（權限 0700，內含 token）
#   5. 安裝並啟動 systemd 服務 automac-client（開機自動啟動）
# 重複執行是安全的：程式與服務會更新，已存在的設定不會被覆蓋。
# 移除：sudo bash uninstall.sh
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/automac
DATA=/var/lib/automac
CONF=$DATA/config.json

die() { echo "錯誤：$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "需要 root：請用 sudo bash install.sh"
command -v systemctl >/dev/null 2>&1 || die "需要 systemd（找不到 systemctl）"
command -v ip >/dev/null 2>&1 || die "找不到 ip 指令，請先安裝 iproute2（Debian/Ubuntu：sudo apt install iproute2）"
PYBIN=$(command -v python3 || true)
[ -n "$PYBIN" ] || die "找不到 python3"
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "需要 Python 3.11 以上（目前是 $("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')）"
"$PYBIN" -c 'import venv, ensurepip' 2>/dev/null \
    || die "缺少 python3-venv（Debian/Ubuntu：sudo apt install python3-venv）"
[ -d "$HERE/automac" ] || die "找不到 $HERE/automac，請在解壓縮後的資料夾裡執行"
if [ ! -f "$CONF" ] && [ ! -f "$HERE/automac-client.json" ]; then
    die "找不到 automac-client.json（實驗室伺服器給的組員設定檔），請放在這個資料夾再執行"
fi

echo "[1/5] 複製程式到 $DEST/lib"
mkdir -p "$DEST/lib"
rm -rf "$DEST/lib/automac"
cp -r "$HERE/automac" "$DEST/lib/automac"
find "$DEST/lib/automac" -name '__pycache__' -prune -exec rm -rf {} +

echo "[2/5] 建立 Python 環境並安裝 psutil、zeroconf（需要連網）"
[ -x "$DEST/venv/bin/python" ] || "$PYBIN" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --disable-pip-version-check "psutil>=5.9" "zeroconf>=0.130" \
    || die "pip 安裝失敗：請確認這台電腦能連到網際網路，再重新執行"

echo "[3/5] 建立指令 /usr/local/bin/automac"
cat > /usr/local/bin/automac <<EOF
#!/bin/sh
PYTHONPATH=$DEST/lib exec $DEST/venv/bin/python -m automac "\$@"
EOF
chmod 755 /usr/local/bin/automac

echo "[4/5] 套用組員設定檔"
mkdir -p "$DATA"
chmod 700 "$DATA"
export AUTOMAC_CONFIG="$CONF"
if [ -f "$CONF" ]; then
    echo "      已有設定（$CONF），保留不動"
else
    ( cd "$HERE" && /usr/local/bin/automac join automac-client.json )
fi

echo "[5/5] 安裝並啟動背景服務"
/usr/local/bin/automac install-service

sleep 3
echo
systemctl --no-pager --lines=0 status automac-client.service | head -4 || true
echo
echo "完成。查看狀態：sudo automac live    日誌：journalctl -u automac-client -f    移除：sudo bash uninstall.sh"
