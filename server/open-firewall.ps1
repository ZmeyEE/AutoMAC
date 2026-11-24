# 用途：在伺服器上為 AutoMAC 加兩條防火牆規則，只開放本機子網路（不對外網與其他網段開放）。
#   TCP 47601：組員端連線的伺服器 API（TLS 1.3 + 憑證釘選 + token）
#   UDP 5353 ：mDNS，讓組員端用「電腦名稱.local」找到伺服器、讓各電腦互相宣告目前使用的 MAC
# 為什麼要有這支：伺服器 API 預設監聽所有網卡（0.0.0.0），能擋住其他網段的只有防火牆；沒有這兩條規則時，
# 請不要啟用伺服器角色。用法（系統管理員 PowerShell）：
#   .\open-firewall.ps1 [-Exe D:\AutoMAC\automac.exe] [-Port 47601]
#   .\open-firewall.ps1 -Remove        移除這兩條規則
param([string]$Exe = '', [int]$Port = 47601, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$names = @('AutoMAC hub API (TCP)', 'AutoMAC mDNS (UDP)')
foreach ($n in $names) { Remove-NetFirewallRule -DisplayName $n -ErrorAction SilentlyContinue }
if ($Remove) { Write-Host '已移除 AutoMAC 防火牆規則'; return }
New-NetFirewallRule -DisplayName $names[0] -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress LocalSubnet -Action Allow -Profile Any | Out-Null
$udp = @{ DisplayName = $names[1]; Direction = 'Inbound'; Protocol = 'UDP'; LocalPort = 5353; RemoteAddress = 'LocalSubnet'; Action = 'Allow'; Profile = 'Any' }
if ($Exe) { $udp.Program = $Exe }
New-NetFirewallRule @udp | Out-Null
Write-Host "已加入規則：TCP $Port 與 UDP 5353，只允許本機子網路"
