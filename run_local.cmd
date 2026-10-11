@echo off
REM 雙擊即可：建 venv -> 裝套件 -> initdb -> seed -> 自測 -> 開儀表板
REM 用 -ExecutionPolicy Bypass 呼叫，所以唔會撞到 PowerShell 執行原則。
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run_local.ps1" %*
echo.
pause
