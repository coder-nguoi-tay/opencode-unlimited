#!/usr/bin/env python3
"""
OpenCode Unlimited - Watchdog & Auto-Resume Engine
Tác giả: coder-nguoi-tay
Mục đích:
- Giám sát file opencode.log trong thời gian thực trên mọi hệ điều hành.
- Phát hiện tức thì các lỗi: 429, 503, QuotaExceeded, today's free messages, ECONNRESET.
- Tự động gọi rotator để cấp IP mới sạch (không trùng lặp).
- Chờ 60 giây để đường truyền và hệ thống ổn định hoàn toàn.
- TỰ ĐỘNG GỬI PROMPT TIẾP TỤC (RESUME) cho phiên làm việc hiện tại mà không cần thao tác tay.
"""

import os
import sys
import time
import re
import json
import sqlite3
import datetime
import threading
import subprocess
import platform
import shutil

from spoofer import get_opencode_dir

class OpenCodeWatchdog:
    def __init__(self, rotator, stabilize_wait_seconds=60, auto_resume=True,
                 prompt_resume_text="Tiếp tục thực hiện các bước tiếp theo trong công việc đang làm dở."):
        self.rotator = rotator
        self.stabilize_wait_seconds = int(stabilize_wait_seconds)
        self.auto_resume = auto_resume
        self.prompt_resume_text = prompt_resume_text

        self.opencode_dir = get_opencode_dir()
        self.log_file = os.path.join(self.opencode_dir, "log", "opencode.log")
        self.autoreset_log = os.path.join(self.opencode_dir, "log", "opencode-autoreset.log")
        self.db_file = os.path.join(self.opencode_dir, "opencode.db")

        self.opencode_bin = self._find_opencode_binary()
        self.lock = threading.Lock()
        self.is_handling = False
        self.running = False

    def _find_opencode_binary(self):
        system = platform.system()
        # 1. Thử qua PATH
        found = shutil.which("opencode")
        if found:
            return found

        # 2. Thử các đường dẫn mặc định
        home = os.path.expanduser("~")
        candidates = [
            os.path.join(home, ".local", "bin", "opencode"),
            os.path.join(home, ".opencode", "bin", "opencode"),
            os.path.join(home, ".opencode", "bin", "opencode.exe"),
            os.path.join(home, "AppData", "Local", "Programs", "opencode", "opencode.exe"),
        ]
        for c in candidates:
            if os.path.exists(c):
                return c

        return "opencode"

    def log(self, msg):
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{now}] {msg}"
        print(line, flush=True)
        try:
            with open(self.autoreset_log, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def get_active_sessions(self):
        """Lấy danh sách các session đang chạy từ API của OpenCode."""
        try:
            res = subprocess.run(
                [self.opencode_bin, "api", "get", "/api/session/active"],
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

    def get_latest_session_id(self):
        """Lấy session ID gần nhất từ database SQLite của OpenCode."""
        if not os.path.exists(self.db_file):
            return None
        try:
            conn = sqlite3.connect(self.db_file, timeout=3)
            cursor = conn.cursor()
            cursor.execute("SELECT id FROM session_v2 ORDER BY time_updated DESC LIMIT 1;")
            row = cursor.fetchone()
            conn.close()
            if row:
                return row[0]
        except Exception:
            pass
        return None

    def send_resume_prompt(self, session_id):
        """Gửi prompt tiếp tục tới session qua OpenCode API."""
        self.log(f"💬 Đang tự động gửi prompt tiếp tục tới session: {session_id}...")
        try:
            body = json.dumps({
                "text": self.prompt_resume_text,
                "delivery": "steer",
                "resume": True
            })
            cmd = [
                self.opencode_bin, "api", "post",
                f"/api/session/{session_id}/prompt",
                "-d", body
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            if res.returncode == 0:
                self.log(f"✅ Gửi lệnh tiếp tục thành công! Phiên {session_id} đang chạy tiếp.")
                return True, res.stdout
            else:
                self.log(f"⚠️ Lỗi khi gửi lệnh tiếp tục (code {res.returncode}): {res.stderr or res.stdout}")
                return False, res.stderr or res.stdout
        except Exception as e:
            self.log(f"⚠️ Ngoại lệ khi gửi lệnh tiếp tục: {e}")
            return False, str(e)

    def handle_limit_and_resume(self, reason, session_hint=None, delay_seconds=0):
        with self.lock:
            if self.is_handling:
                self.log("⏳ Đang trong quá trình reset/resume phiên trước, bỏ qua trigger trùng lặp.")
                return
            self.is_handling = True

        try:
            self.log("========================================================")
            self.log(f"🚨 PHÁT HIỆN LỖI/HẠN MỨC TRONG OPENCODE!")
            self.log(f"   Lý do: {reason}")

            # 1. Đánh dấu IP hiện tại vào blacklist
            failing_ip = self.rotator.check_health()
            if failing_ip:
                self.log(f"🚫 Đánh dấu IP {failing_ip} vào danh sách đã dùng (bị limit: {reason})")
                self.rotator.mark_ip_used(failing_ip, reason=f"Chặn limit: {reason}")

            # 2. Xác định session ID mục tiêu
            target_session = session_hint
            if not target_session:
                active_list = self.get_active_sessions()
                if active_list:
                    target_session = active_list[0]
            if not target_session:
                target_session = self.get_latest_session_id()

            self.log(f"   Target Session: {target_session or 'Chưa xác định'}")

            # 3. Đợi nếu hệ thống yêu cầu retry delay
            if delay_seconds > 0:
                self.log(f"⏳ Cần chờ {delay_seconds}s trước khi reset...")
                time.sleep(delay_seconds)

            # 4. Kích hoạt xoay IP lọc trùng
            self.rotator.notify("OpenCode Auto-Resume", "Đang tự động đổi IP sạch mới...")
            success, new_ip, _ = self.rotator.rotate(force=True, reason=f"Hết quota trên session {target_session or 'active'}")

            if not success or not new_ip:
                self.log("❌ Không thể kết nối IP mới sau khi xoay. Tạm hoãn gửi prompt.")
                return

            # 5. Đợi thời gian ổn định IP
            self.log(f"⏳ Đã có IP mới ({new_ip}). Đang đợi {self.stabilize_wait_seconds}s để đường truyền ổn định...")
            self.rotator.notify("OpenCode Auto-Resume", f"Đã có IP mới ({new_ip}). Đang chờ {self.stabilize_wait_seconds}s...")

            rem = self.stabilize_wait_seconds
            step = 15
            while rem > 0:
                self.log(f"   ⏱️ Còn {rem}s nữa sẽ tự động gửi lệnh tiếp tục task...")
                time.sleep(min(step, rem))
                rem -= step

            # 6. Tự động gửi prompt tiếp tục nếu được bật
            if self.auto_resume and target_session:
                self.log(f"🚀 Hết thời gian chờ! Tự động gửi lệnh tiếp tục phiên {target_session}...")
                ok, _ = self.send_resume_prompt(target_session)
                if ok:
                    self.rotator.notify("OpenCode Auto-Resume", f"Đã tự động tiếp tục code phiên {target_session[:12]}...")
                else:
                    time.sleep(5)
                    self.send_resume_prompt(target_session)
            elif not target_session:
                self.log("⚠️ Không tìm thấy session nào để tự động resume.")

            self.log("========================================================")
        except Exception as e:
            self.log(f"❌ Lỗi trong luồng xử lý: {e}")
        finally:
            with self.lock:
                self.is_handling = False

    def start_monitoring(self):
        """Bắt đầu theo dõi opencode.log theo thời gian thực."""
        self.running = True
        self.log("👀 OpenCode Watchdog đang giám sát log thời gian thực...")
        self.log(f"   File log: {self.log_file}")
        self.log(f"   Binary:   {self.opencode_bin}")

        if not os.path.exists(self.log_file):
            os.makedirs(os.path.dirname(self.log_file), exist_ok=True)
            open(self.log_file, "a").close()

        try:
            f = open(self.log_file, "r", encoding="utf-8", errors="ignore")
            f.seek(0, os.SEEK_END)
            last_trigger_time = 0

            while self.running:
                line = f.readline()
                if not line:
                    time.sleep(0.4)
                    continue

                line_lower = line.lower()
                is_trigger = False
                reason = ""

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
                elif "econnreset" in line_lower or "socket connection was closed unexpectedly" in line_lower:
                    is_trigger = True
                    reason = "ECONNRESET / Socket Reset"

                if is_trigger:
                    now = time.time()
                    if now - last_trigger_time < 25:
                        continue
                    last_trigger_time = now

                    session_hint = None
                    m1 = re.search(r"sessionID=([a-zA-Z0-9_-]+)", line)
                    if m1:
                        session_hint = m1.group(1)
                    else:
                        m2 = re.search(r"session\.id=([a-zA-Z0-9_-]+)", line)
                        if m2:
                            session_hint = m2.group(1)

                    delay = 0
                    dm = re.search(r"retryAfterMs\":\s*(\d+)", line)
                    if dm:
                        delay = int(dm.group(1)) / 1000.0

                    t = threading.Thread(
                        target=self.handle_limit_and_resume,
                        args=(reason, session_hint, delay),
                        daemon=True
                    )
                    t.start()
        except Exception as e:
            self.log(f"❌ Watchdog gặp lỗi: {e}")
        finally:
            self.running = False
