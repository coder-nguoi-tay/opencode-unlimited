@echo off
REM ========================================================
REM  OpenCode Proxy Wrapper (Windows)
REM  Khoi dong OpenCode Desktop / CLI chi dinh rieng Proxy noi bo
REM  Khong anh huong toi TeamViewer, AnyDesk hay trinh duyet khac
REM ========================================================

set "HTTP_PROXY=http://127.0.0.1:28080"
set "HTTPS_PROXY=http://127.0.0.1:28080"
set "ALL_PROXY=http://127.0.0.1:28080"
set "NO_PROXY=localhost,127.0.0.1,::1"

set "OPENCODE_EXE=%LOCALAPPDATA%\Programs\ai.opencode.desktop\OpenCode.exe"
if exist "%OPENCODE_EXE%" (
    start "" "%OPENCODE_EXE%" --proxy-server="http://127.0.0.1:28080" --proxy-bypass-list="localhost;127.0.0.1" %*
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
