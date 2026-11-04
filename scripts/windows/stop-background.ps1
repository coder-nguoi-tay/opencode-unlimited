# Stop OpenCode Unlimited running background instances
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🛑 DỪNG CÁC TIẾN TRÌNH CHẠY NGẦM OPENCODE UNLIMITED" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$stopped = 0
Get-CimInstance Win32_Process | Where-Object { 
    $_.CommandLine -like "*main.py*" -or $_.CommandLine -like "*bridge.py*"
} | ForEach-Object {
    Write-Host "🛑 Đang tắt tiến trình: $($_.ProcessId) ($($_.Name))" -ForegroundColor Yellow
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped++
}

# Tắt Windows System Proxy nếu đang trỏ tới 127.0.0.1:28080
try {
    $proxy = (Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings').ProxyServer
    if ($proxy -like "*127.0.0.1:28080*") {
        Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -Name ProxyEnable -Value 0
        Write-Host "🌐 Đã khôi phục Windows System Proxy về tắt." -ForegroundColor Green
    }
} catch {}

if ($stopped -gt 0) {
    Write-Host "✅ Đã dừng thành công $stopped tiến trình." -ForegroundColor Green
} else {
    Write-Host "ℹ️ Không tìm thấy tiến trình nào đang chạy ngầm." -ForegroundColor Cyan
}
