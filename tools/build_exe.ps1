# 用途：把 AutoMAC 打包成單一 exe（dist\automac.exe）。需要專案 .venv 內已安裝 pyinstaller。
# --uac-admin：開啟時自動要求系統管理員（換 MAC 需要）；--windowed：不出現黑色主控台視窗。
# 用法：powershell -ExecutionPolicy Bypass -File tools\build_exe.ps1
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root
& "$root\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed --uac-admin `
    --name automac --paths src --collect-submodules zeroconf `
    --distpath dist --workpath build\pyi --specpath build `
    tools\exe_entry.py
if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失敗（代碼 $LASTEXITCODE）" }
Get-Item "$root\dist\automac.exe" | Select-Object Name, @{n = 'MB'; e = { [math]::Round($_.Length / 1MB, 1) } }
