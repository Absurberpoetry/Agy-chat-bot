@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ========================================
echo   AgyChat Bot Windows Installer
echo ========================================

set "PY_CMD="
py -3 --version >nul 2>&1
if %errorlevel%==0 (
    set "PY_CMD=py -3"
) else (
    for %%P in (
        "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
        "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
    ) do (
        if exist %%~P (
            set "PY_CMD=%%~P"
            goto :found_py
        )
    )
    where python >nul 2>&1
    if !errorlevel!==0 (
        for /f "delims=" %%I in ('where python') do (
            echo %%I | findstr /i "WindowsApps" >nul
            if !errorlevel! neq 0 (
                set "PY_CMD=%%I"
                goto :found_py
            )
        )
    )
)

:found_py
if "%PY_CMD%"=="" (
    echo [ERROR] Python 3.11+ not found. Please install Python 3.11+ first.
    exit /b 1
)

echo [1/3] Python detected: %PY_CMD%
if not exist ".venv\Scripts\python.exe" (
    echo [2/3] Creating virtual environment .venv ...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (
        echo [ERROR] Failed to create .venv.
        exit /b 1
    )
) else (
    echo [2/3] Reusing existing .venv ...
)

echo [3/3] Installing requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [ERROR] pip install failed.
    exit /b 1
)

if not exist "config.json" (
    copy /y "templates\config.example.json" "config.json" >nul
    echo [OK] Created config.json from templates\config.example.json
) else (
    echo [OK] Kept existing config.json
)

if not exist "settings.antigravity-cli.bot.json" (
    copy /y "templates\settings.antigravity-cli.example.json" "settings.antigravity-cli.bot.json" >nul
    echo [OK] Created settings.antigravity-cli.bot.json
)

".venv\Scripts\python.exe" -X utf8 -c "print('\n========================================\n  安装完成！下一步：\n  1. 编辑 config.json（填写代理、QQ/TG 参数与 WebUI 密码）\n  2. 前台启动测试：运行 start.bat\n  3. 后台持久保活：运行 start_background.bat（SSH 断开不掉线）\n========================================')"
endlocal
