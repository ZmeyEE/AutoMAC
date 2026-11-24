# 用途：把 AutoMAC 以排程工作的方式裝到這台伺服器，開機自動啟動（SYSTEM 身分，換 MAC 需要）。
# 預設「試運行」：只監看與記錄，不會真的換 MAC。確認紀錄沒問題後，要啟用自動切換才加 -Enable。
# 用法（系統管理員 PowerShell）：
#   .\install-service.ps1                 安裝並以試運行啟動
#   .\install-service.ps1 -Enable         改為真的自動切換（會讓伺服器 IP 改變，其他服務與 Pi 傳輸會短暫中斷）
#   .\install-service.ps1 -Remove         移除
# 安裝資料夾預設 C:\AutoMAC（可用環境變數 AUTOMAC_HOME 指定，不要放在共用資料夾內）；日誌在該資料夾的 automac.log
param([switch]$Enable, [switch]$Remove)
$ErrorActionPreference = 'Stop'
$Root = if ($env:AUTOMAC_HOME) { $env:AUTOMAC_HOME } else { 'C:\AutoMAC' }
$Name = 'AutoMAC'
if ($Remove) {
    & "$PSScriptRoot\stop-automac.ps1"
    Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match 'automac.*run' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Unregister-ScheduledTask -TaskName $Name -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host '已移除排程工作 AutoMAC'; return
}
$mode = if ($Enable) { '--auto' } else { '--dry-run' }
# 有 automac.exe 就用它（設定檔放在 exe 旁邊，不需要 Python 與原始碼）；沒有才退回 venv 的 python
if (Test-Path "$Root\automac.exe") { $cmd = "$Root\automac.exe run $mode" }
else { $cmd = "set AUTOMAC_CONFIG=$Root\config.json&& set PYTHONPATH=$Root\src&& $Root\.venv\Scripts\python.exe -X utf8 -m automac run $mode" }
# exe 是視窗程式：經 cmd /c 啟動時 cmd 立刻結束，排程工作顯示「就緒」而 exe 變孤兒，當掉也不會被重啟；所以 exe 直接當動作
if (Test-Path "$Root\automac.exe") { $action = New-ScheduledTaskAction -Execute "$Root\automac.exe" -Argument "run $mode" -WorkingDirectory $Root }
else { $action = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument "/c $cmd" }
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -RunLevel Highest -LogonType ServiceAccount
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 5 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName $Name -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
& "$PSScriptRoot\stop-automac.ps1"
Start-ScheduledTask -TaskName $Name
Write-Host "已安裝並啟動 AutoMAC（模式 $mode）。日誌：$Root\automac.log"
