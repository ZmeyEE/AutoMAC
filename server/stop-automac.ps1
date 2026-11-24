# 用途：確實停掉背景的 AutoMAC。Stop-ScheduledTask 只會停外層命令列，底下的 python 會變孤兒繼續跑（占著 mDNS 名稱與 API 埠），
# 所以要連命令列含 "-m automac" 的 python 程序、以及 automac.exe 一起結束。需要系統管理員。
Stop-ScheduledTask -TaskName AutoMAC -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process | Where-Object { ($_.Name -like 'python*' -and $_.CommandLine -match '-m automac') -or $_.Name -eq 'automac.exe' } |
    ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 2
