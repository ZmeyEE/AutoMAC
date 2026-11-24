#!/bin/bash
# 用途：伺服器（NAS）換 MAC 後 IP 會變，但 pi-monitor 的 /etc/fstab 把 IP 寫死，掛載就一直指向舊位址。
# 這支腳本做兩件事：
#   1. fstab 的伺服器位址從 IP 改成「<電腦名稱>.local」（mDNS 名稱；Pi 已裝 libnss-mdns，實測能解析 Windows 主機名稱）
#   2. 裝一個每 30 秒檢查的 systemd 計時器 nas-heal：名稱解析出的 IP 和目前掛載的不同、或掛載沒回應，就重新掛載
# 不改 monitor.py：它本來就會先存本機 spool、NAS 回來後補推，換成能自己接回就夠了。
# 用法（需要 root，也就是你輸入 sudo 密碼）：
#   sudo bash fix-nas-mount.sh <伺服器電腦名稱> [共用資料夾名稱]   例：sudo bash fix-nas-mount.sh SERVER-PC share
#   sudo bash fix-nas-mount.sh --undo                 還原 fstab 並移除計時器
# 限制：mDNS 紀錄有存活時間（約 2 分鐘），換 MAC 後最久約 2 到 3 分鐘才接回；這段期間錄影照常存本機。
set -euo pipefail

MNT=/mnt/nas
SHARE="${2:-share}"        # 共用資料夾名稱（第二個參數），預設 share
HEAL=/usr/local/sbin/nas-heal.sh
UNIT_DIR=/etc/systemd/system

[ "$(id -u)" -eq 0 ] || { echo "需要 root：請用 sudo 執行"; exit 1; }

# ---------- 還原 ----------
if [ "${1:-}" = "--undo" ]; then
    systemctl disable --now nas-heal.timer 2>/dev/null || true
    rm -f "$UNIT_DIR/nas-heal.timer" "$UNIT_DIR/nas-heal.service" "$HEAL"
    latest=$(ls -1t /etc/fstab.bak-* 2>/dev/null | head -1 || true)
    if [ -n "$latest" ]; then cp -a "$latest" /etc/fstab; echo "fstab 已還原為 $latest"; fi
    systemctl daemon-reload
    echo "已還原。若要立刻套用舊 fstab：sudo systemctl restart mnt-nas.automount"
    exit 0
fi

HOST="${1:?請給伺服器電腦名稱（不含 .local），例如 SERVER-PC}"
FQDN="${HOST}.local"

# ---------- 0. 先確認名稱真的解析得到，否則什麼都不改 ----------
NEWIP=$(getent ahostsv4 "$FQDN" | awk 'NR==1{print $1}')
if [ -z "$NEWIP" ]; then
    echo "解析不到 $FQDN（伺服器沒開機、名稱打錯、或 mDNS 被擋）。沒有改任何東西。"
    exit 1
fi
echo "$FQDN 解析為 $NEWIP"

# ---------- 1. 備份並修改 fstab（只改位址那一段，憑證與選項不動）----------
BAK="/etc/fstab.bak-$(date +%Y%m%d-%H%M%S)"
cp -a /etc/fstab "$BAK"
echo "已備份 fstab 到 $BAK"
sed -i -E "s#^//[^/[:space:]]+/${SHARE}([[:space:]])#//${FQDN}/${SHARE}\1#" /etc/fstab
grep -q "^//${FQDN}/${SHARE}[[:space:]]" /etc/fstab || { echo "fstab 沒有改到（格式不符），已保留備份 $BAK，請手動檢查"; exit 1; }

# ---------- 2. 自我修復腳本 ----------
cat > "$HEAL" <<EOF
#!/bin/bash
# 由 fix-nas-mount.sh 產生。每 30 秒由 nas-heal.timer 執行。
# 邏輯：伺服器名稱現在對應的 IP，和掛載使用的 addr 不同，或掛載點讀不到，就卸載重掛。
MNT=$MNT
NAME=$FQDN
log() { logger -t nas-heal "\$*"; }

new=\$(getent ahostsv4 "\$NAME" | awk 'NR==1{print \$1}')
[ -n "\$new" ] || exit 0                      # 伺服器目前不在線，沒事可做，等下次

cur=\$(findmnt -n -t cifs -o OPTIONS "\$MNT" 2>/dev/null | grep -o 'addr=[^,]*' | head -1 | cut -d= -f2)

if [ -n "\$cur" ] && [ "\$cur" = "\$new" ] && timeout 8 stat "\$MNT" >/dev/null 2>&1 && timeout 8 ls "\$MNT" >/dev/null 2>&1; then
    exit 0                                    # 位址一致且讀得到，正常
fi

log "需要重掛：目前 addr=\${cur:-無} 名稱解析=\$new"
umount -l "\$MNT" 2>/dev/null                 # -l：有程式還在用也先卸掉，monitor.py 的複製會失敗後自動重試
systemctl restart mnt-nas.automount
timeout 20 ls "\$MNT" >/dev/null 2>&1 && log "重掛成功" || log "重掛後仍讀不到，下次再試"
EOF
chmod 755 "$HEAL"

# ---------- 3. systemd 單元 ----------
cat > "$UNIT_DIR/nas-heal.service" <<EOF
[Unit]
Description=NAS 掛載自我修復（伺服器換 IP 後重掛）
After=network-online.target

[Service]
Type=oneshot
ExecStart=$HEAL
EOF
cat > "$UNIT_DIR/nas-heal.timer" <<EOF
[Unit]
Description=每 30 秒檢查 NAS 掛載

[Timer]
OnBootSec=60
OnUnitActiveSec=30
AccuracySec=5s

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable --now nas-heal.timer
systemctl start nas-heal.service || true
echo "完成。查看紀錄：journalctl -t nas-heal -n 20    目前掛載：findmnt -t cifs"
