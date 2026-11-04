#!/usr/bin/env python3
"""
OpenCode Unlimited - Master Cross-Platform Runner (1-Command All-in-One)
Tác giả: coder-nguoi-tay
Mục đích:
- 1 Lệnh duy nhất chạy trơn tru trên cả macOS, Linux và Windows 10 & 11.
- Tự động dựng Local Proxy Bridge (không cần GOST).
- Tự động xoay IP khi hết lượt + Lọc bỏ các IP trùng lặp trong ngày.
- Tự động theo dõi log và Resume task đang làm dở 100% tự động.
"""

import os
import sys
import json
import time
import platform
import argparse
import subprocess
import signal
import threading

# Fix mã hóa Unicode tiếng Việt trên Windows Console (tránh lỗi charmap/cp1252)
# Đồng thời hỗ trợ pythonw.exe chạy nền không có console window (sys.stdout is None)
if sys.platform == "win32":
    if sys.stdout is None:
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "opencode_unlimited.log")
        sys.stdout = open(log_path, "a", encoding="utf-8", buffering=1)
    elif hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    if sys.stderr is None:
        sys.stderr = sys.stdout if sys.stdout else open(os.devnull, "w", encoding="utf-8")
    elif hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from bridge import ProxyBridge
from rotator import ProxyRotator
from watchdog import OpenCodeWatchdog
from spoofer import setup_spoofer

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")

def parse_proxy_host_port(upstream_str):
    """Tách Host và Port từ chuỗi proxy (hỗ trợ IP:PORT, user:pass@IP:PORT, IP:PORT:user:pass, http://...)."""
    s = str(upstream_str).strip()
    if "://" in s:
        s = s.split("://", 1)[1]
    if "@" in s:
        s = s.split("@", 1)[1]
    parts = s.split(":")
    if len(parts) >= 2:
        host = parts[0].strip()
        port_str = parts[1].strip()
        if port_str.isdigit():
            return host, int(port_str)
    return s, 8080

def set_windows_system_proxy(enable=True, proxy_server="127.0.0.1:28080"):
    """Tùy chọn thiết lập System Proxy trên Windows (HKCU không cần quyền Admin)."""
    if platform.system() != "Windows":
        return
    try:
        import winreg
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
            if enable:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_server)
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, "localhost;127.0.0.1;<local>;*.teamviewer.com;*.teamviewer.de;*.tm-server.com;*.anydesk.com")
            else:
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
    except Exception:
        pass

def load_or_init_config():
    """Đọc cấu hình hoặc khởi tạo tương tác nếu chưa có."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Không thể đọc {CONFIG_FILE}, dùng cấu hình mặc định: {e}")

    # Cấu hình mặc định
    default_config = {
        "api_url": "https://proxyxoay.shop/api/get.php?key=YOUR_API_KEY",
        "upstream_proxy": "160.250.166.20:10427",
        "local_port": 28080,
        "cooldown_extra_seconds": 2,
        "stabilize_wait_seconds": 60,
        "max_dedup_attempts": 10,
        "auto_resume": True,
        "prompt_resume_text": "Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở.",
        "enable_system_proxy": False
    }

    if os.path.exists(CONFIG_EXAMPLE):
        try:
            with open(CONFIG_EXAMPLE, "r", encoding="utf-8") as f:
                default_config.update(json.load(f))
        except Exception:
            pass

    # Nếu đang chạy interactive (có stdin), hỏi người dùng
    if sys.stdin.isatty() and not os.path.exists(CONFIG_FILE):
        print("\n========================================================")
        print(" 🚀 CHÀO MỪNG ĐẾN VỚI OPENCODE UNLIMITED (SETUP LẦN ĐẦU)")
        print("========================================================")
        api_key = input("👉 Nhập API Key hoặc Link xoay IP (Enter để bỏ qua nếu chỉnh sau): ").strip()
        if api_key:
            if api_key.startswith("http"):
                default_config["api_url"] = api_key
            else:
                default_config["api_url"] = f"https://proxyxoay.shop/api/get.php?key={api_key}"

        upstream = input(f"👉 Nhập Upstream Proxy Host:Port [{default_config['upstream_proxy']}]: ").strip()
        if upstream:
            default_config["upstream_proxy"] = upstream

        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(default_config, f, ensure_ascii=False, indent=2)
            print(f"✅ Đã lưu cấu hình vào: {CONFIG_FILE}\n")
        except Exception as e:
            print(f"⚠️ Không thể lưu config.json: {e}")

    return default_config

def print_banner(cfg, public_ip=None):
    os_name = f"{platform.system()} {platform.release()} ({platform.machine()})"
    print(r"""
   ____                   ______          __        __  __      ___           _ __           __
  / __ \____  ___  ____  / ____/___  ____/ /__     / / / /___  / (_)___ ___  (_) /____  ____/ /
 / / / / __ \/ _ \/ __ \/ /   / __ \/ __  / _ \   / / / / __ \/ / / __ `__ \/ / __/ _ \/ __  / 
/ /_/ / /_/ /  __/ / / / /___/ /_/ / /_/ /  __/  / /_/ / / / / / / / / / / / / /_/  __/ /_/ /  
\____/ .___/\___/_/ /_/\____/\____/\__,_/\___/   \____/_/ /_/_/_/_/ /_/ /_/_/\__/\___/\__,_/   
    /_/                                                                                         
    """)
    print("================================================================================")
    print(f" 💻 Hệ điều hành:      {os_name}")
    print(f" 🔌 Local Bridge:       127.0.0.1:{cfg['local_port']}")
    print(f" 🌐 Upstream Proxy:     {cfg['upstream_proxy']}")
    print(f" 📡 API Xoay IP:        {cfg['api_url'][:40]}...")
    print(f" 🛡️ Lọc trùng IP:       Bật (Tự động loại bỏ các IP đã dùng trong ngày)")
    print(f" ⏱️ Chờ ổn định:       {cfg['stabilize_wait_seconds']}s trước khi tự động Resume")
    print(f" 🎯 Trạng thái mạng:    {public_ip or 'Đang kiểm tra kết nối...'}")
    print("================================================================================\n")

def main():
    parser = argparse.ArgumentParser(description="OpenCode Unlimited - Cross-Platform Automation")
    parser.add_argument("--rotate", action="store_true", help="Chủ động xoay IP mới ngay lập tức")
    parser.add_argument("--list-ips", action="store_true", help="Xem danh sách IP đã dùng trong chu kỳ hôm nay")
    parser.add_argument("--clear-ips", action="store_true", help="Xóa sạch danh sách IP đã dùng")
    parser.add_argument("--launch", action="store_true", help="Tự động khởi động OpenCode sau khi bật proxy")
    parser.add_argument("--system-proxy", action="store_true", help="Bật Windows System Proxy (cho OpenCode Desktop GUI)")
    args = parser.parse_args()

    cfg = load_or_init_config()

    # Khởi tạo rotator
    rotator = ProxyRotator(
        api_url=cfg["api_url"],
        upstream_proxy=cfg["upstream_proxy"],
        local_port=cfg["local_port"],
        cooldown_extra_seconds=cfg.get("cooldown_extra_seconds", 2),
        max_dedup_attempts=cfg.get("max_dedup_attempts", 10)
    )

    if args.list_ips:
        used = rotator.get_used_ips_summary()
        cycle_dt = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(rotator.get_current_quota_cycle_start()))
        print(f"📋 Danh sách IP đã dùng hôm nay (từ {cycle_dt}): {len(used)} IP")
        for ip, last_seen, count, reason in sorted(used, key=lambda x: x[1]):
            print(f"  • {ip:<16} (Lần cuối: {last_seen}, Số lần: {count}, Lý do: {reason})")
        return

    if args.clear_ips:
        rotator.clear_used_ips()
        print("✅ Đã xóa sạch danh sách IP đã dùng.")
        return

    # Tách Host và Port của Upstream an toàn
    u_host, u_port = parse_proxy_host_port(cfg["upstream_proxy"])

    # 1. Khởi động Proxy Bridge
    bridge = ProxyBridge(u_host, u_port, local_host="127.0.0.1", local_port=cfg["local_port"])
    bridge.start()

    # 2. Sinh Device ID ban đầu (và đồng bộ Registry MachineGuid trên Windows)
    initial_device_id = setup_spoofer()

    # 3. Lấy IP hiện tại qua Bridge
    current_ip = bridge.check_health()
    print_banner(cfg, public_ip=current_ip)

    if args.rotate:
        rotator.rotate(force=True, reason="Chạy lệnh thủ công --rotate")
        bridge.stop()
        return

    # Thiết lập biến môi trường Proxy cho phiên hiện tại
    proxy_url = f"http://127.0.0.1:{cfg['local_port']}"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["ALL_PROXY"] = proxy_url
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

    # Tùy chọn kích hoạt Windows System Proxy
    use_system_proxy = args.system_proxy or cfg.get("enable_system_proxy", False)
    if use_system_proxy and platform.system() == "Windows":
        set_windows_system_proxy(enable=True, proxy_server=f"127.0.0.1:{cfg['local_port']}")
        print("🌐 Đã bật Windows System Proxy -> 127.0.0.1:" + str(cfg['local_port']))

    # 4. Khởi động Watchdog giám sát OpenCode
    watchdog = OpenCodeWatchdog(
        rotator=rotator,
        stabilize_wait_seconds=cfg.get("stabilize_wait_seconds", 60),
        auto_resume=cfg.get("auto_resume", True),
        prompt_resume_text=cfg.get("prompt_resume_text", "Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở.")
    )

    watchdog_thread = threading.Thread(target=watchdog.start_monitoring, daemon=True)
    watchdog_thread.start()

    print("🟢 Tất cả hệ thống đã sẵn sàng!")
    print(f"👉 Proxy nội bộ: {proxy_url}")
    print("👉 Watchdog đang chạy ngầm và tự động Resume khi dính limit.")
    print("👉 Bấm Ctrl + C để dừng chương trình bất kỳ lúc nào.\n")

    # 5. Nếu có cờ --launch thì gọi OpenCode trực tiếp
    if args.launch:
        opencode_bin = watchdog.opencode_bin
        print(f"🚀 Đang mở OpenCode ({opencode_bin})...\n")
        try:
            if platform.system() == "Windows" and opencode_bin.lower().endswith((".cmd", ".bat")):
                subprocess.run(["cmd.exe", "/c", opencode_bin], env=os.environ)
            else:
                subprocess.run([opencode_bin], env=os.environ)
        except KeyboardInterrupt:
            pass
        finally:
            if use_system_proxy and platform.system() == "Windows":
                set_windows_system_proxy(enable=False)
            bridge.stop()
        return

    # Vòng lặp giữ process hoạt động
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Đang tắt OpenCode Unlimited...")
    finally:
        if use_system_proxy and platform.system() == "Windows":
            set_windows_system_proxy(enable=False)
            print("🌐 Đã khôi phục Windows System Proxy về mặc định.")
        bridge.stop()
        print("👋 Tạm biệt!")

if __name__ == "__main__":
    main()
