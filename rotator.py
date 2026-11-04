#!/usr/bin/env python3
"""
OpenCode Unlimited - Rotating Proxy Manager & Deduplication Engine
Tác giả: coder-nguoi-tay
Mục đích:
- Gọi API xoay IP tự động (Hỗ trợ proxyxoay.shop và các proxy xoay tương thích).
- Tự động phát hiện và chờ đúng thời gian Cooldown của nhà mạng.
- Quản lý danh sách IP đã dùng (used_ips.json), tự động lọc bỏ các IP trùng lặp.
- Tự động reset danh sách theo chu kỳ hạn mức toàn cầu (07:00 AM giờ VN / 00:00 UTC).
"""

import urllib.request
import urllib.error
import json
import time
import datetime
import os
import sys
import re
import platform
import subprocess
import shutil
import threading

# Fix mã hóa Unicode tiếng Việt trên Windows Console
if sys.platform == "win32":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from spoofer import get_opencode_dir, generate_new_device_id

class ProxyRotator:
    def __init__(self, api_url, upstream_proxy="160.250.166.20:10427", local_port=28080,
                 cooldown_extra_seconds=2, max_dedup_attempts=10, logger=None):
        self.api_url = api_url
        self.upstream_proxy = upstream_proxy
        self.local_port = int(local_port)
        self.cooldown_extra_seconds = int(cooldown_extra_seconds)
        self.max_dedup_attempts = int(max_dedup_attempts)
        self.logger = logger
        self.opencode_dir = get_opencode_dir()
        self.used_ips_file = os.path.join(self.opencode_dir, "used_ips.json")
        self.log_file = os.path.join(self.opencode_dir, "log", "proxy-rotate.log")
        os.makedirs(os.path.dirname(self.log_file), exist_ok=True)

    def log(self, msg):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}"
        print(line, flush=True)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def notify(self, title, message):
        """Hiển thị thông báo trên Desktop mà không làm nghẽn tiến trình chính."""
        system = platform.system()

        def _send():
            try:
                if system == "Darwin":
                    apple_script = f'display notification "{message}" with title "{title}" sound name "Glass"'
                    subprocess.run(["osascript", "-e", apple_script], capture_output=True)
                elif system == "Linux" and shutil.which("notify-send"):
                    subprocess.run(["notify-send", title, message], capture_output=True)
                elif system == "Windows":
                    safe_title = title.replace("'", "").replace('"', "")
                    safe_msg = message.replace("'", "").replace('"', "")
                    ps_cmd = f"""
                    [reflection.assembly]::loadwithpartialname('System.Windows.Forms') | Out-Null
                    [reflection.assembly]::loadwithpartialname('System.Drawing') | Out-Null
                    $notify = New-Object System.Windows.Forms.NotifyIcon
                    $notify.Icon = [System.Drawing.SystemIcons]::Information
                    $notify.Visible = $True
                    $notify.ShowBalloonTip(3000, '{safe_title}', '{safe_msg}', [System.Windows.Forms.ToolTipIcon]::Info)
                    Start-Sleep -Seconds 1
                    $notify.Dispose()
                    """
                    subprocess.run(
                        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
                        capture_output=True, text=True, timeout=5
                    )
            except Exception:
                pass

        threading.Thread(target=_send, daemon=True).start()

    # ========================================================
    # QUẢN LÝ DEDUP & CHU KỲ QUOTA TOÀN CẦU (07:00 AM VN)
    # ========================================================

    def get_current_quota_cycle_start(self):
        """Hạn mức OpenCode reset lúc 00:00 UTC (tức 7:00 AM giờ Việt Nam).
        Tính mốc 7:00 AM gần nhất. Nếu IP được dùng trước mốc này, nó đã được reset."""
        now = datetime.datetime.now()
        reset_today = now.replace(hour=7, minute=0, second=0, microsecond=0)
        if now < reset_today:
            reset_today -= datetime.timedelta(days=1)
        return reset_today.timestamp()

    def load_used_ips(self):
        if not os.path.exists(self.used_ips_file):
            return {"ips": {}}
        try:
            with open(self.used_ips_file, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {"ips": {}}

    def save_used_ips(self, data):
        try:
            with open(self.used_ips_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def is_ip_used(self, ip):
        if not ip or len(ip) < 7 or "." not in ip:
            return False
        data = self.load_used_ips()
        ips = data.get("ips", {})
        if ip in ips:
            info = ips[ip]
            last_seen = info.get("timestamp", 0)
            if last_seen >= self.get_current_quota_cycle_start():
                return True
        return False

    def mark_ip_used(self, ip, reason=""):
        if not ip or len(ip) < 7 or "." not in ip:
            return
        data = self.load_used_ips()
        if "ips" not in data:
            data["ips"] = {}

        now = datetime.datetime.now()
        ts = now.timestamp()
        time_str = now.strftime("%Y-%m-%d %H:%M:%S")

        current_entry = data["ips"].get(ip, {})
        count = current_entry.get("count", 0) + 1
        first_seen = current_entry.get("first_seen", time_str)

        data["ips"][ip] = {
            "timestamp": ts,
            "last_seen": time_str,
            "first_seen": first_seen,
            "count": count,
            "reason": reason or current_entry.get("reason", "Used")
        }
        self.save_used_ips(data)

    def get_used_ips_summary(self):
        data = self.load_used_ips()
        cycle_start = self.get_current_quota_cycle_start()
        active_used = []
        for ip, info in data.get("ips", {}).items():
            if info.get("timestamp", 0) >= cycle_start:
                active_used.append((ip, info.get("last_seen", ""), info.get("count", 1), info.get("reason", "")))
        return active_used

    def clear_used_ips(self):
        self.save_used_ips({"ips": {}})
        self.log("🗑️ Đã xóa sạch danh sách IP đã dùng.")

    # ========================================================
    # HEALTH CHECK & XOAY IP
    # ========================================================

    def check_health(self, timeout=7):
        endpoints = [
            "https://api.ipify.org",
            "https://icanhazip.com",
            "https://ifconfig.me/ip"
        ]
        proxy_url = f"http://127.0.0.1:{self.local_port}"
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url})
        )
        for ep in endpoints:
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": "Mozilla/5.0"})
                with opener.open(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        ip = resp.read().decode("utf-8").strip()
                        if ("." in ip or ":" in ip) and len(ip) <= 45:
                            return ip
            except Exception:
                pass

        # Fallback thử qua trực tiếp upstream
        clean_upstream = self.upstream_proxy
        if "://" in clean_upstream:
            clean_upstream = clean_upstream.split("://", 1)[1]
        if "@" in clean_upstream:
            clean_upstream = clean_upstream.split("@", 1)[1]
        upstream_url = f"http://{clean_upstream}"
        opener_up = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": upstream_url, "https": upstream_url})
        )
        for ep in endpoints[:2]:
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": "Mozilla/5.0"})
                with opener_up.open(req, timeout=timeout) as resp:
                    if resp.status == 200:
                        ip = resp.read().decode("utf-8").strip()
                        if ("." in ip or ":" in ip) and len(ip) <= 45:
                            return ip
            except Exception:
                pass

        return None

    def rotate(self, force=False, reason="Yêu cầu xoay IP"):
        self.log("========================================================")
        self.log(f"🔄 Bắt đầu xoay IP tự động [Lý do: {reason}]...")

        remaining_seconds = 1200
        chosen_ip = None

        for dedup_attempt in range(1, self.max_dedup_attempts + 1):
            if dedup_attempt > 1:
                self.log(f"🔄 Đang xoay lại để tìm IP mới tinh (Lần thử {dedup_attempt}/{self.max_dedup_attempts})...")

            # 1. Gửi request đổi IP tới API
            for req_attempt in range(1, 5):
                try:
                    req = urllib.request.Request(self.api_url, headers={"User-Agent": "Mozilla/5.0"})
                    with urllib.request.urlopen(req, timeout=20) as resp:
                        content = resp.read().decode("utf-8")
                        api_response = {}
                        try:
                            api_response = json.loads(content)
                            msg = api_response.get("message", content)
                        except Exception:
                            msg = content

                        self.log(f"📡 API phản hồi: {msg}")

                        # Kiểm tra Cooldown (hỗ trợ cả có dấu và không dấu tiếng Việt)
                        cooldown_match = re.search(r"(?:Con|Còn)?\s*(\d+)\s*s\s*(?:moi|mới)?\s*co\s*the\s*doi|cooldown.*?(\d+)|doi sau (\d+)s", msg, re.IGNORECASE)
                        if cooldown_match:
                            raw_s = next(g for g in cooldown_match.groups() if g is not None)
                            wait_s = int(raw_s) + self.cooldown_extra_seconds
                            self.log(f"⏳ API yêu cầu chờ cooldown {wait_s}s để đổi IP mới. Đang chờ đúng thời gian...")
                            self.notify("Proxy Cooldown", f"Đang chờ {wait_s}s để đổi IP mới...")
                            time.sleep(wait_s)
                            continue

                        # Đọc thời gian sống còn lại
                        match = re.search(r"die sau (\d+)s", msg)
                        if match:
                            remaining_seconds = int(match.group(1))
                            self.log(f"⏱️ Thời hạn sống của IP: {remaining_seconds}s (~{remaining_seconds/60:.1f} phút)")
                        break
                except Exception as e:
                    self.log(f"⚠️ Lỗi gọi API xoay IP (lần {req_attempt}/4): {e}")
                    time.sleep(3)

            # 2. Đợi modem 4G hoàn tất đổi IP
            self.log("⏳ Đang đợi modem 4G hoàn tất cấp IP mới...")
            time.sleep(25)

            # 3. Thăm dò kiểm tra IP mới
            new_ip = None
            for attempt in range(1, 20):
                new_ip = self.check_health()
                if new_ip:
                    break
                self.log(f"  · Đang kiểm tra kết nối proxy (lần {attempt}/20)...")
                time.sleep(2)

            if not new_ip:
                self.log("⚠️ Chưa nhận được IP mới ở lần thử này.")
                continue

            # 4. Kiểm tra trùng lặp
            if self.is_ip_used(new_ip):
                used_list = self.get_used_ips_summary()
                self.log(f"⚠️ IP {new_ip} ĐÃ TỪNG SỬ DỤNG HÔM NAY (trùng trong {len(used_list)} IP đã dùng)!")
                self.log(f"🚫 Bỏ qua IP {new_ip} và tiếp tục xoay để lấy IP khác...")
                self.notify("Trùng IP Cũ", f"IP {new_ip} đã dùng. Đang tự động đổi IP khác...")
                time.sleep(3)
                continue
            else:
                chosen_ip = new_ip
                self.mark_ip_used(chosen_ip, reason=reason)
                self.log(f"🎉 ĐÃ LẤY ĐƯỢC IP MỚI TINH (Chưa dùng hôm nay): {chosen_ip}")
                break

        if not chosen_ip and new_ip:
            chosen_ip = new_ip
            self.mark_ip_used(chosen_ip, reason="Fallback (Pool exhausted)")
            self.log(f"⚠️ Đã thử {self.max_dedup_attempts} lần xoay nhưng pool IP bị lặp. Tạm dùng IP: {chosen_ip}")

        # 5. Sinh Device UUID mới
        new_uuid = generate_new_device_id()

        if chosen_ip:
            self.log(f"✅ HOÀN TẤT XOAY IP! IP hiện tại: {chosen_ip} | Device ID: {new_uuid}")
            self.notify("Proxy Xoay Thành Công", f"Đã kết nối IP mới: {chosen_ip}")
            self.log("========================================================")
            return True, chosen_ip, remaining_seconds
        else:
            self.log("❌ Không thể lấy IP mới sau các lần thử.")
            self.log("========================================================")
            return False, None, remaining_seconds

if __name__ == "__main__":
    api_url = sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].startswith("http") else "https://proxyxoay.shop/api/get.php?key=YOUR_KEY"
    rotator = ProxyRotator(api_url)

    if "--list-ips" in sys.argv:
        used = rotator.get_used_ips_summary()
        cycle_dt = datetime.datetime.fromtimestamp(rotator.get_current_quota_cycle_start()).strftime("%Y-%m-%d %H:%M:%S")
        print(f"📋 Danh sách IP đã dùng trong chu kỳ hôm nay (từ {cycle_dt}): {len(used)} IP")
        for ip, last_seen, count, reason in sorted(used, key=lambda x: x[1]):
            print(f"  • {ip:<16} (Lần cuối: {last_seen}, Số lần xoay trúng: {count}, Lý do: {reason})")
        sys.exit(0)

    if "--clear-ips" in sys.argv:
        rotator.clear_used_ips()
        print("✅ Đã xóa sạch danh sách IP đã dùng.")
        sys.exit(0)

    print("ℹ️ Chạy rotator trực tiếp. Dùng --list-ips hoặc --clear-ips.")
