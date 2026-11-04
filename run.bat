@echo off
REM OpenCode Unlimited - Quick Run Script (Windows 10 & 11)
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

cd /d "%~dp0"

where python >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    python main.py %*
    exit /b %ERRORLEVEL%
)

where py >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    py -3 main.py %*
    exit /b %ERRORLEVEL%
)

echo.
echo ========================================================
echo  Loi: May tinh cua ban chua cai dat Python!
echo  Vui long cai dat Python tu: https://www.python.org/downloads/
echo  Luu y khi cai dat: Nho tich chon "Add Python to PATH"
echo ========================================================
echo.
pause
exit /b 1
