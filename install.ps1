# OpenCode Unlimited - 1-Line Installer & Runner (Windows 10 & 11 PowerShell)

$ErrorActionPreference = "Stop"

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

$RepoUrl = "https://github.com/coder-nguoi-tay/opencode-unlimited.git"
$ZipUrl  = "https://github.com/coder-nguoi-tay/opencode-unlimited/archive/refs/heads/main.zip"
$InstallDir = "$env:USERPROFILE\.opencode-unlimited"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🚀 BAT DAU CAI DAT OPENCODE UNLIMITED (WINDOWS 10 & 11)" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

# 1. Kiem tra Python
$PythonCmd = $null
if (Get-Command python -ErrorAction SilentlyContinue) {
    $PythonCmd = "python"
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $PythonCmd = "py -3"
} else {
    Write-Host "❌ Loi: Chua tim thay Python tren may tinh!" -ForegroundColor Red
    Write-Host "👉 Vui long tai va cai dat Python tu: https://www.python.org/downloads/" -ForegroundColor Yellow
    Write-Host "⚠️ Khi cai nho tich chon checkbox: 'Add python.exe to PATH'" -ForegroundColor Yellow
    exit 1
}

# 2. Chuan bi thu muc cai dat
if ((Test-Path "main.py") -and (Test-Path "bridge.py")) {
    $TargetDir = (Get-Location).Path
} else {
    if (-not (Test-Path $InstallDir)) {
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    if (Get-Command git -ErrorAction SilentlyContinue) {
        if (Test-Path "$InstallDir\.git") {
            Write-Host "🔄 Dang cap nhat code moi nhat tu GitHub..." -ForegroundColor Yellow
            Push-Location $InstallDir
            git pull --quiet
            Pop-Location
        } else {
            Write-Host "📥 Dang tai ma nguon tu GitHub ve: $InstallDir..." -ForegroundColor Yellow
            git clone --quiet $RepoUrl $InstallDir
        }
    } else {
        # Fallback neu may Windows chua cai Git
        Write-Host "📦 Git chua cai dat, dang tai file ZIP truc tiep..." -ForegroundColor Yellow
        $ZipFile = "$env:TEMP\opencode-unlimited.zip"
        Invoke-WebRequest -Uri $ZipUrl -OutFile $ZipFile -UseBasicParsing
        Expand-Archive -Path $ZipFile -DestinationPath "$env:TEMP\opencode-unlimited-extracted" -Force
        Copy-Item -Path "$env:TEMP\opencode-unlimited-extracted\opencode-unlimited-main\*" -Destination $InstallDir -Recurse -Force
        Remove-Item -Path $ZipFile -Force -ErrorAction SilentlyContinue
        Remove-Item -Path "$env:TEMP\opencode-unlimited-extracted" -Recurse -Force -ErrorAction SilentlyContinue
    }
    $TargetDir = $InstallDir
}

# 3. Tao lenh toan cuc 'opencode-unlimited' cho Windows
try {
    $CmdFile = Join-Path $TargetDir "opencode-unlimited.cmd"
    $BatchLine1 = "@echo off"
    $BatchLine2 = 'call "%~dp0run.bat" %*'
    Set-Content -Path $CmdFile -Value @($BatchLine1, $BatchLine2) -Encoding ASCII

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($UserPath -notlike "*$TargetDir*") {
        [Environment]::SetEnvironmentVariable("Path", "$TargetDir;$UserPath", "User")
        $env:Path = "$TargetDir;$env:Path"
    }
} catch {
    # Bo qua neu khong the ghi bien moi truong User
}

# 4. Tao Desktop Shortcut
try {
    $WshShell = New-Object -ComObject WScript.Shell
    $DesktopPath = [Environment]::GetFolderPath("Desktop")
    if (Test-Path $DesktopPath) {
        $ShortcutPath = Join-Path $DesktopPath "OpenCode Unlimited.lnk"
        $Shortcut = $WshShell.CreateShortcut($ShortcutPath)
        $Shortcut.TargetPath = (Join-Path $TargetDir "run.bat")
        $Shortcut.WorkingDirectory = $TargetDir
        $Shortcut.Description = "OpenCode Unlimited Automation for Windows"
        $Shortcut.Save()
    }
} catch {
    # Bo qua neu khong tao duoc shortcut
}

Set-Location $TargetDir
Write-Host "✅ Cai dat thanh cong tai: $TargetDir" -ForegroundColor Green
Write-Host "💡 Tu gio ban co the go 'opencode-unlimited' o bat ky cua so PowerShell / CMD nao!" -ForegroundColor Cyan
Write-Host "🚀 Dang khoi dong OpenCode Unlimited..." -ForegroundColor Cyan

Invoke-Expression "$PythonCmd main.py $args"
