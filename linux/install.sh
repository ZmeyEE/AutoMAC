#!/bin/bash
# 用途：一鍵安裝 AutoMAC 的 Linux 組員端（背景服務，沒有視窗）。
# 用法（解壓縮後，在資料夾裡執行這一行就好）：./install.sh        （不是 root 會自動用 sudo 重新執行，可能要你輸入密碼）
# 做的事（全部在這個腳本裡，沒有別的隱藏步驟）：
#   1. 需要 root：不是 root 就改用 sudo 重新執行自己
#   2. 檢查環境；缺 ip 指令、python3 或 python3-venv 時，用系統的套件管理程式（apt 或 dnf）自動安裝，並先告訴你裝了什麼
#   3. 把程式複製到 /opt/automac/lib，建立 /opt/automac/venv 並用 pip 安裝 psutil 與 zeroconf（需要連網）
#   4. 建立指令 /usr/local/bin/automac
#   5. 套用同資料夾的 automac-client.json（只收連線欄位）到 /var/lib/automac/config.json（權限 0700，內含 token）
#   6. 安裝並啟動 systemd 服務 automac-client（開機自動啟動），最多等 30 秒確認它真的在跑，並顯示目前狀態
# 重複執行是安全的：程式與服務會更新，已存在的設定不會被覆蓋。
# 移除：sudo bash /opt/automac/uninstall.sh（安裝時會把 uninstall.sh 也放進去，解壓縮的資料夾可以刪）
set -euo pipefail
umask 022                                  # 不管呼叫者的 umask 是什麼，安裝出來的檔案都不是群組或其他人可寫（服務用 root 執行這裡的程式）

HERE=$(cd "$(dirname "$0")" && pwd)
DEST=/opt/automac
DATA=/var/lib/automac
CONF=$DATA/config.json

die() { echo "錯誤：$*" >&2; exit 1; }

# ---- 1. 需要 root
if [ "$(id -u)" -ne 0 ]; then
    command -v sudo >/dev/null 2>&1 || die "需要 root，而且找不到 sudo：請用 root 身分執行這個腳本"
    echo "需要管理員權限，改用 sudo 重新執行（可能要輸入你的密碼）…"
    exec sudo bash "$HERE/install.sh" "$@"
fi
command -v systemctl >/dev/null 2>&1 || die "需要 systemd（找不到 systemctl）"
[ -d "$HERE/automac" ] || die "找不到 $HERE/automac，請在解壓縮後的資料夾裡執行"
if [ ! -f "$CONF" ] && [ ! -f "$HERE/automac-client.json" ]; then
    die "找不到 automac-client.json（實驗室伺服器給的組員設定檔），請放在這個資料夾再執行"
fi

# ---- 2. 系統套件：缺什麼補什麼
py_ok() { python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; }
venv_ok() { python3 -c 'import venv, ensurepip' 2>/dev/null; }
need=""
command -v ip >/dev/null 2>&1 || need="$need ip"
command -v python3 >/dev/null 2>&1 || need="$need python3"
if command -v python3 >/dev/null 2>&1 && py_ok && ! venv_ok; then need="$need venv"; fi
if [ -n "$need" ]; then
    if command -v apt-get >/dev/null 2>&1; then
        pk="iproute2 python3 python3-venv"
        echo "缺少系統套件（$need），用 apt 安裝：$pk"
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq && apt-get install -y -qq --no-install-recommends $pk || die "apt 安裝失敗，請手動安裝：$pk"
    elif command -v dnf >/dev/null 2>&1; then
        pk="iproute python3"
        echo "缺少系統套件（$need），用 dnf 安裝：$pk"
        dnf install -y -q $pk || die "dnf 安裝失敗，請手動安裝：$pk"
    else
        die "缺少 $need，而且不認得這個系統的套件管理程式：請手動安裝 iproute2、python3（3.11 以上）與 venv 模組"
    fi
fi
command -v ip >/dev/null 2>&1 || die "找不到 ip 指令，請安裝 iproute2"
PYBIN=$(command -v python3 || true)
[ -n "$PYBIN" ] || die "找不到 python3"
py_ok || die "需要 Python 3.11 以上（目前是 $("$PYBIN" -c 'import sys; print("%d.%d" % sys.version_info[:2])')）。Ubuntu 22.04 內建 3.10，請先安裝較新的 Python"
venv_ok || die "缺少 Python 的 venv 模組（Debian/Ubuntu：apt install python3-venv）"

echo "[1/5] 複製程式到 $DEST/lib"
mkdir -p "$DEST/lib"
rm -rf "$DEST/lib/automac"
cp -r "$HERE/automac" "$DEST/lib/automac"
find "$DEST/lib/automac" -name '__pycache__' -prune -exec rm -rf {} +
[ -f "$HERE/uninstall.sh" ] && install -m 755 "$HERE/uninstall.sh" "$DEST/uninstall.sh"       # 解壓縮的資料夾之後可以刪，移除腳本留一份在安裝位置

echo "[2/5] 建立 Python 環境並安裝 psutil、zeroconf（需要連網）"
[ -x "$DEST/venv/bin/python" ] || "$PYBIN" -m venv "$DEST/venv"
"$DEST/venv/bin/pip" install --quiet --disable-pip-version-check "psutil>=5.9" "zeroconf>=0.130" \
    || die "pip 安裝失敗：請確認這台電腦能連到網際網路，再重新執行"

chown -R root:root "$DEST" && chmod -R go-w "$DEST"          # 服務以 root 執行 $DEST 底下的程式，不可被其他人改寫

echo "[3/5] 建立指令 /usr/local/bin/automac"
cat > /usr/local/bin/automac <<EOF
#!/bin/sh
PYTHONPATH=$DEST/lib exec $DEST/venv/bin/python -P -m automac "\$@"
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
( cd / && /usr/local/bin/automac install-service )        # 在 / 執行：不讓目前資料夾（解壓縮的位置）影響套件路徑

# ---- 6. 確認真的在跑
ok=""
for _ in $(seq 1 30); do
    if systemctl is-active --quiet automac-client.service && /usr/local/bin/automac live --lines 0 >/dev/null 2>&1; then ok=1; break; fi
    sleep 1
done
echo
if [ -n "$ok" ]; then
    /usr/local/bin/automac live --lines 6 || true
    echo
    echo "安裝完成，服務已在執行，開機會自動啟動。之後查看狀態：sudo automac live    日誌：journalctl -u automac-client -f    移除：sudo bash $DEST/uninstall.sh"
else
    echo "服務已安裝，但 30 秒內沒有回應。最近的日誌：" >&2
    journalctl -u automac-client.service -n 20 --no-pager >&2 || true
    exit 1
fi
