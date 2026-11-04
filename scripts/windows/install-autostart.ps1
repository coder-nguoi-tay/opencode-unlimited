# OpenCode Unlimited - Autostart Service on Windows 10 & 11
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$TaskName = "OpenCodeUnlimited"
$ProjectDir = (Get-Item $PSScriptRoot).Parent.Parent.FullName
$VbsPath = Join-Path $PSScriptRoot "start-background.vbs"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🚀 ĐĂNG KÝ OPENCODE UNLIMITED CHẠY KHỞI ĐỘNG CÙNG WINDOWS" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

try {
    $Action = New-ScheduledTaskAction -Execute "wscript.exe" -Argument "`"$VbsPath`"" -WorkingDirectory $ProjectDir
    $Trigger = New-ScheduledTaskTrigger -AtLogOn
    $Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit 0

    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Settings $Settings -Description "OpenCode Unlimited Background Service" -Force | Out-Null
    Write-Host "✅ Đã đăng ký thành công Scheduled Task: '$TaskName'!" -ForegroundColor Green
    Write-Host "💡 Mỗi khi đăng nhập Windows, hệ thống sẽ tự động chạy ngầm mà không hiện cửa sổ console." -ForegroundColor Cyan
} catch {
    Write-Host "❌ Lỗi khi đăng ký task: $_" -ForegroundColor Red
}
