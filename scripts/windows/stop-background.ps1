# Stop OpenCode Unlimited running background instances
# Lưu ý: param() bắt buộc phải là câu lệnh đầu tiên của script.
param (
    [switch]$StopOpenCode
)

[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "========================================================" -ForegroundColor Cyan
Write-Host " 🛑 DỪNG CÁC TIẾN TRÌNH CHẠY NGẦM OPENCODE UNLIMITED" -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan

$stopped = 0
# Chỉ nhắm vào tiến trình Python (python/pythonw/py) chạy main.py hoặc bridge.py,
# tránh tắt nhầm editor hay chương trình khác có "main.py" trong command line.
Get-CimInstance Win32_Process | Where-Object {
    ($_.Name -match '^(python|pythonw|py)(\d+(\.\d+)*)?\.exe$') -and
    ($_.CommandLine -match '(^|[\\/\s"])(main|bridge)\.py\b')
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

if ($StopOpenCode) {
    Get-Process -Name "opencode-cli", "OpenCode" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "🛑 Đang dừng OpenCode: $($_.Id) ($($_.ProcessName))" -ForegroundColor Yellow
        Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
        $stopped++
    }
}

if ($stopped -gt 0) {
    Write-Host "✅ Đã dừng thành công $stopped tiến trình." -ForegroundColor Green
} else {
    Write-Host "ℹ️ Không tìm thấy tiến trình nào đang chạy ngầm." -ForegroundColor Cyan
}
