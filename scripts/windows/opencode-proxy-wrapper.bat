@echo off
REM ========================================================
REM  OpenCode Proxy Wrapper (Windows)
REM  Khoi dong OpenCode Desktop voi Proxy Unlimited
REM  Bao ve TeamViewer, AnyDesk khong bi anh huong
REM ========================================================

set "HTTP_PROXY=http://127.0.0.1:28080"
set "HTTPS_PROXY=http://127.0.0.1:28080"
set "ALL_PROXY=http://127.0.0.1:28080"
set "NO_PROXY=localhost,127.0.0.1,::1,%COMPUTERNAME%,<local>"

REM Tat tien trinh OpenCode cu de backend khoi dong lai voi Proxy va Device ID moi
taskkill /F /IM OpenCode.exe >nul 2>nul
taskkill /F /IM opencode-cli.exe >nul 2>nul
ping 127.0.0.1 -n 2 >nul

set "OPENCODE_EXE=%LOCALAPPDATA%\Programs\ai.opencode.desktop\OpenCode.exe"
if exist "%OPENCODE_EXE%" (
    start "" "%OPENCODE_EXE%" --proxy-server="http://127.0.0.1:28080" --proxy-bypass-list="<local>;localhost;127.0.0.1;::1;%COMPUTERNAME%" %*
    exit /b 0
)

where opencode >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    opencode %*
    exit /b %ERRORLEVEL%
)

echo OpenCode binary khong tim thay trong thu muc tieu chuan.
pause
exit /b 1
