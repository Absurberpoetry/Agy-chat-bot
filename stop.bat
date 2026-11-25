@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    schtasks /End /TN AGYBot >nul 2>&1
    echo AGY CLI BOT stopped.
    exit /b 0
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -X utf8 -m agy_bot.win_bg stop
