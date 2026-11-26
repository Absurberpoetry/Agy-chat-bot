@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    schtasks /End /TN AgyChatBot >nul 2>&1
    schtasks /End /TN AGYBot >nul 2>&1
    echo Agy Chat Bot stopped.
    exit /b 0
)

set "PYTHONPATH=%~dp0src"
".venv\Scripts\python.exe" -X utf8 -m agy_chat_bot.win_bg stop
