# OpenCode Unlimited - Gỡ bỏ Autostart Task
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$TaskName = "OpenCodeUnlimited"

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🗑️ GỠ BỎ OPENCODE UNLIMITED KHỎI KHỞI ĐỘNG CÙNG WINDOWS" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

try {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Write-Host "✅ Đã gỡ bỏ Scheduled Task '$TaskName' thành công!" -ForegroundColor Green
} catch {
    Write-Host "❌ Lỗi: $_" -ForegroundColor Red
}
