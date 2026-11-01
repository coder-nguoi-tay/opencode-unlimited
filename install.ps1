# OpenCode Unlimited - 1-Line Installer & Runner (Windows PowerShell)

$ErrorActionPreference = "Stop"

$RepoUrl = "https://github.com/coder-nguoi-tay/opencode-unlimited.git"
$InstallDir = "$env:USERPROFILE\.opencode-unlimited"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🚀 BAT DAU CAI DAT OPENCODE UNLIMITED (WINDOWS 1-CLICK)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# Kiem tra Python
$PythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCmd = "py -3"
} else {
    Write-Host "❌ Loi: Chua tim thay Python tren may tinh!" -ForegroundColor Red
    Write-Host "👉 Vui long cai dat Python tu: https://www.python.org/downloads/" -ForegroundColor Yellow
    exit 1
}

# Kiem tra thu muc hien tai
if ((Test-Path "main.py") -and (Test-Path "bridge.py")) {
    $TargetDir = (Get-Location).Path
} else {
    if (Test-Path "$InstallDir\.git") {
        Write-Host "🔄 Dang cap nhat code moi nhat..." -ForegroundColor Yellow
        Set-Location $InstallDir
        git pull --quiet
    } else {
        Write-Host "📥 Dang tai code tu GitHub ve: $InstallDir..." -ForegroundColor Yellow
        git clone --quiet $RepoUrl $InstallDir
        Set-Location $InstallDir
    }
    $TargetDir = $InstallDir
}

Set-Location $TargetDir
Write-Host "✅ Khoi dong thanh cong tai: $TargetDir" -ForegroundColor Green
Write-Host "🚀 Dang chay OpenCode Unlimited..." -ForegroundColor Cyan

Invoke-Expression "$PythonCmd main.py $args"
