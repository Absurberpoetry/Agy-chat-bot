@echo off
chcp 65001 >nul
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [ERROR] .venv not found. Please run install.bat first.
    exit /b 1
)
if not exist "config.json" (
    echo [ERROR] config.json not found. Please run install.bat first.
    exit /b 1
)

".venv\Scripts\python.exe" -X utf8 -u run.py %*
