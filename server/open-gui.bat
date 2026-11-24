@echo off
rem 用途：在伺服器桌面開 AutoMAC 視窗。要在伺服器本機（實體螢幕或遠端桌面）執行。
rem 流程：自動要求系統管理員 → 暫停背景服務（避免兩份服務同名衝突）→ 開視窗 → 關視窗後恢復背景服務。
net session >nul 2>&1 || (powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs" & exit /b)
if "%AUTOMAC_HOME%"=="" (set ROOT=C:\AutoMAC) else set ROOT=%AUTOMAC_HOME%
set AUTOMAC_CONFIG=%ROOT%\config.json
set PYTHONPATH=%ROOT%\src
powershell -NoProfile -ExecutionPolicy Bypass -File "%ROOT%\stop-automac.ps1"
"%ROOT%\.venv\Scripts\python.exe" -X utf8 -m automac gui
schtasks /run /tn AutoMAC >nul 2>&1
