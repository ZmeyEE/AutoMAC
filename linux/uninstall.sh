#!/bin/bash
# 用途：移除 AutoMAC 的 Linux 組員端（服務、程式、設定）。用法：sudo bash /opt/automac/uninstall.sh（或解壓縮資料夾裡的 uninstall.sh）
# MAC 清單、標籤與用量都記在伺服器上，刪掉本機資料不會遺失紀錄。
# 注意：換過的 MAC 不會自動換回，重開機後網卡會回到硬體原本的 MAC。
set -u
[ "$(id -u)" -eq 0 ] || { echo "錯誤：需要 root：請用 sudo bash uninstall.sh" >&2; exit 1; }
if [ -x /usr/local/bin/automac ]; then
    AUTOMAC_CONFIG=/var/lib/automac/config.json /usr/local/bin/automac uninstall-service --purge || true
else
    systemctl disable --now automac-client.service 2>/dev/null || true
    rm -f /etc/systemd/system/automac-client.service
    systemctl daemon-reload 2>/dev/null || true
fi
rm -f /usr/local/bin/automac
rm -rf /opt/automac
echo "已移除 AutoMAC"
