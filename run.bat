@echo off
REM OpenCode Unlimited - Quick Run Script (Windows)

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python main.py %*
) else (
    where py >nul 2>nul
    if %ERRORLEVEL% EQU 0 (
        py -3 main.py %*
    ) else (
        echo ❌ Loi: May tinh cua ban chua cai dat Python!
        echo 👉 Vui long cai dat Python tu: https://www.python.org/downloads/
        pause
    )
)
