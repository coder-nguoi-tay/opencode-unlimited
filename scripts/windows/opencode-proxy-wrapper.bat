@echo off
REM ========================================================
REM  OpenCode Proxy Wrapper (Windows)
REM  Khoi dong OpenCode voi cac bien moi truong Proxy noi bo
REM ========================================================

set "HTTP_PROXY=http://127.0.0.1:28080"
set "HTTPS_PROXY=http://127.0.0.1:28080"
set "ALL_PROXY=http://127.0.0.1:28080"
set "NO_PROXY=localhost,127.0.0.1,::1"

where opencode >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    opencode %*
    exit /b %ERRORLEVEL%
)

if exist "%LOCALAPPDATA%\Programs\ai.opencode.desktop\OpenCode.exe" (
    start "" "%LOCALAPPDATA%\Programs\ai.opencode.desktop\OpenCode.exe" %*
    exit /b 0
)

echo OpenCode binary khong tim thay trong thu muc tieu chuan.
exit /b 1
