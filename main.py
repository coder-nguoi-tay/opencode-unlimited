#!/usr/bin/env python3
"""
OpenCode Unlimited - Master Cross-Platform Runner (1-Command All-in-One)
Tác giả: coder-nguoi-tay
Mục đích:
- 1 Lệnh duy nhất chạy trơn tru trên cả macOS, Linux, Windows.
- Tự động dựng Local Proxy Bridge (không cần GOST).
- Tự động xoay IP khi hết lượt + Lọc bỏ các IP trùng lặp.
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

from bridge import ProxyBridge
from rotator import ProxyRotator
from watchdog import OpenCodeWatchdog
from spoofer import setup_spoofer

CONFIG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
CONFIG_EXAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.example.json")

def load_or_init_config():
    """Đọc cấu hình hoặc khởi tạo tương tác nếu chưa có."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"⚠️ Không thể đọc {CONFIG_FILE}, dùng cấu hình mặc định: {e}")

    # Nếu chưa có config.json, kiểm tra nếu có config.example.json
    default_config = {
        "api_url": "https://proxyxoay.shop/api/get.php?key=YOUR_API_KEY",
        "upstream_proxy": "160.250.166.20:10427",
        "local_port": 28080,
        "cooldown_extra_seconds": 2,
        "stabilize_wait_seconds": 60,
        "max_dedup_attempts": 10,
        "auto_resume": True,
        "prompt_resume_text": "Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở."
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
    args = parser.parse_args()

    cfg = load_or_init_config()

    # Khởi tạo rotator & spoofer
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

    # Tách Host và Port của Upstream
    upstream = cfg["upstream_proxy"]
    if "://" in upstream:
        upstream = upstream.split("://", 1)[1]
    if ":" in upstream:
        u_host, u_port = upstream.split(":", 1)
    else:
        u_host, u_port = upstream, 8080

    # 1. Khởi động Proxy Bridge
    bridge = ProxyBridge(u_host, int(u_port), local_host="127.0.0.1", local_port=cfg["local_port"])
    bridge.start()

    # 2. Sinh Device ID ban đầu
    setup_spoofer()

    # 3. Lấy IP hiện tại qua Bridge
    current_ip = bridge.check_health()

    print_banner(cfg, public_ip=current_ip)

    if args.rotate:
        rotator.rotate(force=True, reason="Chạy lệnh thủ công --rotate")
        return

    # Thiết lập biến môi trường Proxy cho phiên hiện tại
    proxy_url = f"http://127.0.0.1:{cfg['local_port']}"
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    os.environ["ALL_PROXY"] = proxy_url
    os.environ["NO_PROXY"] = "localhost,127.0.0.1,::1"

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
        print(f"🚀 Đang mở OpenCode CLI ({opencode_bin})...\n")
        try:
            subprocess.run([opencode_bin], env=os.environ)
        except KeyboardInterrupt:
            pass
        return

    # Vòng lặp giữ process hoạt động
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n🛑 Đang tắt OpenCode Unlimited...")
        bridge.stop()
        print("👋 Tạm biệt!")

if __name__ == "__main__":
    main()
