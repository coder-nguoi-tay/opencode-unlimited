#!/usr/bin/env python3
"""
OpenCode Auto-Reset & Auto-Resume Watchdog (100% Tự động & Liền mạch)
Tác giả: Antigravity Assistant
Mục đích:
- Tự động phát hiện khi OpenCode chạm hạn mức (Limit / 429 / QuotaExceeded / today's free messages / 503 / ECONNRESET).
- Tự động lưu và blacklist IP bị limit để lần sau không bao giờ tái sử dụng IP đó.
- Đứng chờ đúng thời gian cooldown của nhà mạng / API nếu có (không để gián đoạn hay lỗi dồn dập).
- Tự động gọi API xoay IP, lọc bỏ các IP trùng lặp và cấp Device UUID mới.
- TỰ ĐỘNG GỬI LỆNH TIẾP TỤC (RESUME) cho phiên làm việc hiện tại mà KHÔNG CẦN con người can thiệp hay bấm tiếp tục.
"""

import os
import sys
import time
import re
import json
import subprocess
import sqlite3
import datetime
import threading

LOG_DIR = os.path.expanduser("~/.local/share/opencode/log")
LOG_FILE = os.path.join(LOG_DIR, "opencode.log")
AUTORESET_LOG = os.path.join(LOG_DIR, "opencode-autoreset.log")
LOCK_FILE = "/tmp/opencode_reset_in_progress"
LAST_TRIGGER_FILE = "/tmp/opencode_last_trigger"
DB_FILE = os.path.expanduser("~/.local/share/opencode/opencode.db")
OPENCODE_BIN = os.path.expanduser("~/.local/bin/opencode")
if not os.path.exists(OPENCODE_BIN):
    OPENCODE_BIN = os.path.expanduser("~/.opencode/bin/opencode")
os.environ["PATH"] = f"{os.path.expanduser('~/.local/bin')}:{os.path.expanduser('~/.opencode/bin')}:/opt/homebrew/bin:/usr/local/bin:{os.environ.get('PATH', '')}"

PROXY_ROTATE_CMD = os.path.expanduser("~/.local/bin/proxy-rotate")
USED_IPS_FILE = os.path.expanduser("~/.local/share/opencode/used_ips.json")

os.makedirs(LOG_DIR, exist_ok=True)

def log(msg):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{now}] {msg}"
    print(line, flush=True)
    try:
        with open(AUTORESET_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def notify(title, message):
    try:
        apple_script = f'display notification "{message}" with title "{title}" sound name "Glass"'
        subprocess.run(["osascript", "-e", apple_script], capture_output=True)
    except Exception:
        pass

def mark_ip_used(ip, reason=""):
    """Đánh dấu IP đã dùng vào blacklist file."""
    if not ip or len(ip) < 7 or "." not in ip:
        return
    data = {"ips": {}}
    if os.path.exists(USED_IPS_FILE):
        try:
            with open(USED_IPS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {"ips": {}}
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
    try:
        with open(USED_IPS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

def get_active_sessions():
    """Lấy danh sách các session đang active từ OpenCode API."""
    try:
        res = subprocess.run(
            [OPENCODE_BIN, "api", "get", "/api/session/active"],
            capture_output=True, text=True, timeout=5
        )
        if res.returncode == 0:
            data = json.loads(res.stdout)
            active = data.get("data", {})
            if isinstance(active, dict):
                return list(active.keys())
    except Exception:
        pass
    return []

def get_latest_session_id():
    """Lấy session ID gần nhất từ database SQLite của OpenCode."""
    if not os.path.exists(DB_FILE):
        return None
    try:
        conn = sqlite3.connect(DB_FILE, timeout=3)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM session_v2 ORDER BY time_updated DESC LIMIT 1;")
        row = cursor.fetchone()
        conn.close()
        if row:
            return row[0]
    except Exception:
        pass
    return None

def check_proxy_health():
    """Kiểm tra xem kết nối Internet qua proxy cục bộ 28080 đã sống chưa và trả về IP public."""
    try:
        cmd = [
            "curl", "-s", "--max-time", "6",
            "-x", "http://127.0.0.1:28080",
            "https://api.ipify.org"
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        if res.returncode == 0 and res.stdout.strip():
            ip = res.stdout.strip()
            if len(ip) >= 7 and "." in ip:
                return ip
    except Exception:
        pass
    return None

def send_resume_prompt(session_id, prompt="Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở."):
    """Gửi prompt tiếp tục tới session qua OpenCode API."""
    log(f"💬 Đang gửi lệnh tự động tiếp tục tới session: {session_id}...")
    try:
        body = json.dumps({
            "text": prompt,
            "delivery": "steer",
            "resume": True
        })
        cmd = [
            OPENCODE_BIN, "api", "post",
            f"/api/session/{session_id}/prompt",
            "-d", body
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if res.returncode == 0:
            log(f"✅ Gửi lệnh tiếp tục thành công! Phiên {session_id} đang chạy tiếp.")
            return True, res.stdout
        else:
            log(f"⚠️ Lỗi khi gửi lệnh tiếp tục (code {res.returncode}): {res.stderr or res.stdout}")
            return False, res.stderr or res.stdout
    except Exception as e:
        log(f"⚠️ Exception khi gửi lệnh tiếp tục: {e}")
        return False, str(e)

is_handling = False
lock = threading.Lock()

def handle_limit_and_resume(reason, session_hint=None, delay_seconds=0):
    global is_handling
    with lock:
        if is_handling:
            log("⏳ Đang trong quá trình reset/resume phiên trước, bỏ qua trigger trùng lặp.")
            return
        is_handling = True

    try:
        log("========================================================")
        log(f"🚨 PHÁT HIỆN LỖI/HẠN MỨC TRONG OPENCODE!")
        log(f"   Lý do: {reason}")

        # 1. Ghi nhận và blacklist ngay IP đang bị limit (nếu lấy được)
        failing_ip = check_proxy_health()
        if failing_ip:
            log(f"🚫 Đánh dấu IP {failing_ip} vào danh sách đã dùng (bị chặn: {reason})")
            mark_ip_used(failing_ip, reason=f"Chặn limit: {reason}")

        # 2. Xác định target session ID
        target_session = session_hint
        if not target_session:
            active_list = get_active_sessions()
            if active_list:
                target_session = active_list[0]
        if not target_session:
            target_session = get_latest_session_id()

        log(f"   Target Session: {target_session or 'Chưa xác định'}")

        # 3. Nếu có độ trễ bắt buộc (ví dụ retryAfterMs)
        if delay_seconds > 0:
            log(f"⏳ Cần chờ {delay_seconds}s trước khi reset theo yêu cầu hệ thống...")
            time.sleep(delay_seconds)

        # 4. Kích hoạt xoay IP khẩn cấp (có bộ lọc chống trùng IP)
        notify("OpenCode Auto-Resume", "Đang tự động xoay IP mới tinh (chống trùng IP cũ)...")
        rotate_cmd = [
            PROXY_ROTATE_CMD,
            "--force",
            f"--reason=Chạm hạn mức trên session {target_session or 'active'}"
        ]
        res = subprocess.run(rotate_cmd, capture_output=True, text=True, timeout=360)
        log(f"🔄 proxy-rotate kết thúc với mã {res.returncode}")

        # 5. Xác nhận kết nối Internet qua proxy đã lên 100%
        new_ip = None
        for i in range(1, 15):
            new_ip = check_proxy_health()
            if new_ip:
                break
            time.sleep(2)

        if not new_ip:
            log("❌ Không thể kết nối proxy sau khi xoay IP! Tạm hoãn gửi prompt.")
            return

        log(f"🎉 Kết nối mới đã sẵn sàng: IP = {new_ip}")

        # 6. Đợi đúng 60 giây (1 phút) theo yêu cầu để IP mới và OpenCode ổn định hoàn toàn
        log(f"⏳ Đã đổi IP thành công sang {new_ip}. Đang đợi 60 giây (1 phút) để đường truyền và hệ thống ổn định trước khi tự động chạy tiếp task...")
        notify("OpenCode Auto-Resume", f"Đã có IP mới ({new_ip}). Đang chờ 1 phút trước khi tự động tiếp tục task...")

        for remaining in range(60, 0, -15):
            log(f"   ⏱️ Còn {remaining}s nữa sẽ tự động gửi lệnh tiếp tục task...")
            time.sleep(min(15, remaining))

        # 7. Tự động gửi prompt tiếp tục phiên làm việc
        if target_session:
            log(f"🚀 Hết 1 phút chờ! Tự động gửi lệnh tiếp tục công việc cho phiên {target_session}...")
            success, output = send_resume_prompt(target_session)
            if success:
                notify("OpenCode Auto-Resume", f"Đã tự động tiếp tục code phiên {target_session[:12]}...")
            else:
                time.sleep(5)
                send_resume_prompt(target_session)
        else:
            log("⚠️ Không tìm thấy session nào để tự động resume.")

        log("========================================================")
    except Exception as e:
        log(f"❌ Lỗi trong luồng handle_limit_and_resume: {e}")
    finally:
        with lock:
            is_handling = False

def monitor_log():
    log("👀 OpenCode Auto-Resume Watchdog đang giám sát log thời gian thực...")
    log(f"   Log file: {LOG_FILE}")
    log("   Chế độ: Tự động đổi IP + Chống trùng IP đã dùng + Chờ 60s + TỰ ĐỘNG RESUME 100%.")

    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "a").close()

    try:
        f = open(LOG_FILE, "r", encoding="utf-8", errors="ignore")
        f.seek(0, os.SEEK_END)
        current_inode = os.fstat(f.fileno()).st_ino

        last_trigger_time = 0

        while True:
            if os.path.exists(LOG_FILE):
                try:
                    new_inode = os.stat(LOG_FILE).st_ino
                    if new_inode != current_inode:
                        f.close()
                        f = open(LOG_FILE, "r", encoding="utf-8", errors="ignore")
                        current_inode = new_inode
                        log("🔄 Đã phát hiện log rotate, kết nối lại file log mới.")
                except Exception:
                    pass

            line = f.readline()
            if not line:
                time.sleep(0.4)
                continue

            # Kiểm tra các mẫu lỗi giới hạn quota / proxy disconnect
            is_trigger = False
            reason = ""
            line_lower = line.lower()

            if "free usage limit reached" in line_lower:
                is_trigger = True
                reason = "Free usage limit reached"
            elif "quotaexceeded" in line_lower:
                is_trigger = True
                reason = "QuotaExceeded"
            elif "today's free messages" in line_lower or "free messages" in line_lower:
                is_trigger = True
                reason = "today's free messages"
            elif "freeusagelimiterror" in line_lower:
                is_trigger = True
                reason = "FreeUsageLimitError"
            elif "provider.free-limit" in line_lower:
                is_trigger = True
                reason = "provider.free-limit"
            elif 'status":429' in line or 'status": 429' in line or "http 429" in line_lower or "ratelimit" in line_lower:
                is_trigger = True
                reason = "HTTP 429 RateLimit"
            elif "http 503" in line_lower or 'status":503' in line or 'status": 503' in line or "service unavailable" in line_lower:
                is_trigger = True
                reason = "HTTP 503 Service Unavailable"
            elif "econnreset" in line_lower or "socket connection was closed unexpectedly" in line_lower or "unexpected eof" in line_lower:
                is_trigger = True
                reason = "ECONNRESET / Socket Reset"

            if is_trigger:
                now = time.time()
                # Chống trigger lặp lại dồn dập (tối thiểu cách nhau 25s)
                if now - last_trigger_time < 25:
                    continue
                last_trigger_time = now

                session_hint = None
                s_match = re.search(r"sessionID=([a-zA-Z0-9_-]+)", line)
                if not s_match:
                    s_match = re.search(r"session\.id=([a-zA-Z0-9_-]+)", line)
                if s_match:
                    session_hint = s_match.group(1)

                delay = 0
                d_match = re.search(r"retryAfterMs\":\s*(\d+)", line)
                if d_match:
                    delay = int(d_match.group(1)) / 1000.0

                t = threading.Thread(
                    target=handle_limit_and_resume,
                    args=(reason, session_hint, delay)
                )
                t.daemon = True
                t.start()

    except KeyboardInterrupt:
        log("🛑 Đã dừng Watchdog thủ công.")
    except Exception as e:
        log(f"❌ Watchdog gặp lỗi ngoại lệ: {e}")

if __name__ == "__main__":
    monitor_log()
